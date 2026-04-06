"""News feed REST endpoints.

Read strategy (hot-path first):
  1. Redis sorted set  `news:recent`  (sub-millisecond)
  2. PostgreSQL        `news_items`    (fallback when Redis is cold/empty)
"""
import json
import logging
import os
from typing import Optional
from datetime import datetime, timezone, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, Query
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/news", tags=["news"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_redis = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


@router.get("/feed")
async def get_news_feed(
    limit: int = Query(default=50, le=200),
    sector: Optional[str] = None,
    source: Optional[str] = None,
    since_minutes: int = Query(default=480, description="News from last N minutes"),
):
    """
    Get recent news items.
    Reads from Redis hot cache first; falls back to PostgreSQL when cache is empty.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)

    # ── 1. Try Redis ───────────────────────────────────────────────────────
    try:
        redis = await _get_redis()
        raw_items = await redis.zrangebyscore(
            "news:recent",
            since.timestamp(),
            "+inf",
            withscores=False,
            start=0,
            num=limit * 2,  # over-fetch to allow client-side filter
        )

        items = []
        for raw in reversed(raw_items):  # most-recent first
            try:
                item = json.loads(raw)
                if sector and sector.upper() not in (item.get("sectors") or []):
                    continue
                if source and item.get("source", "").upper() != source.upper():
                    continue
                items.append(item)
                if len(items) >= limit:
                    break
            except json.JSONDecodeError:
                continue

        if items:
            return {"items": items, "count": len(items), "sector": sector, "source": "redis"}

    except Exception as e:
        logger.warning(f"Redis news feed unavailable: {e}")

    # ── 2. Fallback: PostgreSQL ────────────────────────────────────────────
    try:
        session_factory = get_session_factory()
        async with session_factory() as session:
            filters = ["timestamp_utc >= :since"]
            params: dict = {"since": since, "limit": limit}

            if source:
                filters.append("source = :source")
                params["source"] = source.upper()

            where = " AND ".join(filters)
            rows = await session.execute(
                text(f"""
                    SELECT id, source, timestamp_utc, headline, body, url,
                           ingest_latency_ms, category, symbols_mentioned
                    FROM news_items
                    WHERE {where}
                    ORDER BY timestamp_utc DESC
                    LIMIT :limit
                """),
                params,
            )
            items = [
                {
                    "id": str(r.id),
                    "source": r.source,
                    "timestamp_utc": r.timestamp_utc.isoformat(),
                    "headline": r.headline,
                    "body": r.body,
                    "url": r.url,
                    "ingest_latency_ms": r.ingest_latency_ms,
                    "category": r.category,
                    "symbols_mentioned": r.symbols_mentioned or [],
                }
                for r in rows
            ]

        return {"items": items, "count": len(items), "sector": sector, "source": "db"}

    except Exception as e:
        logger.error(f"DB news feed error: {e}")
        return {"items": [], "count": 0, "sector": sector, "error": "Storage unavailable"}
