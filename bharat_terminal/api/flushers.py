"""Redis-queue → Postgres flushers (runs in the api-gateway lifespan).

Two producers push JSON entries onto capped Redis lists from every service:

    alerts:history  <- analysis/guardrails._push_history  -> alert_events
    llm:calls       <- analysis/llm_logger._push_redis    -> llm_call_log

This module drains both every FLUSH_INTERVAL_S into the durable tables
(migration 035). Rationale: the producers are sync code in ~8 services with
no SQLAlchemy setup; a capped Redis list + one always-on drainer (the
api-gateway already has DB access) is the minimal durable path.

Failure contract:
- Malformed JSON entry: skipped with a log line; never kills the batch.
- DB failure: the whole batch is RPUSHed back (tail position) and retried
  next tick; growth is bounded by the producers' LTRIM caps.
- Everything fail-open: a flusher crash must never affect the API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import redis.asyncio as aioredis
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
FLUSH_INTERVAL_S = float(os.getenv("FLUSH_INTERVAL_S", "15"))
FLUSH_BATCH_MAX = int(os.getenv("FLUSH_BATCH_MAX", "500"))

_ALERT_INSERT = text("""
    INSERT INTO alert_events (ts, service, kind, subject, body, delivered)
    VALUES (CAST(:ts AS timestamptz), :service, :kind, :subject, :body, :delivered)
""")

_LLM_INSERT = text("""
    INSERT INTO llm_call_log
        (call_id, ts, service, model, stage, context_id,
         prompt_tokens, completion_tokens, thinking_tokens, cached_tokens,
         web_search_calls, turns, latency_ms, cost_usd, success, error)
    VALUES
        (:call_id, CAST(:ts AS timestamptz), :service, :model, :stage, :context_id,
         :prompt_tokens, :completion_tokens, :thinking_tokens, :cached_tokens,
         :web_search_calls, :turns, :latency_ms, :cost_usd, :success, :error)
    ON CONFLICT (call_id) DO NOTHING
""")


def _alert_params(entry: dict) -> dict:
    return {
        "ts": entry.get("ts"),
        "service": str(entry.get("service") or "unknown")[:120],
        "kind": str(entry.get("kind") or "UNKNOWN")[:120],
        "subject": str(entry.get("subject") or "")[:500],
        "body": entry.get("body"),
        "delivered": entry.get("delivered"),
    }


def _llm_params(entry: dict) -> dict:
    return {
        "call_id": str(entry.get("call_id") or "")[:200],
        "ts": entry.get("timestamp"),
        "service": str(entry.get("service") or "")[:120] or None,
        "model": str(entry.get("model") or "unknown")[:120],
        "stage": str(entry.get("stage") or "unknown")[:120],
        "context_id": str(entry.get("context_id") or "")[:200] or None,
        "prompt_tokens": int(entry.get("prompt_tokens") or 0),
        "completion_tokens": int(entry.get("completion_tokens") or 0),
        "thinking_tokens": int(entry.get("thinking_tokens") or 0),
        "cached_tokens": int(entry.get("cached_tokens") or 0),
        "web_search_calls": int(entry.get("web_search_calls") or 0),
        "turns": int(entry.get("turns") or 0),
        "latency_ms": float(entry.get("latency_ms") or 0.0),
        "cost_usd": float(entry.get("cost_usd") or 0.0),
        "success": bool(entry.get("success", True)),
        "error": entry.get("error"),
    }


_QUEUES = (
    ("alerts:history", _ALERT_INSERT, _alert_params),
    ("llm:calls", _LLM_INSERT, _llm_params),
)

_redis = None
_task: asyncio.Task | None = None


async def _get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def _drain_queue(r, queue: str, insert, to_params) -> int:
    """RPOP up to FLUSH_BATCH_MAX entries and insert them in one transaction.
    Returns rows inserted. Raises nothing — on DB failure the raw entries are
    pushed back for the next tick."""
    raw = await r.rpop(queue, FLUSH_BATCH_MAX)
    if not raw:
        return 0
    if isinstance(raw, str):  # count=1 edge: redis returns a bare string
        raw = [raw]

    params, kept = [], []
    for item in raw:
        try:
            params.append(to_params(json.loads(item)))
            kept.append(item)
        except Exception as e:
            logger.warning("[flusher] %s: skipping malformed entry (%s): %.120s",
                           queue, e, item)

    if not params:
        return 0
    try:
        factory = get_session_factory()
        async with factory() as s:
            await s.execute(insert, params)  # executemany
            await s.commit()
        return len(params)
    except Exception as e:
        logger.warning("[flusher] %s: DB insert failed (%s) — pushing %d "
                       "entries back", queue, str(e)[:200], len(kept))
        try:
            if kept:
                await r.rpush(queue, *kept)
        except Exception:
            logger.error("[flusher] %s: push-back also failed; %d entries lost",
                         queue, len(kept))
        return 0


async def _flush_loop() -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        try:
            r = await _get_redis()
            for queue, insert, to_params in _QUEUES:
                n = await _drain_queue(r, queue, insert, to_params)
                if n:
                    logger.debug("[flusher] %s -> %d rows", queue, n)
        except Exception as e:
            logger.warning("[flusher] cycle failed: %s", str(e)[:200])


async def start_flushers() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_flush_loop(), name="redis-flushers")
        logger.info("Redis->Postgres flushers started (interval %.0fs)",
                    FLUSH_INTERVAL_S)


async def stop_flushers() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
