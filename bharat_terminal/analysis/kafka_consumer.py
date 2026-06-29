import json
import logging
import os
from datetime import datetime
from typing import Callable, Awaitable
from aiokafka import AIOKafkaConsumer
from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
# Analysis consumes the ENRICHED stream: every news item lands here carrying
# the full scraped article body (full_body) when one could be fetched, and the
# RSS summary otherwise. Ingestion guarantees exactly-once delivery to this
# topic for every item (fetched items via the ArticleFetcher; filings /
# sentiment / skip-fetch items published body-less directly). The raw
# raw.news.in topic is retained only for the DB/UI feed.
TOPIC_ENRICHED_NEWS = os.getenv("ANALYSIS_NEWS_TOPIC", "raw.news.enriched.in")
CONSUMER_GROUP = "analysis-workers"


class NewsKafkaConsumer:
    def __init__(self):
        self._consumer = None

    async def start(self):
        self._consumer = AIOKafkaConsumer(
            TOPIC_ENRICHED_NEWS,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            group_id=CONSUMER_GROUP,
            auto_offset_reset="latest",
            enable_auto_commit=True,
            auto_commit_interval_ms=1000,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            max_poll_records=10,
        )
        await self._consumer.start()
        logger.info(f"Consumer connected. Listening on {TOPIC_ENRICHED_NEWS}")

    async def consume(self, handler: Callable[[NewsItem], Awaitable[None]]):
        async for msg in self._consumer:
            try:
                data = msg.value
                data["timestamp_utc"] = datetime.fromisoformat(data["timestamp_utc"])
                news_item = NewsItem(**data)
                await handler(news_item)
            except Exception as e:
                logger.error(f"Failed to process message: {e}", exc_info=True)

    async def lag(self) -> int:
        """Total unconsumed messages across assigned partitions (best-effort).

        Used by the stall watchdog to tell a genuinely stuck consumer (lag > 0,
        not advancing) apart from a healthy but idle one (lag 0, no work)."""
        if not self._consumer:
            return 0
        total = 0
        try:
            for tp in self._consumer.assignment():
                hw = self._consumer.highwater(tp)
                if hw is None:
                    continue
                pos = await self._consumer.position(tp)
                total += max(0, hw - pos)
        except Exception as e:
            logger.debug(f"lag() unavailable: {e}")
        return total

    async def stop(self):
        if self._consumer:
            await self._consumer.stop()
