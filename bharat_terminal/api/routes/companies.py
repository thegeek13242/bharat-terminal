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

        rows = (await s.execute(
            text(f"""
                SELECT c.symbol,
                       c.company_name,
                       c.sector_nse AS sector,
                       c.mcap_cr,
                       c.is_active,
                       (SELECT MAX(fr.period_end) FROM financial_results fr WHERE fr.symbol = c.symbol) AS latest_quarter,
                       (SELECT COUNT(*) FROM financial_results fr WHERE fr.symbol = c.symbol) AS quarters_loaded,
                       (SELECT d.generated_at FROM company_dossiers d WHERE d.symbol = c.symbol) AS dossier_at,
                       (SELECT d.data_quality FROM company_dossiers d WHERE d.symbol = c.symbol) AS dossier_quality
                FROM companies c
                WHERE {where_clause}
                ORDER BY c.mcap_cr DESC NULLS LAST, c.symbol
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
                    c.industry, c.mcap_cr, c.market_cap_tier,
                    c.exchange, c.is_active,
                    c.revenue_ttm_cr, c.pat_ttm_cr, c.ebitda_margin_pct,
                    c.pe_ratio, c.pb_ratio, c.roe_pct, c.roce_pct,
                    c.dividend_yield_pct, c.book_value, c.eps_ttm,
                    c.face_value, c.last_price, c.fifty_two_week_high,
                    c.fifty_two_week_low, c.dcf_fair_value, c.dcf_margin_of_safety_pct,
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
