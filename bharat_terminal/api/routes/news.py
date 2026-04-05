"""News feed REST endpoints."""
import json
import logging
import os
from typing import Optional
from datetime import datetime, timezone, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/news", tags=["news"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_redis = None


async def get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


@router.get("/feed")
async def get_news_feed(
    limit: int = Query(default=50, le=200),
    sector: Optional[str] = None,
    source: Optional[str] = None,
    since_minutes: int = Query(default=60, description="News from last N minutes"),
):
    """
    Get recent news items. Reads from Redis cache of recent NewsItems.

    Adapters publish to Redis sorted set `news:recent` (score = unix timestamp).
    """
    redis = await get_redis()

    try:
        # Read recent news from Redis sorted set (score = unix timestamp)
        since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        since_ts = since.timestamp()

        raw_items = await redis.zrangebyscore(
            "news:recent",
            since_ts,
            "+inf",
            withscores=False,
            start=0,
            num=limit,
        )

        items = []
        for raw in reversed(raw_items):  # Most recent first
            try:
                item = json.loads(raw)
                if sector and sector.upper() not in (item.get("sectors") or []):
                    continue
                if source and item.get("source", "").upper() != source.upper():
                    continue
                items.append(item)
            except json.JSONDecodeError:
                continue

        return {"items": items, "count": len(items), "sector": sector}

    except Exception as e:
        logger.warning(f"Redis news feed error: {e}")
        return {"items": [], "count": 0, "sector": sector, "error": "Cache unavailable"}
