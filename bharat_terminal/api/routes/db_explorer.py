"""
Database explorer — read-only tabular access to every major table.

A SAFE-LISTED set of tables can be browsed via /db/{table}. We do not
accept user-supplied SQL or filter columns — the safe-list maps each
table to a fixed query template with paging.

Endpoints (all paginated, max 500 per page):
  GET /db/tables                             — list of browseable tables w/ row counts
  GET /db/{table}                            — rows from table, paginated
  GET /db/sync-log                           — sync history (specialised)
  GET /db/result-events                      — earnings calendar
  GET /db/news-items                         — news_items (alias for /db/news_items)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/db", tags=["db-explorer"])


# Map table → (columns to expose, default sort, search column or None)
TABLE_REGISTRY = {
    "companies": {
        "columns": [
            "symbol", "company_name", "isin", "sector_nse", "industry",
            "mcap_cr", "is_active", "last_price", "fifty_two_week_high",
            "fifty_two_week_low", "pe_ratio", "pb_ratio", "roe_pct",
            "dividend_yield_pct", "updated_at",
        ],
        "order_by": "mcap_cr DESC NULLS LAST",
        "search_col": "company_name",
    },
    "financial_results": {
        "columns": [
            "id", "symbol", "period_end", "period_type", "consolidated",
            "revenue_cr", "ebitda_cr", "ebitda_margin_pct",
            "pat_cr", "eps_basic", "eps_diluted",
            "source", "created_at",
        ],
        "order_by": "period_end DESC, symbol",
        "search_col": "symbol",
    },
    "price_points": {
        "columns": ["symbol", "date", "open", "high", "low", "close", "volume"],
        "order_by": "date DESC, symbol",
        "search_col": "symbol",
    },
    "news_items": {
        "columns": [
            "id", "source", "timestamp_utc", "headline", "url",
            "category", "ingest_latency_ms",
        ],
        "order_by": "timestamp_utc DESC",
        "search_col": "headline",
    },
    "impact_reports": {
        "columns": [
            "id", "news_id", "relevant", "confidence", "macro_theme",
            "processing_latency_ms", "created_at",
        ],
        "order_by": "created_at DESC",
        "search_col": "macro_theme",
    },
    "nse_filings": {
        "columns": [
            "id", "symbol", "filing_date", "from_date", "to_date",
            "consolidated", "audited", "ind_as", "xbrl_parsed",
            "broadcast_date",
        ],
        "order_by": "filing_date DESC",
        "search_col": "symbol",
    },
    "result_events": {
        "columns": [
            "id", "symbol", "result_date", "description",
            "sync_status", "synced_at", "created_at",
        ],
        "order_by": "result_date DESC",
        "search_col": "symbol",
    },
    "sync_log": {
        "columns": [
            "id", "sync_type", "source", "started_at", "completed_at",
            "status", "records_processed", "records_failed", "error_message",
        ],
        "order_by": "started_at DESC",
        "search_col": None,
    },
    "triggers": {
        "columns": ["id", "label", "kind", "unit", "data_source", "created_at"],
        "order_by": "id",
        "search_col": "label",
    },
    "macro_events": {
        "columns": ["id", "label", "trigger_id", "trigger_spec", "notes", "created_at"],
        "order_by": "id",
        "search_col": "label",
    },
    "dossier_sensitivities": {
        "columns": [
            "id", "symbol", "trigger_id", "beta", "channel",
            "lag_days", "confidence", "rationale", "generated_at",
        ],
        "order_by": "generated_at DESC",
        "search_col": "symbol",
    },
    "dossier_linkages": {
        "columns": [
            "id", "source_symbol", "target_symbol", "kind",
            "strength", "direction", "propagation_lag_days",
            "rationale", "generated_at",
        ],
        "order_by": "strength DESC",
        "search_col": "source_symbol",
    },
    "trigger_betas": {
        "columns": [
            "id", "symbol", "trigger_id", "beta", "beta_low", "beta_high",
            "confidence", "derivation_method", "model_used",
            "calibrated_at",
        ],
        "order_by": "calibrated_at DESC",
        "search_col": "symbol",
    },
    "fundamental_exposures": {
        "columns": [
            "id", "symbol", "trigger_id", "exposure_kind", "pct_of_total",
            "total_basis", "mechanism", "model_used", "extracted_at",
        ],
        "order_by": "extracted_at DESC",
        "search_col": "symbol",
    },
    "shareholding_patterns": {
        "columns": [
            "id", "symbol", "period_end",
            "promoter_holding_pct", "fii_holding_pct", "dii_holding_pct",
            "public_holding_pct", "created_at",
        ],
        "order_by": "period_end DESC",
        "search_col": "symbol",
    },
    "corporate_actions": {
        "columns": [
            "id", "symbol", "action_type", "record_date", "ex_date",
            "ratio_or_amount", "description", "created_at",
        ],
        "order_by": "ex_date DESC NULLS LAST",
        "search_col": "symbol",
    },
}


@router.get("/tables")
async def list_tables():
    """All browseable tables + live row counts."""
    factory = get_session_factory()
    async with factory() as s:
        result = []
        for table in TABLE_REGISTRY:
            try:
                cnt = (await s.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()
            except Exception as e:
                logger.debug(f"row count for {table} failed: {e}")
                cnt = None
            result.append({
                "table": table,
                "rows": cnt,
                "columns": TABLE_REGISTRY[table]["columns"],
            })
    return {"tables": result}


@router.get("/{table}")
async def get_table_rows(
    table: str,
    q: Optional[str] = Query(None, description="Substring search on the table's primary search column"),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
):
    """Paginated rows from a safe-listed table."""
    reg = TABLE_REGISTRY.get(table)
    if not reg:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown or non-browseable table: {table}. "
                   f"Allowed: {sorted(TABLE_REGISTRY)}",
        )

    cols = ", ".join(reg["columns"])
    where = "TRUE"
    params: dict = {"limit": limit, "offset": offset}
    if q and reg["search_col"]:
        where = f"{reg['search_col']} ILIKE :q"
        params["q"] = f"%{q}%"

    factory = get_session_factory()
    async with factory() as s:
        total = (await s.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {where}"),
            {k: v for k, v in params.items() if k not in ("limit", "offset")},
        )).scalar_one()

        rows = (await s.execute(
            text(f"""
                SELECT {cols} FROM {table}
                WHERE {where}
                ORDER BY {reg['order_by']}
                LIMIT :limit OFFSET :offset
            """),
            params,
        )).mappings().all()

    return {
        "table": table,
        "total": total,
        "limit": limit,
        "offset": offset,
        "columns": reg["columns"],
        "rows": [dict(r) for r in rows],
    }
