import asyncio
import os
import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, time as dtime, timezone, timedelta
from typing import AsyncIterator, Optional

import aiohttp

try:
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover
    aioredis = None

from bharat_terminal.types import NewsItem

# Hard gate: never publish items older than this
_MAX_ITEM_AGE_HOURS = 48

# Adaptive polling — IST market hours (NSE: 09:15-15:30; widen slightly for pre/post)
IST_OFFSET = timedelta(hours=5, minutes=30)
MARKET_OPEN_IST = dtime(8, 0)
MARKET_CLOSE_IST = dtime(16, 0)

# Redis key prefix for persistent seen-id sets
SEEN_IDS_KEY_PREFIX = "news:seen:"

# Backstop on the per-item publish callback. The 2026-07-20 stall parked
# adapter loops forever inside the callback with no exception and no log —
# this converts any such park into a loud TimeoutError, which the run loop
# logs and fix-5d2cfde's post-publish seen-marking then turns into a retry.
ADAPTER_CALLBACK_TIMEOUT_S = float(os.getenv("ADAPTER_CALLBACK_TIMEOUT_S", "120"))
SEEN_IDS_TTL_SECONDS = 48 * 3600

logger = logging.getLogger(__name__)


def _is_market_hours(now_utc: Optional[datetime] = None) -> bool:
    """Return True if current time is within NSE market hours (Mon-Fri 08:00-16:00 IST)."""
    now = now_utc or datetime.now(timezone.utc)
    now_ist = now + IST_OFFSET
    if now_ist.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN_IST <= now_ist.time() <= MARKET_CLOSE_IST


