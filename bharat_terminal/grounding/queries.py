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
from datetime import timedelta
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
    `consolidated=True` (default) prefers consolidated statements, falling back
    to whatever exists if the company files only standalone.
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
        stmt = _base()
        if consolidated is not None:
            stmt = stmt.where(FinancialResult.consolidated == consolidated)
        rows = (await s.execute(stmt.limit(periods))).all()

        # Fall back to the other basis if the preferred one has no rows.
        if not rows and consolidated is not None:
            rows = (await s.execute(_base().limit(periods))).all()

    if not rows:
        return {"error": f"No financial_results rows for {symbol}"}

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

    return {
        "symbol": symbol,
        "currency": "INR_crore",
        "basis": "consolidated" if (consolidated and out[0]["consolidated"]) else "as_filed",
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
