"""Watchlist management endpoints (per authenticated user)."""
import json
import logging
import os
from typing import List, Optional
from fastapi import APIRouter, Body, Depends
import redis.asyncio as aioredis
from pydantic import BaseModel

from bharat_terminal.api.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/watchlist", tags=["watchlist"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
_redis = None


async def get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def _key(user: dict) -> str:
    return f"watchlist:user:{user['id']}"


class WatchlistItem(BaseModel):
    symbol: str
    price_alert: Optional[float] = None
    impact_threshold: int = 3  # 1-5


@router.get("/")
async def get_watchlist(user: dict = Depends(get_current_user)):
    redis = await get_redis()
    raw = await redis.get(_key(user))
    if not raw:
        return {"items": []}
    return {"items": json.loads(raw)}


@router.post("/")
async def update_watchlist(
    items: List[WatchlistItem] = Body(...),
    user: dict = Depends(get_current_user),
):
    redis = await get_redis()
    data = [item.model_dump() for item in items]
    await redis.set(_key(user), json.dumps(data))
    return {"saved": len(data), "items": data}
