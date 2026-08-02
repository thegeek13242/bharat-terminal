"""Per-user activity recording — API request log + frontend beacon sink.

Feeds user_activity_events (migration 035), the substrate for /admin/analytics
(visits, time-on-platform, feature histograms). Design constraints, in order:

1. ZERO hot-path cost: the middleware only enqueues (bounded asyncio.Queue,
   drop-on-full); a background task batch-inserts every FLUSH_S seconds.
2. Never break a request: every step is fail-open.
3. Store the ROUTE TEMPLATE (/company/{symbol}), never the raw URL — no
   symbol/query cardinality, no accidental PII in paths.
4. High-frequency polling routes are sampled 1-in-N so feature histograms
   stay directionally right without drowning the table.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Optional

from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from bharat_terminal.api.auth import FEATURE_REGISTRY, user_from_session_cookie
from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)

FLUSH_S = float(os.getenv("ACTIVITY_FLUSH_S", "5"))
QUEUE_MAX = int(os.getenv("ACTIVITY_QUEUE_MAX", "10000"))

# Prefixes never recorded: infra, auth (login noise), the WS upgrade, the
# beacon itself (records through record_event directly), CORS preflight.
SKIP_PREFIXES = ("/health", "/auth", "/ws", "/events", "/docs", "/openapi")

# Hot polling endpoints: recorded 1-in-SAMPLE_RATE. Everything else 1-in-1.
SAMPLED_PREFIXES = ("/internal/ltp", "/sentiment", "/news", "/impact")
SAMPLE_RATE = int(os.getenv("ACTIVITY_SAMPLE_RATE", "20"))

# Longest prefix wins so /internal/ltp beats a hypothetical /internal.
_FEATURE_BY_PREFIX = sorted(
    ((p, feat) for feat, prefixes in FEATURE_REGISTRY.items() for p in prefixes),
    key=lambda pf: -len(pf[0]),
)

_INSERT = text("""
    INSERT INTO user_activity_events
        (user_id, email, event_type, method, path, status, duration_ms, feature)
    VALUES (:user_id, :email, :event_type, :method, :path, :status,
            :duration_ms, :feature)
""")


def feature_for_path(path: str) -> Optional[str]:
    for prefix, feat in _FEATURE_BY_PREFIX:
        if path.startswith(prefix):
            return feat
    return None


class ActivityRecorder:
    """Bounded queue + periodic batch flusher. Lifespan-managed."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self._dropped = 0
        self._task: Optional[asyncio.Task] = None

    def enqueue(self, event: dict) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logger.warning("[activity] queue full — %d events dropped so far",
                               self._dropped)

    async def _flush(self) -> None:
        batch = []
        while not self._queue.empty() and len(batch) < 2000:
            batch.append(self._queue.get_nowait())
        if not batch:
            return
        try:
            factory = get_session_factory()
            async with factory() as s:
                await s.execute(_INSERT, batch)  # executemany
                await s.commit()
        except Exception as e:
            # Fail-open: analytics loss is acceptable, API impact is not.
            # (Covers pre-migration-035 deploys where the table is absent.)
            logger.warning("[activity] flush of %d events failed: %s",
                           len(batch), str(e)[:200])

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_S)
            try:
                await self._flush()
            except Exception as e:
                logger.warning("[activity] flush loop error: %s", e)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="activity-recorder")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            await self._flush()  # best-effort final drain


recorder = ActivityRecorder()


def record_event(user: dict, event_type: str, path: Optional[str]) -> None:
    """Direct sink for beacon events (page_view/heartbeat)."""
    try:
        uid = int(user["id"])
    except (KeyError, TypeError, ValueError):
        return
    recorder.enqueue({
        "user_id": uid,
        "email": user.get("email"),
        "event_type": event_type,
        "method": None,
        "path": (path or "")[:200] or None,
        "status": None,
        "duration_ms": None,
        "feature": feature_for_path(path or ""),
    })


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        try:
            self._record(request, response, (time.monotonic() - t0) * 1000)
        except Exception as e:  # recording must never break a request
            logger.debug("[activity] record failed: %s", e)
        return response

    def _record(self, request, response, duration_ms: float) -> None:
        if request.method == "OPTIONS":
            return
        raw_path = request.url.path
        if any(raw_path.startswith(p) for p in SKIP_PREFIXES):
            return
        # Non-raising decode; unauthenticated traffic is not analytics.
        user = user_from_session_cookie(request.cookies)
        if not user:
            return
        if (any(raw_path.startswith(p) for p in SAMPLED_PREFIXES)
                and random.randrange(SAMPLE_RATE) != 0):
            return
        try:
            uid = int(user["id"])
        except (TypeError, ValueError):
            return
        # Route TEMPLATE, not the raw URL (set by the router after matching).
        route = request.scope.get("route")
        path = getattr(route, "path", None) or raw_path[:120]
        recorder.enqueue({
            "user_id": uid,
            "email": user.get("email"),
            "event_type": "api",
            "method": request.method,
            "path": path[:200],
            "status": response.status_code,
            "duration_ms": int(duration_ms),
            "feature": feature_for_path(path),
        })
