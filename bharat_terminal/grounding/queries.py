"""
Read-only grounding queries over NSE-sourced data.

These functions are the *grounded* alternative to web search: they return facts
straight from our own `financial_results` (full quarterly cost structure, margins,
segment_data) and `price_points` (daily OHLCV) tables. They are intentionally
side-effect-free and JSON-serializable so they can be exposed identically as
in-process dossier-agent tools and as MCP tools.

Connection follows the same async-SQLAlchemy-per-call pattern as
`bharat_terminal/research/db.py` (batch-like, low QPS).
"""
from __future__ import annotations

import logging
import math
import os
import statistics
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bharat_terminal.kb.models import (
    CorporateAction, FinancialResult, NseFiling, PricePoint, ShareholdingPattern,
)

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://bharat:bharat@postgres:5432/bharat_terminal",
)

_engine = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None

# Trading days per year — used to annualize daily-return volatility.
_TRADING_DAYS = 252

# Cost-structure / profitability columns surfaced to the agent for grounding
# input-cost vs output-price sensitivities. Mirrors financial_results schema.
_FIN_FIELDS = (
    "revenue_cr", "other_income_cr", "total_income_cr",
    "cost_of_materials_cr", "purchases_stock_in_trade_cr", "inventory_changes_cr",
    "employee_expense_cr", "finance_costs_cr", "depreciation_cr",
    "other_expenses_cr", "total_expenses_cr",
    "pbt_cr", "tax_cr", "pat_cr", "pat_attributable_cr",
    "ebitda_cr", "ebitda_margin_pct",
    "eps_basic", "eps_diluted",
    "debt_equity_ratio", "interest_coverage_ratio",
)


def _get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    sm = _get_sessionmaker()
    async with sm() as session:
        yield session  # read-only: no commit needed


# ── Financials ────────────────────────────────────────────────────────────

