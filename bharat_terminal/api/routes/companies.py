"""
Companies browsing endpoints.

Direct DB queries against:
  - companies, financial_results, price_points, dossier_linkages

Endpoints:
  GET /companies?q=&sector=&limit=&offset=        — paginated search
  GET /companies/{symbol}                          — single row
  GET /companies/{symbol}/financials               — quarterly history
  GET /companies/{symbol}/prices?days=             — OHLCV history
  GET /companies/{symbol}/linkages                  — outbound + inbound edges from dossier_linkages
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory
from bharat_terminal.kb.marquee import resolve_investor

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("")
async def list_companies(
    q: Optional[str] = Query(None, description="Search by symbol or company_name prefix"),
    sector: Optional[str] = Query(None),
    is_active: bool = Query(True),
    has_dossier: Optional[bool] = Query(None, description="If true, only companies with a dossier"),
    has_financials: Optional[bool] = Query(None),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
):
    """Paginated company list with optional search + filters."""
    factory = get_session_factory()
    async with factory() as s:
        where = ["c.is_active = :is_active"]
        params: dict = {"is_active": is_active, "limit": limit, "offset": offset}
        if q:
            where.append("(c.symbol ILIKE :q OR c.company_name ILIKE :q)")
            params["q"] = f"%{q}%"
        if sector:
            where.append("c.sector_nse = :sector")
            params["sector"] = sector
        if has_dossier is True:
            where.append("EXISTS (SELECT 1 FROM company_dossiers d WHERE d.symbol = c.symbol)")
        elif has_dossier is False:
            where.append("NOT EXISTS (SELECT 1 FROM company_dossiers d WHERE d.symbol = c.symbol)")
        if has_financials is True:
            where.append("EXISTS (SELECT 1 FROM financial_results fr WHERE fr.symbol = c.symbol)")

        where_clause = " AND ".join(where) if where else "TRUE"

        total = (await s.execute(
            text(f"SELECT COUNT(*) FROM companies c WHERE {where_clause}"),
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        )).scalar_one()

        # Analyst-first columns backed by data we can TRUST:
        #  - ltp / chg_1m_pct from price_points (market data, reliable).
        #  - pe computed live = ltp / eps_ttm, guarded to a sane band (0, 500].
        #  - mcap_cr and revenue_ttm_cr are systematically scale-corrupted in source
        #    (small/mid-caps show lakh-crore values overlapping the real large-caps),
        #    so they are NOT returned — a wrong number is worse than none. Reinstate
        #    once the fundamentals scale bug is fixed at ingestion.
        #  - pb_ratio / roe_pct are 100% NULL in source (no equity/book value).
        #  Default order surfaces names we have a dossier for, then alphabetical.
        rows = (await s.execute(
            text(f"""
                SELECT c.symbol,
                       c.company_name,
                       c.sector_nse AS sector,
                       px.ltp,
                       CASE WHEN px.close_1m > 0
                            THEN round((((px.ltp - px.close_1m) / px.close_1m) * 100)::numeric, 2)
                       END AS chg_1m_pct,
                       CASE WHEN c.eps_ttm > 0 AND px.ltp IS NOT NULL AND (px.ltp / c.eps_ttm) <= 500
                            THEN round((px.ltp / c.eps_ttm)::numeric, 1)
                       END AS pe_ratio,
                       c.is_active,
                       (SELECT MAX(fr.period_end) FROM financial_results fr WHERE fr.symbol = c.symbol) AS latest_quarter,
                       (SELECT COUNT(*) FROM financial_results fr WHERE fr.symbol = c.symbol) AS quarters_loaded,
                       (SELECT d.generated_at FROM company_dossiers d WHERE d.symbol = c.symbol) AS dossier_at,
                       (SELECT d.data_quality FROM company_dossiers d WHERE d.symbol = c.symbol) AS dossier_quality
                FROM companies c
                LEFT JOIN LATERAL (
                    SELECT
                      (SELECT pp.close FROM price_points pp WHERE pp.symbol = c.symbol
                         ORDER BY pp.date DESC LIMIT 1) AS ltp,
                      (SELECT pp.close FROM price_points pp WHERE pp.symbol = c.symbol
                         AND pp.date <= now() - interval '30 days'
                         ORDER BY pp.date DESC LIMIT 1) AS close_1m
                ) px ON true
                WHERE {where_clause}
                ORDER BY EXISTS (SELECT 1 FROM company_dossiers d WHERE d.symbol = c.symbol) DESC, c.symbol
                LIMIT :limit OFFSET :offset
            """),
            params,
        )).mappings().all()

    return {"total": total, "limit": limit, "offset": offset, "items": [dict(r) for r in rows]}


@router.get("/{symbol}")
async def get_company(symbol: str):
    """Full single-company record."""
    factory = get_session_factory()
    async with factory() as s:
        row = (await s.execute(
            text("""
                SELECT
                    c.symbol, c.company_name, c.isin, c.sector_nse AS sector,
                    c.industry_nse AS industry, c.mcap_cr,
                    c.exchange, c.is_active, c.description_200w, c.moat_classification,
                    c.revenue_ttm_cr, c.pat_cr, c.ebitda_margin_pct,
                    c.pe_ratio, c.pb_ratio, c.roe_pct, c.eps_ttm,
                    c.net_debt_cr, c.interest_coverage, c.fcf_yield_pct,
                    c.fair_value_per_share, c.margin_of_safety_pct,
                    c.dcf_bull_value, c.dcf_bear_value, c.dcf_confidence,
                    c.median_target_price, c.num_analysts,
                    c.buy_pct, c.hold_pct, c.sell_pct,
                    c.revenue_segments, c.geography_split,
                    c.created_at, c.updated_at,
                    (SELECT MAX(period_end) FROM financial_results WHERE symbol = c.symbol) AS latest_quarter,
                    (SELECT COUNT(*) FROM financial_results WHERE symbol = c.symbol) AS quarters_loaded,
                    (SELECT MAX(date) FROM price_points WHERE symbol = c.symbol) AS latest_price_date,
                    EXISTS (SELECT 1 FROM company_dossiers WHERE symbol = c.symbol) AS has_dossier
                FROM companies c
                WHERE c.symbol = :symbol
            """),
            {"symbol": symbol.upper()},
        )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol}")
    return dict(row)


@router.get("/{symbol}/financials")
async def get_financials(
    symbol: str,
    period: str = Query("quarterly", regex="^(quarterly|annual|all)$"),
    consolidated: Optional[bool] = Query(None),
    limit: int = Query(40, le=200),
):
    """Quarterly / annual financials from XBRL-parsed financial_results."""
    factory = get_session_factory()
    async with factory() as s:
        where = ["symbol = :symbol"]
        params: dict = {"symbol": symbol.upper(), "limit": limit}
        if period != "all":
            where.append("period_type = :period")
            params["period"] = period
        if consolidated is not None:
            where.append("consolidated = :consolidated")
            params["consolidated"] = consolidated

        rows = (await s.execute(
            text(f"""
                SELECT id, symbol, period_end, period_type, consolidated,
                       revenue_cr, other_income_cr,
                       cost_of_materials_cr, employee_expense_cr, finance_costs_cr,
                       depreciation_cr, other_expenses_cr, total_expenses_cr,
                       pbt_cr, tax_cr, pat_cr, pat_attributable_cr,
                       eps_basic, eps_diluted,
                       ebitda_cr, ebitda_margin_pct,
                       segment_data,
                       source
                FROM financial_results
                WHERE {' AND '.join(where)}
                ORDER BY period_end DESC, consolidated DESC
                LIMIT :limit
            """),
            params,
        )).mappings().all()

    if not rows:
        raise HTTPException(status_code=404, detail=f"No financials for {symbol}")
    return {"symbol": symbol.upper(), "count": len(rows), "items": [dict(r) for r in rows]}


@router.get("/{symbol}/prices")
async def get_prices(
    symbol: str,
    days: int = Query(365, le=2000),
):
    """OHLCV time-series from bhavcopy."""
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(
            text("""
                SELECT date, open, high, low, close, volume
                FROM price_points
                WHERE symbol = :symbol
                  AND date >= (CURRENT_DATE - :days * INTERVAL '1 day')
                ORDER BY date
            """),
            {"symbol": symbol.upper(), "days": days},
        )).mappings().all()

    return {"symbol": symbol.upper(), "count": len(rows), "items": [dict(r) for r in rows]}


@router.get("/{symbol}/linkages")
async def get_linkages(symbol: str):
    """Outbound + inbound dossier linkages — the 2nd-degree network for this company."""
    factory = get_session_factory()
    async with factory() as s:
        outbound = (await s.execute(
            text("""
                SELECT target_symbol AS other, kind, strength, direction,
                       propagation_lag_days, rationale
                FROM dossier_linkages
                WHERE source_symbol = :symbol
                ORDER BY strength DESC
            """),
            {"symbol": symbol.upper()},
        )).mappings().all()

        inbound = (await s.execute(
            text("""
                SELECT source_symbol AS other, kind, strength, direction,
                       propagation_lag_days, rationale
                FROM dossier_linkages
                WHERE target_symbol = :symbol
                ORDER BY strength DESC
            """),
            {"symbol": symbol.upper()},
        )).mappings().all()

    return {
        "symbol": symbol.upper(),
        "outbound": [dict(r) for r in outbound],
        "inbound":  [dict(r) for r in inbound],
    }


async def _stock_recent_deals(s, sym: str) -> list:
    """Recent informed (non-excluded) bulk/block deals for a stock."""
    rows = (await s.execute(
        text("""
            SELECT deal_date, exchange, deal_type, client_name,
                   client_name_norm, marquee_key, side, quantity, value_cr
            FROM market_deals
            WHERE symbol = :symbol AND is_excluded = false
            ORDER BY deal_date DESC, value_cr DESC NULLS LAST
            LIMIT 20
        """),
        {"symbol": sym},
    )).mappings().all()
    return [{
        "deal_date": d["deal_date"].isoformat(),
        "exchange": d["exchange"], "deal_type": d["deal_type"],
        "client_name": d["client_name"],
        "investor_id": (f"marquee:{d['marquee_key']}" if d["marquee_key"]
                        else f"norm:{d['client_name_norm']}"),
        "marquee": (resolve_investor(d["client_name"]) or {}).get("display") if d["marquee_key"] else None,
        "side": d["side"], "quantity": d["quantity"], "value_cr": d["value_cr"],
    } for d in rows]


@router.get("/{symbol}/holders")
async def get_holders(symbol: str):
    """Named (>=1%) holders for the latest disclosed quarter with QoQ change.

    Full-joins the latest quarter against the prior one so new positions, exits,
    adds and trims all surface. Curated marquee holders are labelled + flagged.
    ₹ value = shares_held x latest close (NULL when shares undisclosed in XBRL).
    """
    sym = symbol.upper()
    factory = get_session_factory()
    async with factory() as s:
        quarters = (await s.execute(
            text("""SELECT DISTINCT quarter_end FROM investor_holdings
                    WHERE symbol = :symbol ORDER BY quarter_end DESC LIMIT 2"""),
            {"symbol": sym},
        )).scalars().all()
        if not quarters:
            return {"symbol": sym, "quarter_end": None, "prior_quarter_end": None,
                    "holders": [], "recent_deals": await _stock_recent_deals(s, sym)}
        latest = quarters[0]
        prior = quarters[1] if len(quarters) > 1 else None

        rows = (await s.execute(
            text("""
                WITH cur AS (
                    SELECT holder_name, holder_name_norm, category, holding_pct, shares_held
                    FROM investor_holdings WHERE symbol = :symbol AND quarter_end = :latest
                ),
                prv AS (
                    SELECT holder_name, holding_pct AS prev_pct, shares_held AS prev_shares
                    FROM investor_holdings WHERE symbol = :symbol AND quarter_end = :prior
                ),
                px AS (
                    SELECT close FROM price_points WHERE symbol = :symbol
                    ORDER BY date DESC LIMIT 1
                )
                SELECT
                    COALESCE(cur.holder_name, prv.holder_name) AS holder_name,
                    cur.holder_name_norm, cur.category,
                    cur.holding_pct, cur.shares_held,
                    prv.prev_pct, prv.prev_shares,
                    (SELECT close FROM px) AS ltp,
                    (SELECT mcap_cr FROM companies WHERE symbol = :symbol) AS mcap_cr
                FROM cur
                FULL OUTER JOIN prv ON cur.holder_name = prv.holder_name
                ORDER BY COALESCE(cur.holding_pct, 0) DESC, COALESCE(prv.prev_pct, 0) DESC
            """),
            {"symbol": sym, "latest": latest, "prior": prior},
        )).mappings().all()

    holders = []
    for r in rows:
        pct, prev = r["holding_pct"], r["prev_pct"]
        if pct is None:
            change = "exit"
            delta = None if prev is None else round(-prev, 2)
        elif prev is None:
            change = "new"
            delta = None
        else:
            delta = round(pct - prev, 2)
            change = "add" if delta > 0.01 else "trim" if delta < -0.01 else "hold"
        shares, ltp, mcap = r["shares_held"], r["ltp"], r["mcap_cr"]
        # Prefer disclosed shares × live price; fall back to pct × market cap
        # (mathematically the same stake value) when shares are undisclosed.
        if shares and ltp:
            value_cr = round(float(shares) * float(ltp) / 1e7, 2)
        elif pct is not None and mcap:
            value_cr = round(pct / 100.0 * float(mcap), 2)
        else:
            value_cr = None
        m = resolve_investor(r["holder_name"])
        holders.append({
            "holder_name": r["holder_name"],
            "holder_name_norm": r["holder_name_norm"],
            "category": r["category"],
            "holding_pct": pct,
            "prev_pct": prev,
            "delta_pct": delta,
            "change": change,
            "shares_held": shares,
            "value_cr": value_cr,
            "marquee": m["display"] if m else None,
            "marquee_key": m["key"] if m else None,
            "marquee_type": m["type"] if m else None,
        })

    # Recent bulk/block deals for this stock (informed flows only) — the faster,
    # intra-quarter smart-money signal alongside the quarterly holders snapshot.
    async with factory() as s:
        recent_deals = await _stock_recent_deals(s, sym)

    return {
        "symbol": sym,
        "quarter_end": latest.isoformat(),
        "prior_quarter_end": prior.isoformat() if prior else None,
        "holders": holders,
        "recent_deals": recent_deals,
    }
