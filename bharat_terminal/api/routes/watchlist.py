"""Watchlist management endpoints."""
import json
import logging
import os
from typing import List, Optional
from fastapi import APIRouter, Body
import redis.asyncio as aioredis
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/watchlist", tags=["watchlist"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_redis = None


async def get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


class WatchlistItem(BaseModel):
    symbol: str
    price_alert: Optional[float] = None
    impact_threshold: int = 3  # 1-5


@router.get("/")
async def get_watchlist():
    redis = await get_redis()
    raw = await redis.get("watchlist:default")
    if not raw:
        return {"items": []}
    return {"items": json.loads(raw)}


@router.post("/")
async def update_watchlist(items: List[WatchlistItem] = Body(...)):
    redis = await get_redis()
    data = [item.model_dump() for item in items]
    await redis.set("watchlist:default", json.dumps(data))
    return {"saved": len(data), "items": data}