async def get_financials(
    symbol: str,
    periods: int = 8,
    consolidated: Optional[bool] = True,
) -> Dict[str, Any]:
    """Most recent `periods` quarterly results for `symbol`, newest first.

    All rows in `financial_results` are quarterly; trailing-twelve-month figures
    can be derived by summing the four most recent flow rows (revenue, pat, …).
    `consolidated=True` (default) prefers consolidated statements, but the
    preference is applied PER QUARTER: a quarter is returned as consolidated only
    if a consolidated filing exists for it; quarters filed standalone-only are
    returned as standalone rather than falling back to an older consolidated
    quarter. This guarantees the latest reported quarters are always included
    (companies that switch to standalone-only reporting are no longer stuck on a
    stale consolidated quarter).
    """
    symbol = (symbol or "").upper().strip()
    periods = max(1, min(int(periods), 24))

    def _base():
        # LEFT JOIN nse_filings so each row carries its source-filing provenance.
        return (
            select(FinancialResult, NseFiling.xbrl_url)
            .join(NseFiling, NseFiling.id == FinancialResult.nse_filing_id, isouter=True)
            .where(FinancialResult.symbol == symbol)
            .order_by(FinancialResult.period_end.desc())
        )

    async with _session() as s:
        # Fetch ALL quarters (both bases), newest first; choose the basis per
        # quarter below rather than filtering globally.
        all_rows = (await s.execute(_base())).all()

    if not all_rows:
        return {"error": f"No financial_results rows for {symbol}"}

    # Per quarter (period_end), keep the preferred reporting basis: when
    # `consolidated` is set, a row matching that basis replaces a non-matching one
    # for the same quarter — but a quarter that exists in ONLY the other basis is
    # kept as-is (no stale fallback to an older quarter). Then take the most
    # recent `periods` quarters.
    chosen: Dict[Any, Any] = {}
    for r, filing_url in all_rows:
        pe = r.period_end
        current = chosen.get(pe)
        if current is None:
            chosen[pe] = (r, filing_url)
        elif (
            consolidated is not None
            and r.consolidated == consolidated
            and current[0].consolidated != consolidated
        ):
            chosen[pe] = (r, filing_url)
    rows = [chosen[pe] for pe in sorted(chosen, reverse=True)[:periods]]

    out: List[Dict[str, Any]] = []
    for r, filing_url in rows:
        period: Dict[str, Any] = {
            "period_end": r.period_end.isoformat() if r.period_end else None,
            "period_type": r.period_type,
            # Accounting taxonomy this company files under. For banking / insurance,
            # the generic P&L columns below (cost_of_materials_cr, etc.) do not apply
            # — their real operating lines are in `sector_financials`, and revenue_cr
            # is interest earned (banks) / premium (insurers).
            "taxonomy": r.taxonomy,
            "consolidated": r.consolidated,
            "audited": r.audited,
            "segment_data": r.segment_data,
            # Sector-specific P&L lines (banks: interest_earned/interest_expended/
            # net_interest_income/provisions; insurers: premium/claims_incurred/
            # net_commission). NULL for general (Ind AS / NBFC / non-Ind AS) companies.
            "sector_financials": r.sector_financials,
            # Provenance: these figures are PARSED FROM NSE FILINGS into our own
            # database — they are authoritative and need no external lookup. Cite
            # them as source NSE_DB with this period_end + nse_filing_id. The
            # underlying NSE filing is recorded for audit only; do NOT re-fetch it.
            "provenance": {
                "source": "NSE_DB",
                "table": "financial_results",
                "nse_filing_id": r.nse_filing_id,
                "period_end": r.period_end.isoformat() if r.period_end else None,
                "underlying_nse_filing": filing_url,  # audit trail; not for re-fetch
            },
        }
        for f in _FIN_FIELDS:
            period[f] = getattr(r, f, None)
        out.append(period)

    # Basis may now be mixed across the returned quarters (per-quarter selection).
    all_cons = all(p["consolidated"] for p in out)
    any_cons = any(p["consolidated"] for p in out)
    basis = "consolidated" if all_cons else ("standalone" if not any_cons else "mixed")
    return {
        "symbol": symbol,
        "currency": "INR_crore",
        "basis": basis,
        "source": "NSE_DB",  # authoritative; cite our DB, never re-fetch externally
        "count": len(out),
        "periods": out,
    }


