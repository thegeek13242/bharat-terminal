"""Relay Angel One equity LTP ticks to internal WebSocket clients.

The ingestion adapter publishes each tick to the Redis channel `ltp.updates`
(and mirrors the latest value to `ltp:{SYMBOL}` for snapshot reads). This
relay subscribes to the channel and fans ticks out — ONLY to clients that
issued a gated `subscribe_ltp` (see routes/websocket.py). It also refreshes
the `ltp:want` demand ZSET every 30s for symbols with live subscribers, so
the adapter keeps their subscriptions alive.

LICENSING: personal-use Angel One data — the fanout path is per-subscriber
by design; never convert this to a broadcast.
"""
import asyncio
import json
import logging
import os
import time

import redis.asyncio as aioredis

from bharat_terminal.api.ws_manager import manager

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
LTP_WANT_KEY = "ltp:want"
LTP_CHANNEL = "ltp.updates"
DEMAND_KEEPALIVE_S = 30

_redis = None
_tasks: list = []


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def note_demand(symbol: str) -> None:
    """Record demand for a symbol so the adapter subscribes it. Fail-open."""
    try:
        r = await _get_redis()
        await r.zadd(LTP_WANT_KEY, {symbol.upper(): time.time()})
    except Exception as e:
        logger.debug(f"[ltp_relay] note_demand failed: {e}")


async def cached_ltp(symbol: str):
    """Latest tick snapshot for a symbol (None if absent/stale)."""
    try:
        r = await _get_redis()
        raw = await r.get(f"ltp:{symbol.upper()}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _pump() -> None:
    """Redis pub/sub → per-subscriber WS delivery. Reconnects forever."""
    while True:
        try:
            r = await _get_redis()
            pubsub = r.pubsub()
            await pubsub.subscribe(LTP_CHANNEL)
            logger.info(f"[ltp_relay] subscribed to {LTP_CHANNEL}")
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                except Exception:
                    continue
                symbol = data.get("symbol")
                if symbol:
                    await manager.send_ltp(symbol, {"type": "ltp", **data})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[ltp_relay] pump error (reconnecting in 3s): {e}")
            await asyncio.sleep(3)


async def _demand_keepalive() -> None:
    """Refresh the demand ZSET for symbols with live subscribers."""
    while True:
        try:
            await asyncio.sleep(DEMAND_KEEPALIVE_S)
            symbols = manager.ltp_symbols()
            if not symbols:
                continue
            r = await _get_redis()
            now = time.time()
            await r.zadd(LTP_WANT_KEY, {s: now for s in symbols})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[ltp_relay] keepalive error: {e}")


async def start_ltp_relay() -> None:
    _tasks.append(asyncio.create_task(_pump(), name="ltp-relay-pump"))
    _tasks.append(asyncio.create_task(_demand_keepalive(), name="ltp-relay-keepalive"))
    logger.info("LTP relay started")


async def stop_ltp_relay() -> None:
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _tasks.clear()
    logger.info("LTP relay stopped")
