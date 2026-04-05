import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import AsyncIterator
import feedparser
from bharat_terminal.types import NewsItem
from .base import BaseAdapter

logger = logging.getLogger(__name__)


class PTIAdapter(BaseAdapter):
    """PTI news via Google News RSS (PTI byline filter — no direct subscription required for dev)."""

    source_name = "PTI"
    poll_interval_seconds = 180.0
    rate_limit_per_minute = 3

    # Google News RSS filtered for PTI stories about Indian markets
    RSS_FEEDS = [
        "https://news.google.com/rss/search?q=PTI+India+stock+market&hl=en-IN&gl=IN&ceid=IN:en",
        "https://news.google.com/rss/search?q=PTI+NSE+BSE&hl=en-IN&gl=IN&ceid=IN:en",
    ]

    def __init__(self):
        super().__init__()
        self._seen_ids: set = set()

    async def fetch(self) -> AsyncIterator[NewsItem]:
        loop = asyncio.get_event_loop()
        for feed_url in self.RSS_FEEDS:
            ingest_start = time.time()
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, feed_url)
                for entry in feed.entries[:10]:
                    # Filter only PTI byline articles where possible
                    source_title = ""
                    if hasattr(entry, "source") and isinstance(entry.source, dict):
                        source_title = entry.source.get("title", "")
                    # Accept all entries from this already-filtered feed
                    _ = source_title  # reserved for stricter filtering if needed

                    uid = f"PTI_{entry.get('id', entry.get('link', ''))}"
                    if uid in self._seen_ids:
                        continue
                    self._seen_ids.add(uid)
                    ts = datetime.now(timezone.utc)
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        ts = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    ingest_latency = (time.time() - ingest_start) * 1000
                    yield NewsItem(
                        id=uid,
                        source=self.source_name,
                        timestamp_utc=ts,
                        headline=entry.get("title", ""),
                        body=entry.get("summary", ""),
                        url=entry.get("link", feed_url),
                        raw_html=None,
                        ingest_latency_ms=ingest_latency,
                        category="NEWS",
                        symbols_mentioned=[],
                    )
            except Exception as e:
                logger.error(f"[PTI] Error fetching {feed_url}: {e}")
