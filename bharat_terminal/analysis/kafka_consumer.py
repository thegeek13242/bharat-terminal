import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Awaitable
from aiokafka import AIOKafkaConsumer
from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_RAW_NEWS = "raw.news.in"
CONSUMER_GROUP = "analysis-workers"


class NewsKafkaConsumer:
    def __init__(self):
        self._consumer = None

    async def start(self):
        self._consumer = AIOKafkaConsumer(
            TOPIC_RAW_NEWS,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            group_id=CONSUMER_GROUP,
            auto_offset_reset="latest",
            enable_auto_commit=True,
            auto_commit_interval_ms=1000,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            max_poll_records=10,
        )
        await self._consumer.start()
        logger.info(f"Consumer connected. Listening on {TOPIC_RAW_NEWS}")

    async def consume(self, handler: Callable[[NewsItem], Awaitable[None]]):
        async for msg in self._consumer:
            try:
                data = msg.value
                data["timestamp_utc"] = datetime.fromisoformat(data["timestamp_utc"])
                news_item = NewsItem(**data)
                await handler(news_item)
            except Exception as e:
                logger.error(f"Failed to process message: {e}", exc_info=True)

    async def stop(self):
        if self._consumer:
            await self._consumer.stop()