async def get_nbfc_metrics(symbol: str, periods: int = 8) -> Dict[str, Any]:
    """Lender-specific metrics for an NBFC, newest quarter first.

    For an NBFC the generic P&L columns understate the business: the figures that
    matter are the loan book, funding mix, net worth/leverage, net interest income
    and credit cost — which live in `sector_financials` (parsed from the SEBI
    integrated-filing balance sheet). This surfaces them, with a few derived ratios,
    so an agent doesn't have to reconstruct them. Returns an error if `symbol` files
    under a non-NBFC taxonomy (use get_financials for those).

    Per quarter: `lender_lines` (interest_earned, net_interest_income,
    impairment_on_financial_instruments, loans, investments, equity, total_assets,
    debt_securities/borrowings/deposits/subordinated_liabilities, total_borrowings,
    leverage_x) and `derived` (credit_cost_pct_qtr = impairment/loans, roa_pct_qtr =
    pat/total_assets, loans_yoy_pct). All amounts are ₹ crore; ratios are %.
    """
    symbol = (symbol or "").upper().strip()
    periods = max(1, min(int(periods), 24))

    async with _session() as s:
        rows = (
            await s.execute(
                select(FinancialResult)
                .where(
                    FinancialResult.symbol == symbol,
                    FinancialResult.taxonomy == "nbfc",
                )
                .order_by(FinancialResult.period_end.desc())
            )
        ).scalars().all()

    if not rows:
        return {
            "error": f"No NBFC financial_results for {symbol}. It may not be an "
            f"NBFC — use get_financials for banks/insurers/Ind-AS companies."
        }

    # Prefer consolidated per quarter (mirrors get_financials), then take `periods`.
    chosen: Dict[Any, FinancialResult] = {}
    for r in rows:
        cur = chosen.get(r.period_end)
        if cur is None or (r.consolidated and not cur.consolidated):
            chosen[r.period_end] = r
    selected = [chosen[pe] for pe in sorted(chosen, reverse=True)[:periods]]

    out: List[Dict[str, Any]] = []
    for r in selected:
        sf = dict(r.sector_financials or {})
        loans = sf.get("loans")
        impair = sf.get("impairment_on_financial_instruments")
        total_assets = sf.get("total_assets")
        derived: Dict[str, Any] = {}
        # Credit cost = impairment charge as % of the loan book (quarterly, not
        # annualised — multiply by ~4 for a rough annual run-rate).
        if loans and impair is not None:
            derived["credit_cost_pct_qtr"] = round(impair / loans * 100, 3)
        if r.pat_cr is not None and total_assets:
            derived["roa_pct_qtr"] = round(r.pat_cr / total_assets * 100, 3)
        out.append(
            {
                "period_end": r.period_end.isoformat() if r.period_end else None,
                "consolidated": r.consolidated,
                "revenue_cr": r.revenue_cr,
                "total_income_cr": r.total_income_cr,
                "pat_cr": r.pat_cr,
                "tax_cr": r.tax_cr,
                "lender_lines": sf,
                "derived": derived,
                "provenance": {
                    "source": "NSE_DB",
                    "table": "financial_results",
                    "nse_filing_id": r.nse_filing_id,
                    "period_end": r.period_end.isoformat() if r.period_end else None,
                },
            }
        )

    # Year-on-year loan growth: compare each quarter to the one ~4 quarters back.
    for i, period in enumerate(out):
        loans_now = period["lender_lines"].get("loans")
        if loans_now and i + 4 < len(out):
            loans_ago = out[i + 4]["lender_lines"].get("loans")
            if loans_ago:
                period["derived"]["loans_yoy_pct"] = round(
                    (loans_now - loans_ago) / loans_ago * 100, 2
                )

    return {
        "symbol": symbol,
        "currency": "INR_crore",
        "source": "NSE_DB",  # authoritative; cite our DB, never re-fetch externally
        "count": len(out),
        "periods": out,
    }


# ── Price returns / risk ────────────────────────────────────────────────────

def _pct(curr: float, prev: float) -> Optional[float]:
    if prev in (None, 0) or curr is None:
        return None
    return round((curr / prev - 1.0) * 100.0, 2)


async def get_price_returns(
    symbol: str,
    windows: Sequence[int] = (30, 90, 180, 365),
) -> Dict[str, Any]:
    """Trailing returns, annualized volatility and 1y max drawdown for `symbol`.

    Returns are measured over calendar windows (last close on/before
    latest_date - W days). Volatility is the stdev of daily simple returns over
    the last ~252 trading days, annualized. Beta-vs-index is intentionally
    omitted: `price_points` holds no index series (verified), so we do not
    fabricate one.
    """
    symbol = (symbol or "").upper().strip()

    async with _session() as s:
        rows = (await s.execute(
            select(PricePoint.date, PricePoint.close)
            .where(PricePoint.symbol == symbol)
            .order_by(PricePoint.date.asc())
        )).all()

    if not rows:
        return {"error": f"No price_points rows for {symbol}"}

    dates = [r[0] for r in rows]
    closes = [float(r[1]) for r in rows if r[1] is not None]
    if len(closes) < 2:
        return {"error": f"Insufficient price history for {symbol}"}

    latest_date, latest_close = dates[-1], closes[-1]

    # Trailing returns per calendar window.
    returns: Dict[str, Optional[float]] = {}
    for w in windows:
        cutoff = latest_date - timedelta(days=int(w))
        prev_close = None
        for d, c in zip(dates, closes):  # earliest → latest; keep last on/before cutoff
            if d <= cutoff:
                prev_close = c
            else:
                break
        returns[f"{w}d"] = _pct(latest_close, prev_close) if prev_close else None

    # Annualized volatility over the last ~252 trading days.
    recent = closes[-(_TRADING_DAYS + 1):]
    daily_rets = [
        (recent[i] / recent[i - 1] - 1.0)
        for i in range(1, len(recent))
        if recent[i - 1]
    ]
    vol_annual_pct = (
        round(statistics.pstdev(daily_rets) * math.sqrt(_TRADING_DAYS) * 100.0, 2)
        if len(daily_rets) >= 2 else None
    )

    # Max drawdown over the last ~252 trading days.
    peak = -math.inf
    max_dd = 0.0
    for c in recent:
        peak = max(peak, c)
        if peak > 0:
            max_dd = min(max_dd, c / peak - 1.0)
    max_drawdown_pct = round(max_dd * 100.0, 2)

    return {
        "symbol": symbol,
        "as_of": latest_date.isoformat(),
        "latest_close": round(latest_close, 2),
        "trading_days": len(closes),
        "trailing_returns_pct": returns,
        "volatility_annualized_pct": vol_annual_pct,
        "max_drawdown_1y_pct": max_drawdown_pct,
        "beta_vs_index": None,  # no index series in price_points
    }


