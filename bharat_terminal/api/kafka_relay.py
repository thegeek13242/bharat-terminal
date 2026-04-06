"""
Kafka consumer that:
  1. Persists ImpactReports + embedded NewsItems to Redis (hot cache) and PostgreSQL (durable)
  2. Relays ImpactReport events to WebSocket clients

Persistence strategy:
  Redis  — news:recent (sorted set, score=unix_ts, 24h window)
         — impact:{news_id} (string, 24h TTL)
  PostgreSQL — news_items table, impact_reports table (INSERT … ON CONFLICT DO NOTHING)
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import redis.asyncio as aioredis
from aiokafka import AIOKafkaConsumer
from sqlalchemy import text

from bharat_terminal.api.ws_manager import manager
from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_ANALYSED = "analysed.impact.in"
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

NEWS_CACHE_TTL_HOURS = 24
REDIS_NEWS_KEY = "news:recent"

_consumer_task: Optional[asyncio.Task] = None
_redis: Optional[aioredis.Redis] = None


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def start_kafka_relay():
    global _consumer_task
    _consumer_task = asyncio.create_task(_relay_loop())
    logger.info("Kafka relay started")


async def stop_kafka_relay():
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


async def _relay_loop():
    consumer = AIOKafkaConsumer(
        TOPIC_ANALYSED,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="frontend-consumers",
        auto_offset_reset="latest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        enable_auto_commit=True,
    )

    try:
        await consumer.start()
        logger.info(f"Relay consuming from {TOPIC_ANALYSED}")

        async for msg in consumer:
            try:
                report: dict = msg.value
                await _handle_report(report)
            except Exception as e:
                logger.error(f"Relay error: {e}", exc_info=True)

    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()


async def _handle_report(report: dict):
    """Persist to Redis + DB, then broadcast to WebSocket clients."""
    news_id = report.get("news_id", "")
    relevant = report.get("relevant", True)
    news_item: dict = report.get("news_item", {})

    # ── 1. Redis: cache news item in sorted set ────────────────────────────
    redis = await _get_redis()
    try:
        if news_item and news_id:
            # Score = unix timestamp for time-range queries
            ts_raw = news_item.get("timestamp_utc", "")
            try:
                ts = datetime.fromisoformat(ts_raw).timestamp() if ts_raw else datetime.utcnow().timestamp()
            except ValueError:
                ts = datetime.utcnow().timestamp()

            pipe = redis.pipeline()
            pipe.zadd(REDIS_NEWS_KEY, {json.dumps(news_item): ts}, nx=True)
            # Prune entries older than 24h to keep sorted set bounded
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=NEWS_CACHE_TTL_HOURS)).timestamp()
            pipe.zremrangebyscore(REDIS_NEWS_KEY, "-inf", cutoff)
            await pipe.execute()

        # ── 2. Redis: cache impact report with TTL ─────────────────────────
        if news_id:
            await redis.setex(
                f"impact:{news_id}",
                NEWS_CACHE_TTL_HOURS * 3600,
                json.dumps(report),
            )
    except Exception as e:
        logger.warning(f"Redis write failed (continuing): {e}")

    # ── 3. PostgreSQL: durable storage ────────────────────────────────────
    try:
        await _persist_to_db(report, news_item, news_id)
    except Exception as e:
        logger.warning(f"DB persist failed (continuing): {e}")

    # ── 4. WebSocket broadcast ────────────────────────────────────────────
    if not relevant:
        return  # Don't clutter the feed with irrelevant noise

    event = {
        "type": "impact_report",
        "data": report,
        "timestamp": datetime.utcnow().isoformat(),
    }
    await manager.broadcast(event)


async def _persist_to_db(report: dict, news_item: dict, news_id: str):
    session_factory = get_session_factory()
    async with session_factory() as session:
        # Insert news item (ignore duplicates)
        if news_item and news_id:
            await session.execute(
                text("""
                    INSERT INTO news_items
                        (id, source, timestamp_utc, headline, body, url,
                         ingest_latency_ms, category, symbols_mentioned)
                    VALUES
                        (:id, :source, :timestamp_utc, :headline, :body, :url,
                         :ingest_latency_ms, :category, :symbols_mentioned::jsonb)
                    ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id": news_id,
                    "source": news_item.get("source", "UNKNOWN"),
                    "timestamp_utc": _parse_dt(news_item.get("timestamp_utc")),
                    "headline": news_item.get("headline", ""),
                    "body": news_item.get("body"),
                    "url": news_item.get("url", ""),
                    "ingest_latency_ms": news_item.get("ingest_latency_ms", 0.0),
                    "category": news_item.get("category"),
                    "symbols_mentioned": json.dumps(news_item.get("symbols_mentioned", [])),
                },
            )

        # Insert impact report (ignore duplicates)
        report_id = report.get("id", "")
        if report_id:
            await session.execute(
                text("""
                    INSERT INTO impact_reports
                        (id, news_id, relevant, confidence, macro_theme,
                         affected_sectors, company_impacts, trade_signals,
                         processing_latency_ms)
                    VALUES
                        (:id, :news_id, :relevant, :confidence, :macro_theme,
                         :affected_sectors::jsonb, :company_impacts::jsonb,
                         :trade_signals::jsonb, :processing_latency_ms)
                    ON CONFLICT (id) DO NOTHING
                """),
                {
                    "id": report_id,
                    "news_id": news_id,
                    "relevant": report.get("relevant", False),
                    "confidence": report.get("confidence", 0.0),
                    "macro_theme": report.get("macro_theme"),
                    "affected_sectors": json.dumps(report.get("affected_sectors", [])),
                    "company_impacts": json.dumps(report.get("company_impacts", [])),
                    "trade_signals": json.dumps(report.get("trade_signals", [])),
                    "processing_latency_ms": report.get("processing_latency_ms", 0.0),
                },
            )

        await session.commit()


def _parse_dt(value) -> datetime:
    if not value:
        return datetime.utcnow()
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return datetime.utcnow()