class BaseAdapter(ABC):
    """Base class for all news source adapters with circuit breaker and rate limiting."""

    source_name: str
    # Legacy single-interval field (kept for backward compat)
    poll_interval_seconds: float = 60.0
    # Adaptive polling overrides — subclasses can tune
    poll_interval_fast: Optional[float] = None   # During market hours
    poll_interval_slow: Optional[float] = None   # Off-hours
    rate_limit_per_minute: int = 30
    # Whether to use Redis-backed seen-id persistence (falls back to in-memory)
    use_redis_seen: bool = True

    def __init__(self, redis_client: Optional["aioredis.Redis"] = None):
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_times: list = []
        self._circuit_open = False
        self._failure_count = 0
        self._failure_threshold = 5
        self._recovery_timeout = 60
        self._last_failure_time: Optional[float] = None
        self._redis = redis_client
        self._seen_ids: set = set()  # in-memory fallback / cache

    # ── Adaptive polling ──────────────────────────────────────────────────

    def _current_poll_interval(self) -> float:
        """Pick fast or slow interval based on market hours. Falls back to legacy field."""
        if _is_market_hours():
            return self.poll_interval_fast or self.poll_interval_seconds
        return self.poll_interval_slow or self.poll_interval_seconds

    # ── Redis-backed seen-id helpers ──────────────────────────────────────
    # Strategy: hydrate in-memory set once from Redis on startup, then
    # write-through on every add. Reads stay fast (in-memory set lookup).

    def _seen_key(self) -> str:
        return f"{SEEN_IDS_KEY_PREFIX}{self.source_name}"

    async def _hydrate_seen(self) -> None:
        """Load persisted seen-ids from Redis into the in-memory set."""
        if not (self.use_redis_seen and self._redis is not None):
            return
        try:
            members = await self._redis.smembers(self._seen_key())
            if members:
                decoded = {
                    m.decode("utf-8") if isinstance(m, bytes) else m for m in members
                }
                self._seen_ids.update(decoded)
                logger.info(
                    f"[{self.source_name}] Hydrated {len(decoded)} seen-ids from Redis"
                )
        except Exception as e:
            logger.warning(
                f"[{self.source_name}] Redis hydrate failed, starting empty: {e}"
            )

    async def _persist_seen(self, uid: str) -> None:
        """Write-through a single uid to Redis (fire-and-forget friendly)."""
        if not (self.use_redis_seen and self._redis is not None):
            return
        try:
            await self._redis.sadd(self._seen_key(), uid)
            await self._redis.expire(self._seen_key(), SEEN_IDS_TTL_SECONDS)
        except Exception as e:
            logger.debug(
                f"[{self.source_name}] Redis persist for {uid[:40]} failed: {e}"
            )

    def seen_contains(self, uid: str) -> bool:
        """Fast in-memory check. Use after _hydrate_seen() has run."""
        return uid in self._seen_ids

    async def seen_add(self, uid: str) -> None:
        """Mark uid as seen: add to memory + persist to Redis."""
        self._seen_ids.add(uid)
        await self._persist_seen(uid)

    # ── HTTP session & rate limiting ──────────────────────────────────────

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            headers = {"User-Agent": "BharatTerminal/1.0 (Financial Research Tool)"}
            self._session = aiohttp.ClientSession(timeout=timeout, headers=headers)
        return self._session

    def _check_rate_limit(self) -> float:
        """
        Check if we are within the rate limit.
        Returns seconds to sleep if rate limited, 0 otherwise.
        Appends current time to _request_times when a slot is available.
        """
        now = time.time()
        window_start = now - 60
        self._request_times = [t for t in self._request_times if t > window_start]
        if len(self._request_times) >= self.rate_limit_per_minute:
            sleep_time = 60 - (now - self._request_times[0])
            if sleep_time > 0:
                return sleep_time
        self._request_times.append(now)
        return 0

    # ── Circuit breaker ──────────────────────────────────────────────────

    def _is_circuit_open(self) -> bool:
        if not self._circuit_open:
            return False
        if self._last_failure_time and (time.time() - self._last_failure_time) > self._recovery_timeout:
            self._circuit_open = False
            self._failure_count = 0
            logger.info(f"[{self.source_name}] Circuit breaker CLOSED (recovery timeout)")
            return False
        return True

    def _record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self._failure_threshold:
            self._circuit_open = True
            logger.error(
                f"[{self.source_name}] Circuit breaker OPEN after {self._failure_count} failures"
            )

    def _record_success(self):
        self._failure_count = max(0, self._failure_count - 1)

    # ── Main loop ────────────────────────────────────────────────────────

    @abstractmethod
    async def fetch(self) -> AsyncIterator[NewsItem]:
        """Fetch news items from the source."""
        pass

    async def run(self, callback) -> None:
        """Main loop: poll source, handle circuit breaker, call callback with NewsItems."""
        logger.info(
            f"[{self.source_name}] Starting adapter "
            f"(fast={self.poll_interval_fast or self.poll_interval_seconds}s, "
            f"slow={self.poll_interval_slow or self.poll_interval_seconds}s)"
        )
        await self._hydrate_seen()
        self.last_cycle_ts = time.time()
        while True:
            # Liveness heartbeat: proves the loop itself is iterating. The
            # watchdog in main alerts when this freezes (stall ≠ crash — the
            # crash alert never fires for a silent park).
            self.last_cycle_ts = time.time()
            if self._is_circuit_open():
                logger.warning(
                    f"[{self.source_name}] Circuit open, sleeping {self._recovery_timeout}s"
                )
                await asyncio.sleep(self._recovery_timeout)
                continue

            sleep_time = self._check_rate_limit()
            if sleep_time > 0:
                logger.debug(
                    f"[{self.source_name}] Rate limited, sleeping {sleep_time:.1f}s"
                )
                await asyncio.sleep(sleep_time)
                continue

            try:
                async for item in self.fetch():
                    if item.timestamp_utc:
                        age_h = (datetime.now(timezone.utc) - item.timestamp_utc).total_seconds() / 3600
                        if age_h > _MAX_ITEM_AGE_HOURS:
                            logger.debug(
                                f"[{self.source_name}] Dropping stale item "
                                f"({age_h:.0f}h old): {item.headline[:70]}"
                            )
                            # Deliberate drop — mark seen so it is not
                            # refetched every cycle until it ages out.
                            await self.seen_add(item.id)
                            continue
                    try:
                        await asyncio.wait_for(
                            callback(item), timeout=ADAPTER_CALLBACK_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        logger.error(
                            f"[{self.source_name}] publish callback timed out "
                            f"after {ADAPTER_CALLBACK_TIMEOUT_S:.0f}s for "
                            f"{item.id} — item stays unmarked and will retry")
                        raise
                    # 2026-07-20 stall postmortem: marking seen BEFORE publish
                    # turned any downstream hang into permanent item loss
                    # (fetched, marked, never published, skipped forever).
                    # Seen is now recorded only after the callback — publish
                    # included — has succeeded; a failed/hung item is simply
                    # refetched next cycle.
                    await self.seen_add(item.id)
                self._record_success()
            except Exception as e:
                logger.error(f"[{self.source_name}] Fetch error: {e}", exc_info=True)
                self._record_failure()

            await asyncio.sleep(self._current_poll_interval())

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
