"""
Async token-bucket rate limiter for external API calls.
Enforces max N requests/second with random jitter to avoid thundering herd.
"""
import asyncio
import random
import time


class RateLimiter:
    """
    Usage:
        limiter = RateLimiter(rate=3.0)  # 3 requests per second
        async with limiter:
            await client.get(url)
    """

    def __init__(self, rate: float = 3.0, jitter_ms: int = 200):
        self._min_interval = 1.0 / rate
        self._jitter_ms = jitter_ms
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def __aenter__(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                wait = self._min_interval - elapsed
                if self._jitter_ms > 0:
                    wait += random.uniform(0, self._jitter_ms / 1000.0)
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        pass