async def get_forward_returns(
    symbol: str,
    from_date: date,
    horizons: Sequence[int] = (1, 5, 10, 20),
) -> Dict[str, Any]:
    """Realized forward returns (percent) for `symbol` measured from the first
    tradeable close on/after `from_date`, over each horizon in TRADING days.

    Used by the signal-accuracy job (Feature 2). Look-ahead-safe:
      - `entry` = first `price_points` close with date >= from_date.
      - horizon H return = close[entry_index + H] / entry_close - 1; if there are
        not yet H rows after entry, that horizon returns None (NOT 0) — the signal
        is simply too recent to evaluate at that horizon.
      - If no close exists on/after from_date at all, returns status 'no_entry'.
    """
    symbol = (symbol or "").upper().strip()

    async with _session() as s:
        rows = (await s.execute(
            select(PricePoint.date, PricePoint.close)
            .where(PricePoint.symbol == symbol)
            .order_by(PricePoint.date.asc())
        )).all()

    series = [(r[0], float(r[1])) for r in rows if r[1] is not None]
    if not series:
        return {"symbol": symbol, "status": "no_price_data", "returns": {}}

    # First close on/after from_date is the entry (the first tradeable price).
    entry_idx = next((i for i, (d, _) in enumerate(series) if d >= from_date), None)
    if entry_idx is None:
        # from_date is after our latest price — nothing tradeable yet.
        return {"symbol": symbol, "status": "no_entry", "returns": {}}

    entry_date, entry_close = series[entry_idx]
    returns: Dict[str, Optional[float]] = {}
    elapsed_all = True
    for h in horizons:
        tgt = entry_idx + int(h)
        if tgt < len(series):
            returns[f"{h}d"] = _pct(series[tgt][1], entry_close)
        else:
            returns[f"{h}d"] = None  # horizon not yet elapsed
            elapsed_all = False

    return {
        "symbol": symbol,
        "from_date": from_date.isoformat(),
        "entry_date": entry_date.isoformat(),
        "entry_price": round(entry_close, 2),
        "returns": returns,
        "all_horizons_elapsed": elapsed_all,
        "status": "ok",
    }


# ── Shareholding pattern ────────────────────────────────────────────────────

