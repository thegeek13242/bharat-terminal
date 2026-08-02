"""Worker-loop liveness heartbeats.

Docker "Up" only proves the container exists; this project's characteristic
failure mode is a running container whose loop stopped producing (dormant
services, wedged syncs). Each worker calls beat()/abeat() once per loop
ITERATION — in the body that proves liveness, never at startup — writing

    SETEX hb:<service> <ttl> <utc-isoformat>

to Redis. /admin/health reads hb:* and reports "loop alive Ns ago" or
"stalled" (key expired) per service, independent of container state.

Fail-open everywhere: a Redis outage must never take a worker down, so every
error is swallowed after one debug log. Both clients are lazy singletons —
importing this module costs nothing for services that never beat.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DEFAULT_TTL_S = int(os.getenv("HEARTBEAT_TTL_S", "180"))

_sync_client = None
_async_client = None


def _key(service: str) -> str:
    return f"hb:{service}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def beat(service: str, ttl: Optional[int] = None) -> None:
    """Synchronous heartbeat (for sync loop bodies)."""
    global _sync_client
    try:
        if _sync_client is None:
            import redis
            _sync_client = redis.Redis.from_url(
                REDIS_URL, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2,
            )
        _sync_client.setex(_key(service), ttl or DEFAULT_TTL_S, _now())
    except Exception as e:
        logger.debug("heartbeat %s failed: %s", service, e)


async def abeat(service: str, ttl: Optional[int] = None) -> None:
    """Async heartbeat (for asyncio loop bodies)."""
    global _async_client
    try:
        if _async_client is None:
            import redis.asyncio as aioredis
            _async_client = aioredis.from_url(
                REDIS_URL, decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2,
            )
        await _async_client.setex(_key(service), ttl or DEFAULT_TTL_S, _now())
    except Exception as e:
        logger.debug("heartbeat %s failed: %s", service, e)


async def heartbeat_loop(service: str, interval_s: int = 60,
                         ttl: Optional[int] = None) -> None:
    """Standalone beat task for services whose work is purely event-driven
    (Kafka consumers with no periodic tick). Beats immediately, then every
    interval_s. Run as an asyncio task; cancel to stop.

    Nuance: this proves the process EVENT LOOP is alive, not that the
    consumer is making progress — a wedged consumer coroutine with a healthy
    loop still beats. Progress-level stalls are the alert system's job
    (PIPELINE_STALL etc.); this catches dead/looping-restart processes.
    """
    import asyncio
    while True:
        await abeat(service, ttl)
        await asyncio.sleep(interval_s)
