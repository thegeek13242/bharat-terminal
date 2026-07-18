"""Impact report REST endpoints.

Read strategy:
  1. Redis  `impact:{news_id}`   (sub-millisecond, 24h TTL)
  2. PostgreSQL  `impact_reports` (permanent fallback)

/impact/feed returns the most recent N ImpactReports with embedded news_item,
used by the frontend to hydrate on page load.
"""
import json
import logging
import os
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/impact", tags=["impact"])

# Keys that exist in storage (DB/Redis) but must never leave the API: trade
# signals are internal eval telemetry, not customer content (SEBI RA scope).
_INTERNAL_KEYS = ("trade_signals",)


def _strip_internal(report: dict) -> dict:
    for k in _INTERNAL_KEYS:
        report.pop(k, None)
    return report

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_redis = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


@router.get("/feed")
async def get_impact_feed(
    limit: int = Query(default=50, le=200),
    relevant_only: bool = Query(default=True),
    before: str | None = Query(
        default=None,
        description="ISO8601 timestamp; return only reports with created_at strictly "
        "before this value (keyset pagination for loading older events).",
    ),
    symbols: str | None = Query(
        default=None,
        description="Comma-separated NSE/BSE symbols; return only reports whose "
        "company_impacts mention at least one of them (any hop distance / "
        "magnitude — materiality filtering stays client-side where the "
        "per-symbol thresholds live). Lets a small watchlist reach past the "
        "global recency window instead of intersecting with it.",
    ),
):
    """
    Return the most recent ImpactReports with embedded news_item.
    Used by the frontend to hydrate on page load / refresh and to page back
    through older events via the `before` cursor.
    Reads from PostgreSQL (authoritative) and falls back to Redis scan.

    NOTE: the SELECT deliberately omits impact_reports.trade_signals — that
    column is internal eval telemetry and must never reach a customer surface
    (SEBI RA scope). Do not add it here.
    """
    # Parse the optional cursor up-front so a malformed value is a clean 400
    # rather than a 500 buried in the DB layer.
    before_dt = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            raise HTTPException(
                status_code=422, detail=f"Invalid `before` timestamp: {before!r}"
            )

    symbol_list: list[str] = []
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:50]

    # ── 1. PostgreSQL (authoritative, ordered) ─────────────────────────────
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            relevant_filter = "AND ir.relevant = true" if relevant_only else ""
            before_filter = "AND ir.created_at < :before" if before_dt else ""
            symbol_filter = ""
            params: dict = {"limit": limit}
            if before_dt:
                params["before"] = before_dt
            if symbol_list:
                symbol_filter = """AND EXISTS (
                    SELECT 1 FROM jsonb_array_elements(ir.company_impacts) ci
                    WHERE upper(ci->>'symbol') = ANY(:symbols)
                )"""
                params["symbols"] = symbol_list
            rows = await session.execute(
                text(f"""
                    SELECT
                        ir.id, ir.news_id, ir.relevant, ir.confidence,
                        ir.macro_theme, ir.affected_sectors, ir.company_impacts,
                        ir.processing_latency_ms, ir.created_at,
                        ni.source, ni.timestamp_utc, ni.headline, ni.body, ni.summary,
                        ni.url, ni.ingest_latency_ms, ni.category, ni.symbols_mentioned
                    FROM impact_reports ir
                    LEFT JOIN news_items ni ON ni.id = ir.news_id
                    WHERE 1=1 {relevant_filter} {before_filter} {symbol_filter}
                    ORDER BY ir.created_at DESC
                    LIMIT :limit
                """),
                params,
            )
            records = rows.fetchall()

        if records:
            return {
                "items": [_row_to_impact_report(r) for r in records],
                "count": len(records),
                "source": "db",
            }
    except Exception as e:
        logger.warning(f"DB impact feed error: {e}")

    # ── 2. Redis fallback: scan impact:* keys ─────────────────────────────
    try:
        redis = await _get_redis()
        keys = []
        async for key in redis.scan_iter("impact:*", count=200):
            keys.append(key)
            if len(keys) >= limit * 2:
                break

        before_iso = before_dt.isoformat() if before_dt else None
        reports = []
        for key in keys:
            raw = await redis.get(key)
            if raw:
                try:
                    r = json.loads(raw)
                    if relevant_only and not r.get("relevant", True):
                        continue
                    # Keyset cursor: string ISO compare matches the DB ordering
                    # (created_at is stored/serialised as ISO8601).
                    if before_iso and (r.get("created_at") or "") >= before_iso:
                        continue
                    if symbol_list and not any(
                        (ci.get("symbol") or "").upper() in symbol_list
                        for ci in (r.get("company_impacts") or [])
                    ):
                        continue
                    reports.append(_strip_internal(r))
                except json.JSONDecodeError:
                    continue

        # Sort newest first by created_at
        reports.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return {"items": reports[:limit], "count": len(reports[:limit]), "source": "redis"}

    except Exception as e:
        logger.error(f"Redis impact feed fallback error: {e}")
        return {"items": [], "count": 0, "error": "Storage unavailable"}


@router.get("/{news_id}")
async def get_impact(news_id: str):
    """Get ImpactReport for a specific news item. Redis first, then PostgreSQL."""

    # ── 1. Try Redis ───────────────────────────────────────────────────────
    try:
        redis = await _get_redis()
        raw = await redis.get(f"impact:{news_id}")
        if raw:
            return _strip_internal(json.loads(raw))
    except Exception as e:
        logger.warning(f"Redis impact lookup unavailable: {e}")

    # ── 2. Fallback: PostgreSQL ────────────────────────────────────────────
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            row = await session.execute(
                text("""
                    SELECT
                        ir.id, ir.news_id, ir.relevant, ir.confidence,
                        ir.macro_theme, ir.affected_sectors, ir.company_impacts,
                        ir.processing_latency_ms, ir.created_at,
                        ni.source, ni.timestamp_utc, ni.headline, ni.body, ni.summary,
                        ni.url, ni.ingest_latency_ms, ni.category, ni.symbols_mentioned
                    FROM impact_reports ir
                    LEFT JOIN news_items ni ON ni.id = ir.news_id
                    WHERE ir.news_id = :news_id
                    ORDER BY ir.created_at DESC
                    LIMIT 1
                """),
                {"news_id": news_id},
            )
            record = row.fetchone()

        if not record:
            raise HTTPException(status_code=404, detail=f"No impact report found for news_id={news_id}")

        return _row_to_impact_report(record)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DB impact lookup error: {e}")
        raise HTTPException(status_code=503, detail="Storage unavailable")


def _row_to_impact_report(r) -> dict:
    """Convert a DB row (impact_reports LEFT JOIN news_items) to a dict."""
    news_item = None
    if r.headline:  # news_items row joined in
        news_item = {
            "id": str(r.news_id),
            "source": r.source or "UNKNOWN",
            "timestamp_utc": r.timestamp_utc.isoformat() if r.timestamp_utc else None,
            "headline": r.headline,
            "body": r.body,
            "summary": r.summary,
            "url": r.url or "",
            "ingest_latency_ms": r.ingest_latency_ms or 0.0,
            "category": r.category,
            "symbols_mentioned": r.symbols_mentioned or [],
        }

    return {
        "id": str(r.id),
        "news_id": str(r.news_id),
        "relevant": r.relevant,
        "confidence": r.confidence,
        "macro_theme": r.macro_theme,
        "affected_sectors": r.affected_sectors or [],
        "company_impacts": r.company_impacts or [],
        "processing_latency_ms": r.processing_latency_ms,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "news_item": news_item or {},
    }
