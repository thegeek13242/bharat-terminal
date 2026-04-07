import asyncio
import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import AsyncIterator, Optional
import aiohttp
from bharat_terminal.types import NewsItem

# Hard gate: never publish items older than this
_MAX_ITEM_AGE_HOURS = 48

logger = logging.getLogger(__name__)


class BaseAdapter(ABC):
    """Base class for all news source adapters with circuit breaker and rate limiting."""

    source_name: str
    poll_interval_seconds: float = 60.0
    rate_limit_per_minute: int = 30

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_times: list = []
        self._circuit_open = False
        self._failure_count = 0
        self._failure_threshold = 5
        self._recovery_timeout = 60
        self._last_failure_time: Optional[float] = None

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

    @abstractmethod
    async def fetch(self) -> AsyncIterator[NewsItem]:
        """Fetch news items from the source."""
        pass

    async def run(self, callback) -> None:
        """Main loop: poll source, handle circuit breaker, call callback with NewsItems."""
        logger.info(f"[{self.source_name}] Starting adapter")
        while True:
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
                            continue
                    await callback(item)
                self._record_success()
            except Exception as e:
                logger.error(f"[{self.source_name}] Fetch error: {e}", exc_info=True)
                self._record_failure()

            await asyncio.sleep(self.poll_interval_seconds)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
