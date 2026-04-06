"""Impact report REST endpoints.

Read strategy:
  1. Redis  `impact:{news_id}`   (sub-millisecond, 24h TTL)
  2. PostgreSQL  `impact_reports` (permanent fallback)
"""
import json
import logging
import os

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/impact", tags=["impact"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_redis = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


@router.get("/{news_id}")
async def get_impact(news_id: str):
    """Get ImpactReport for a specific news item. Redis first, then PostgreSQL."""

    # ── 1. Try Redis ───────────────────────────────────────────────────────
    try:
        redis = await _get_redis()
        raw = await redis.get(f"impact:{news_id}")
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.warning(f"Redis impact lookup unavailable: {e}")

    # ── 2. Fallback: PostgreSQL ────────────────────────────────────────────
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            row = await session.execute(
                text("""
                    SELECT id, news_id, relevant, confidence, macro_theme,
                           affected_sectors, company_impacts, trade_signals,
                           processing_latency_ms, created_at
                    FROM impact_reports
                    WHERE news_id = :news_id
                    ORDER BY created_at DESC
                    LIMIT 1
                """),
                {"news_id": news_id},
            )
            record = row.fetchone()

        if not record:
            raise HTTPException(status_code=404, detail=f"No impact report found for news_id={news_id}")

        return {
            "id": str(record.id),
            "news_id": str(record.news_id),
            "relevant": record.relevant,
            "confidence": record.confidence,
            "macro_theme": record.macro_theme,
            "affected_sectors": record.affected_sectors or [],
            "company_impacts": record.company_impacts or [],
            "trade_signals": record.trade_signals or [],
            "processing_latency_ms": record.processing_latency_ms,
            "created_at": record.created_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"DB impact lookup error: {e}")
        raise HTTPException(status_code=503, detail="Storage unavailable")
