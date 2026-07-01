"""
Async article body fetcher.

Takes a URL, returns (full_text, word_count, extraction_method).

Extraction cascade:
  1. trafilatura       — best-in-class boilerplate removal for news sites
  2. readability-lxml  — fallback for pages trafilatura chokes on

Features:
  - Worker pool (default 10) consuming an asyncio.Queue of (NewsItem, future)
  - Per-domain semaphore (max 2 concurrent per domain)
  - Rotating User-Agent pool
  - Redis cache keyed by URL fingerprint (24h TTL)
  - Timeouts: 15s connect/read, 30s total

The fetcher publishes an ENRICHED copy of the NewsItem to Kafka (topic:
raw.news.enriched.in) once the body is extracted. Tier 1 items can be
published immediately with body_fetch_status="pending", letting downstream
consumers start work while we fetch in parallel.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections import defaultdict
from typing import Awaitable, Callable, Optional, Tuple
from urllib.parse import urlparse

import httpx
import redis.asyncio as aioredis

from bharat_terminal.ingestion.dedup import url_fingerprint
from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

# Rotating User-Agent pool — real browser UAs to avoid trivial bot blocks
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
]

CACHE_KEY_PREFIX = "news:article:body:"
CACHE_TTL_SECONDS = 24 * 3600
PER_DOMAIN_CONCURRENCY = 2
DEFAULT_WORKER_COUNT = 10
FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=5.0)
MIN_BODY_CHARS = 200  # Anything shorter than this is treated as extraction failure


def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    """Extract article body using trafilatura. Returns None on failure."""
    try:
        import trafilatura
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=False,
            deduplicate=True,
            favor_recall=False,
        )
        if text and len(text) >= MIN_BODY_CHARS:
            return text
    except Exception as e:
        logger.debug(f"trafilatura failed for {url}: {e}")
    return None


def _extract_with_readability(html: str, url: str) -> Optional[str]:
    """Fallback extraction using readability-lxml."""
    try:
        from readability import Document
        from bs4 import BeautifulSoup

        doc = Document(html)
        summary_html = doc.summary(html_partial=True)
        text = BeautifulSoup(summary_html, "lxml").get_text(separator="\n", strip=True)
        if text and len(text) >= MIN_BODY_CHARS:
            return text
    except Exception as e:
        logger.debug(f"readability failed for {url}: {e}")
    return None


def extract_article_text(html: str, url: str) -> Tuple[Optional[str], Optional[str]]:
    """Run the extraction cascade. Returns (text, method) or (None, None)."""
    text = _extract_with_trafilatura(html, url)
    if text:
        return text, "trafilatura"
    text = _extract_with_readability(html, url)
    if text:
        return text, "readability"
    return None, None


class ArticleFetcher:
    """Async article body fetcher with worker pool and per-domain rate limiting."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        publish_callback: Callable[[NewsItem], Awaitable[bool]],
        worker_count: int = DEFAULT_WORKER_COUNT,
    ):
        self._redis = redis_client
        self._publish = publish_callback
        self._worker_count = worker_count
        self._queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=1000)
        self._domain_semaphores: dict = defaultdict(
            lambda: asyncio.Semaphore(PER_DOMAIN_CONCURRENCY)
        )
        self._workers: list = []
        self._client: Optional[httpx.AsyncClient] = None
        self._running = False

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=FETCH_TIMEOUT,
            follow_redirects=True,
            http2=False,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(i)) for i in range(self._worker_count)
        ]
        logger.info(f"ArticleFetcher started with {self._worker_count} workers")

    async def stop(self) -> None:
        self._running = False
        for _ in self._workers:
            await self._queue.put(None)  # sentinel
        await asyncio.gather(*self._workers, return_exceptions=True)
        if self._client:
            await self._client.aclose()
        logger.info("ArticleFetcher stopped")

    async def enqueue(self, item: NewsItem) -> None:
        """Enqueue an item for body fetching. Non-blocking if queue has space.

        If the queue is saturated we must NOT silently drop the item: analysis
        consumes the enriched topic, so a dropped item would never be analysed.
        Instead publish a body-less enriched copy so it is still processed on
        its headline + RSS summary.
        """
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning(
                f"ArticleFetcher queue full; publishing {item.url[:80]} body-less"
            )
            fallback = item.model_copy(update={
                "full_body": None,
                "body_word_count": 0,
                "body_fetch_status": "skipped",
                "body_extraction_method": None,
            })
            try:
                await self._publish(fallback)
            except Exception as e:
                logger.error(
                    f"Failed to publish body-less fallback for {item.id}: {e}"
                )

    async def _worker(self, wid: int) -> None:
        logger.debug(f"ArticleFetcher worker {wid} started")
        while self._running:
            item = await self._queue.get()
            if item is None:
                break
            try:
                await self._process(item)
            except Exception as e:
                logger.error(
                    f"Worker {wid} unhandled error for {item.url[:80]}: {e}",
                    exc_info=True,
                )
            finally:
                self._queue.task_done()

    async def _process(self, item: NewsItem) -> None:
        # Cache check
        cache_key = f"{CACHE_KEY_PREFIX}{url_fingerprint(item.url)}"
        cached = await self._redis.get(cache_key)
        if cached:
            enriched = item.model_copy(update={
                "full_body": cached.decode("utf-8") if isinstance(cached, bytes) else cached,
                "body_word_count": len((cached.decode("utf-8") if isinstance(cached, bytes) else cached).split()),
                "body_fetch_status": "cached",
                "body_extraction_method": "cache",
            })
            await self._publish(enriched)
            return

        domain = urlparse(item.url).netloc
        sem = self._domain_semaphores[domain]
        async with sem:
            text, method, status = await self._fetch_and_extract(item.url)

        if text:
            await self._redis.set(cache_key, text, ex=CACHE_TTL_SECONDS)

        enriched = item.model_copy(update={
            "full_body": text,
            "body_word_count": len(text.split()) if text else 0,
            "body_fetch_status": status,
            "body_extraction_method": method,
        })
        await self._publish(enriched)

    async def _fetch_and_extract(
        self, url: str
    ) -> Tuple[Optional[str], Optional[str], str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            # NOTE: no "br" — would require the `brotli` package; gzip/deflate are decoded natively by httpx
            "Accept-Encoding": "gzip, deflate",
        }
        try:
            resp = await self._client.get(url, headers=headers)
            if resp.status_code >= 400:
                logger.debug(f"fetch {url[:80]} -> HTTP {resp.status_code}")
                return None, None, "failed"
            html = resp.text
        except (httpx.RequestError, httpx.HTTPError) as e:
            logger.debug(f"fetch {url[:80]} -> {type(e).__name__}: {e}")
            return None, None, "failed"

        # Extraction is CPU-bound; run in default executor
        loop = asyncio.get_event_loop()
        text, method = await loop.run_in_executor(
            None, extract_article_text, html, url
        )
        if text:
            return text, method, "success"
        return None, None, "failed"