async def get_shareholding(symbol: str, quarters: int = 4) -> Dict[str, Any]:
    """Quarterly shareholding pattern for `symbol`, newest first.

    From our `shareholding_patterns` table, parsed from the authoritative NSE
    shareholding XBRL: promoter / FII / DII / public %, promoter pledge %, total
    shareholder count. AUTHORITATIVE — prefer over web for any holding figure.
    A NULL promoter_pct means the company has no promoter (e.g. HDFCBANK, ITC),
    not missing data.
    """
    symbol = (symbol or "").upper().strip()
    quarters = max(1, min(int(quarters), 12))
    async with _session() as s:
        rows = (await s.execute(
            select(ShareholdingPattern)
            .where(ShareholdingPattern.symbol == symbol)
            .order_by(ShareholdingPattern.quarter_end.desc())
            .limit(quarters)
        )).scalars().all()
    if not rows:
        return {"error": f"No shareholding_patterns rows for {symbol}"}
    return {
        "symbol": symbol,
        "source": "NSE_DB",  # cite our DB (parsed from NSE shareholding XBRL)
        "count": len(rows),
        "quarters": [{
            "quarter_end": r.quarter_end.isoformat() if r.quarter_end else None,
            "promoter_pct": r.promoter_pct,
            "promoter_pledge_pct": r.promoter_pledge_pct,
            "fii_pct": r.fii_pct,
            "dii_pct": r.dii_pct,
            "public_pct": r.public_pct,
            "num_shareholders": r.num_shareholders,
        } for r in rows],
    }


# ── Corporate actions ───────────────────────────────────────────────────────

