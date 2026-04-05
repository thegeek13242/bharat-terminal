"""Impact report REST endpoints."""
import json
import logging
import os
from fastapi import APIRouter, HTTPException
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/impact", tags=["impact"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_redis = None


async def get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


@router.get("/{news_id}")
async def get_impact(news_id: str):
    """Get ImpactReport for a specific news item."""
    redis = await get_redis()

    try:
        raw = await redis.get(f"impact:{news_id}")
        if not raw:
            raise HTTPException(status_code=404, detail=f"Impact report for {news_id} not found")
        return json.loads(raw)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Redis impact lookup error: {e}")
        raise HTTPException(status_code=503, detail="Cache unavailable")
