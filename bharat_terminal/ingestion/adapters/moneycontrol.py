import time
import logging
import asyncio
from datetime import datetime, timezone
from typing import AsyncIterator
import feedparser
from bharat_terminal.types import NewsItem
from .base import BaseAdapter

logger = logging.getLogger(__name__)


class MoneyControlAdapter(BaseAdapter):
    # Moneycontrol RSS feeds have been dead since 2024; replaced with Investing.com India
    source_name = "INVESTING_IN"
    poll_interval_seconds = 180.0
    poll_interval_fast = 60.0
    poll_interval_slow = 300.0
    rate_limit_per_minute = 5

    # Investing.com RSS carries NO <description> — items are headline + link
    # only — and the article pages are Cloudflare-locked, so this is a
    # headline-only macro feed. We subscribe to the categories most relevant to
    # the macro-trigger graph: economy, commodities, forex, broad market, and
    # earnings. (in.investing.com is in ingestion's _SKIP_FETCH_DOMAINS, so no
    # body-fetch attempts are made for these.)
    RSS_FEEDS = [
        "https://in.investing.com/rss/news_14.rss",    # Economy news
        "https://in.investing.com/rss/news_301.rss",   # India economy
        "https://in.investing.com/rss/news_95.rss",    # Economic indicators
        "https://in.investing.com/rss/news_11.rss",    # Commodities news
        "https://in.investing.com/rss/news_1.rss",     # Forex news
        "https://in.investing.com/rss/news_25.rss",    # Stock market news
        "https://in.investing.com/rss/news_1062.rss",  # Earnings reports & whispers
    ]

    async def fetch(self) -> AsyncIterator[NewsItem]:
        loop = asyncio.get_event_loop()
        for feed_url in self.RSS_FEEDS:
            ingest_start = time.time()
            try:
                feed = await loop.run_in_executor(None, feedparser.parse, feed_url)
                for entry in feed.entries[:10]:
                    uid = f"INV_{entry.get('id', entry.get('link', ''))}"
                    if self.seen_contains(uid):
                        continue
                    await self.seen_add(uid)
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
                        tier="tier2",
                        body_extraction_method="rss",  # headline-only source
                    )
            except Exception as e:
                logger.error(f"[MC] Error fetching {feed_url}: {e}")