async def get_corporate_actions(
    symbol: str, limit: int = 20, action_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Recent corporate actions for `symbol`, newest ex_date first.

    From our `corporate_actions` table (NSE-sourced): dividends (with ₹/share
    `value`), bonuses, splits, buybacks, rights, restructurings. AUTHORITATIVE —
    prefer over web for dividend/buyback/bonus history. Optional `action_type`
    filter (DIVIDEND/BUYBACK/BONUS/SPLIT/RIGHTS/RESTRUCTURE).
    """
    symbol = (symbol or "").upper().strip()
    limit = max(1, min(int(limit), 100))
    async with _session() as s:
        stmt = (
            select(CorporateAction)
            .where(CorporateAction.symbol == symbol)
            .order_by(CorporateAction.ex_date.desc())
        )
        if action_type:
            stmt = stmt.where(CorporateAction.action_type == action_type.upper().strip())
        rows = (await s.execute(stmt.limit(limit))).scalars().all()
    if not rows:
        return {"error": f"No corporate_actions rows for {symbol}"}
    return {
        "symbol": symbol,
        "source": "NSE_DB",
        "count": len(rows),
        "actions": [{
            "ex_date": r.ex_date.isoformat() if r.ex_date else None,
            "record_date": r.record_date.isoformat() if r.record_date else None,
            "action_type": r.action_type,
            "value": r.value,
            "subject": (r.details or {}).get("subject") if isinstance(r.details, dict) else None,
        } for r in rows],
    }


async def search_transcript_chunks(
    query: str,
    symbol: Optional[str] = None,
    k: int = 8,
) -> Dict[str, Any]:
    """Semantic search over concall-transcript chunks (A1 phase 3.2).

    Embeds `query` (RETRIEVAL_QUERY) and cosine-searches the HNSW index.
    Every result's `excerpt` is a verbatim slice of the filed transcript
    (chunks are char spans of full_text — nothing generated). Shared by the
    /transcripts/search API and, later, the research-chat tool loop.
    """
    from sqlalchemy import text as sql_text
    from bharat_terminal.llm.embeddings import embed_query

    qvec = await embed_query(query)
    if qvec is None:
        return {"query": query, "results": []}
    params: Dict[str, Any] = {
        "q": "[" + ",".join(f"{x:.6f}" for x in qvec) + "]",
        "k": max(1, min(int(k), 50)),
    }
    sym_clause = ""
    if symbol:
        sym_clause = "AND tc.symbol = :sym"
        params["sym"] = symbol.upper()
    async with _session() as s:
        rows = (await s.execute(sql_text(f"""
            SELECT tc.transcript_id, tc.symbol, tc.announced_at,
                   tc.char_start, tc.char_end, tc.section,
                   (1 - (tc.embedding <=> (:q)::vector)) AS relevance,
                   substring(ct.full_text FROM tc.char_start + 1
                             FOR tc.char_end - tc.char_start) AS excerpt,
                   c.company_name
            FROM transcript_chunks tc
            JOIN concall_transcripts ct ON ct.id = tc.transcript_id
            LEFT JOIN companies c ON c.symbol = tc.symbol
            WHERE ct.parse_status = 'ok' {sym_clause}
            ORDER BY tc.embedding <=> (:q)::vector
            LIMIT :k
        """), params)).all()
    return {
        "query": query,
        "results": [{
            "transcript_id": r.transcript_id,
            "symbol": r.symbol,
            "company_name": r.company_name,
            "announced_at": r.announced_at.isoformat(),
            "char_start": r.char_start,
            "char_end": r.char_end,
            "section": r.section,
            "relevance": round(float(r.relevance), 4),
            "excerpt": r.excerpt,
        } for r in rows],
    }


async def get_recent_impacts(symbol: str, days: int = 30,
                             limit: int = 15) -> Dict[str, Any]:
    """Recent analysed events touching `symbol` (direct or propagated) with
    the pipeline's per-company assessment. The evidence base for
    'why did it move' questions."""
    from sqlalchemy import text as sql_text

    async with _session() as s:
        rows = (await s.execute(sql_text("""
            SELECT ir.id, ir.created_at, ni.headline, ni.source, ni.url,
                   ci->>'sentiment' AS sentiment,
                   (ci->>'magnitude')::int AS magnitude,
                   (ci->>'hop_distance')::int AS hop_distance,
                   ci->>'explanation' AS explanation
            FROM impact_reports ir
            JOIN news_items ni ON ni.id = ir.news_id
            CROSS JOIN LATERAL jsonb_array_elements(ir.company_impacts) ci
            WHERE ir.relevant
              AND ir.created_at >= now() - make_interval(days => :days)
              AND upper(ci->>'symbol') = :sym
            ORDER BY ir.created_at DESC
            LIMIT :lim
        """), {"sym": symbol.upper(), "days": int(days),
               "lim": max(1, min(int(limit), 50))})).all()
    return {
        "symbol": symbol.upper(),
        "window_days": days,
        "events": [{
            "impact_report_id": r.id,
            "at": r.created_at.isoformat(),
            "headline": r.headline,
            "source": r.source,
            "url": r.url,
            "sentiment": r.sentiment,
            "magnitude": r.magnitude,
            "hop_distance": r.hop_distance,
            "assessment": r.explanation,
        } for r in rows],
    }


async def get_guidance_timeline(symbol: str, limit: int = 40) -> Dict[str, Any]:
    """Management guidance extracted from the company's concalls (values AS
    STATED on the call, verbatim-anchored) with computed QoQ change verdicts."""
    from sqlalchemy import text as sql_text

    async with _session() as s:
        rows = (await s.execute(sql_text("""
            SELECT g.transcript_id, g.announced_at, g.metric, g.period,
                   g.value_low, g.value_high, g.unit, g.change_verdict,
                   substring(ct.full_text FROM g.quote_start + 1
                             FOR g.quote_end - g.quote_start) AS quote
            FROM transcript_guidance g
            JOIN concall_transcripts ct ON ct.id = g.transcript_id
            WHERE g.symbol = :sym
            ORDER BY g.announced_at DESC, g.quote_start
            LIMIT :lim
        """), {"sym": symbol.upper(),
               "lim": max(1, min(int(limit), 100))})).all()
    return {
        "symbol": symbol.upper(),
        "items": [{
            "transcript_id": r.transcript_id,
            "call_date": r.announced_at.date().isoformat(),
            "metric": r.metric,
            "period": r.period,
            "value_low": r.value_low,
            "value_high": r.value_high,
            "unit": r.unit,
            "vs_previous_call": r.change_verdict,
            "verbatim": r.quote,
        } for r in rows],
    }
