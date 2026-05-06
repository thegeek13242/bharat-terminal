"""
Sentiment feed REST endpoints.

Read strategy (hot-path first):
  GET /sentiment/latest    — sentiment:latest hash (current state per market)
  GET /sentiment/recent    — sentiment:recent ZSET (event stream, 24h)
  GET /sentiment/themes    — aggregate counts/dir per macro_theme over the recent window
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sentiment", tags=["sentiment"])

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
REDIS_LATEST_HASH = "sentiment:latest"
REDIS_RECENT_ZSET = "sentiment:recent"

_redis: Optional[aioredis.Redis] = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


@router.get("/latest")
async def get_latest_per_market(
    theme: Optional[str] = None,
    direction: Optional[str] = None,
    limit: int = Query(default=100, le=500),
) -> Dict[str, Any]:
    """One snapshot per market — most recent observation, optionally filtered."""
    redis = await _get_redis()
    try:
        raw = await redis.hgetall(REDIS_LATEST_HASH)
    except Exception as e:
        logger.warning(f"Redis hgetall failed: {e}")
        return {"items": [], "count": 0, "error": "cache_unavailable"}

    items: List[dict] = []
    for _market_id, blob in raw.items():
        try:
            snap = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if theme and snap.get("macro_theme") != theme:
            continue
        if direction and snap.get("direction") != direction:
            continue
        items.append(snap)

    # Sort by abs(delta_1h) desc — biggest moves first; fall back to created_at
    def _key(s: dict) -> tuple:
        d = s.get("delta_1h")
        return (
            abs(float(d)) if d is not None else 0.0,
            s.get("created_at") or "",
        )

    items.sort(key=_key, reverse=True)
    items = items[:limit]
    return {"items": items, "count": len(items)}


@router.get("/recent")
async def get_recent_events(
    limit: int = Query(default=100, le=500),
    since_minutes: int = Query(default=720, description="Look back this many minutes"),
) -> Dict[str, Any]:
    """Event stream — every snapshot, newest first, within the lookback window."""
    redis = await _get_redis()
    try:
        # Pull the most recent N members regardless of since_minutes (capped),
        # then filter by score in Python — simpler than ZRANGEBYSCORE + LIMIT
        # tradeoffs at this scale.
        members = await redis.zrevrange(REDIS_RECENT_ZSET, 0, limit * 4 - 1, withscores=True)
    except Exception as e:
        logger.warning(f"Redis zrevrange failed: {e}")
        return {"items": [], "count": 0, "error": "cache_unavailable"}

    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).timestamp()

    items: List[dict] = []
    for blob, score in members:
        if score < cutoff:
            continue
        try:
            items.append(json.loads(blob))
        except json.JSONDecodeError:
            continue
        if len(items) >= limit:
            break

    return {"items": items, "count": len(items)}


@router.get("/themes")
async def get_theme_aggregates(
    since_minutes: int = Query(default=720),
) -> Dict[str, Any]:
    """
    Aggregate sentiment-by-theme view: for each macro_theme, count of bullish vs
    bearish snapshots in the window, weighted by magnitude. Useful for a macro
    overview banner.
    """
    redis = await _get_redis()
    try:
        members = await redis.zrevrange(REDIS_RECENT_ZSET, 0, 999, withscores=True)
    except Exception as e:
        logger.warning(f"Redis zrevrange failed: {e}")
        return {"themes": [], "error": "cache_unavailable"}

    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).timestamp()

    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "theme": "",
            "bullish": 0,
            "bearish": 0,
            "neutral": 0,
            "ambiguous": 0,
            "weighted_score": 0.0,  # +mag for bullish, -mag for bearish
            "events": 0,
            "last_question": "",
            "last_delta_1h": None,
        }
    )

    for blob, score in members:
        if score < cutoff:
            continue
        try:
            snap = json.loads(blob)
        except json.JSONDecodeError:
            continue

        theme = snap.get("macro_theme") or "UNKNOWN"
        direction = snap.get("direction") or "neutral"
        magnitude = int(snap.get("magnitude") or 1)

        bucket = agg[theme]
        bucket["theme"] = theme
        bucket["events"] += 1
        bucket[direction] = bucket.get(direction, 0) + 1
        if direction == "bullish_in":
            bucket["weighted_score"] += magnitude
        elif direction == "bearish_in":
            bucket["weighted_score"] -= magnitude

        # capture the most recent (first encountered, since zrevrange) per theme
        if not bucket["last_question"]:
            bucket["last_question"] = snap.get("question", "")
            bucket["last_delta_1h"] = snap.get("delta_1h")

    # rename direction keys to match frontend expectation
    out = []
    for v in agg.values():
        out.append({
            "theme": v["theme"],
            "bullish_in": v.get("bullish_in", 0),
            "bearish_in": v.get("bearish_in", 0),
            "neutral": v.get("neutral", 0),
            "ambiguous": v.get("ambiguous", 0),
            "weighted_score": v["weighted_score"],
            "events": v["events"],
            "last_question": v["last_question"],
            "last_delta_1h": v["last_delta_1h"],
        })
    out.sort(key=lambda x: abs(x["weighted_score"]), reverse=True)
    return {"themes": out, "count": len(out)}
