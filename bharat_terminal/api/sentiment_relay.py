"""
Kafka consumer for analysed.sentiment.in:
  • Caches each market's latest snapshot to Redis (HSET, no TTL — refreshed on each
    new observation; cleaned by a soft cap on hash size).
  • Appends to a sorted set keyed by created_at for time-range REST queries.
  • Broadcasts to WebSocket clients as {type: "sentiment_snapshot", data: ...}.

Storage layout
--------------
  sentiment:latest        HASH    market_id -> snapshot JSON (current state)
  sentiment:recent        ZSET    score=unix_ts(created_at), member=snapshot JSON
                                    (24h window, pruned on each write)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import redis.asyncio as aioredis
from aiokafka import AIOKafkaConsumer

from bharat_terminal.api.ws_manager import manager

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_ANALYSED_SENTIMENT = "analysed.sentiment.in"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

REDIS_LATEST_HASH = "sentiment:latest"
REDIS_RECENT_ZSET = "sentiment:recent"
RECENT_TTL_HOURS = 24
LATEST_HASH_SOFT_CAP = 500   # soft cap to prevent unbounded growth

_consumer_task: Optional[asyncio.Task] = None
_redis: Optional[aioredis.Redis] = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def start_sentiment_relay() -> None:
    global _consumer_task
    _consumer_task = asyncio.create_task(_relay_loop(), name="sentiment-relay")
    logger.info("Sentiment relay started")


async def stop_sentiment_relay() -> None:
    global _consumer_task, _redis
    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass
    if _redis:
        await _redis.aclose()
        _redis = None


async def _relay_loop() -> None:
    consumer = AIOKafkaConsumer(
        TOPIC_ANALYSED_SENTIMENT,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="frontend-sentiment-consumers",
        auto_offset_reset="latest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        enable_auto_commit=True,
    )

    try:
        await consumer.start()
        logger.info(f"Sentiment relay consuming from {TOPIC_ANALYSED_SENTIMENT}")

        while True:
            try:
                msg = await consumer.getone()
                snap: dict = msg.value
                await _handle_snapshot(snap)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Sentiment relay error: {e}", exc_info=True)
    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()


async def _handle_snapshot(snap: dict) -> None:
    market_id = snap.get("market_id", "")
    if not market_id:
        return

    # ── 1. Redis: latest-by-market hash + 24h recent ZSET ──────────────────
    redis = await _get_redis()
    try:
        snap_json = json.dumps(snap, default=str)

        ts_raw = snap.get("created_at") or snap.get("timestamp_utc") or ""
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            ts = datetime.now(timezone.utc).timestamp()

        pipe = redis.pipeline()
        pipe.hset(REDIS_LATEST_HASH, market_id, snap_json)
        pipe.zadd(REDIS_RECENT_ZSET, {snap_json: ts})
        # prune ZSET older than 24h
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=RECENT_TTL_HOURS)).timestamp()
        pipe.zremrangebyscore(REDIS_RECENT_ZSET, "-inf", cutoff)
        await pipe.execute()

        # soft cap on the latest hash — prune oldest if exceeded
        size = await redis.hlen(REDIS_LATEST_HASH)
        if size > LATEST_HASH_SOFT_CAP:
            # cheap fallback: trim arbitrary excess; production would track LRU
            keys = await redis.hkeys(REDIS_LATEST_HASH)
            to_drop = keys[: size - LATEST_HASH_SOFT_CAP]
            if to_drop:
                await redis.hdel(REDIS_LATEST_HASH, *to_drop)
    except Exception as e:
        logger.warning(f"Sentiment Redis write failed (continuing): {e}")

    # ── 2. WebSocket broadcast ─────────────────────────────────────────────
    event = {
        "type": "sentiment_snapshot",
        "data": snap,
        "timestamp": datetime.utcnow().isoformat(),
    }
    await manager.broadcast(event)
