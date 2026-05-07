"""Kafka consumer for raw.sentiment.in → deserialised SentimentItem stream."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Awaitable, Callable, Optional

from aiokafka import AIOKafkaConsumer

from bharat_terminal.types import SentimentItem

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_RAW_SENTIMENT = "raw.sentiment.in"
CONSUMER_GROUP = "sentiment-workers"


class SentimentKafkaConsumer:
    def __init__(self, topic: str = TOPIC_RAW_SENTIMENT, group: str = CONSUMER_GROUP):
        self._topic = topic
        self._group = group
        self._consumer: Optional[AIOKafkaConsumer] = None

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            group_id=self._group,
            auto_offset_reset="latest",
            enable_auto_commit=True,
            auto_commit_interval_ms=1000,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            max_poll_records=20,
        )
        await self._consumer.start()
        logger.info(f"[SENTIMENT-CONSUMER] connected; listening on {self._topic}")

    async def consume(
        self, handler: Callable[[SentimentItem], Awaitable[None]]
    ) -> None:
        assert self._consumer is not None, "start() before consume()"
        async for msg in self._consumer:
            try:
                data = msg.value
                # Coerce datetime fields if str
                for key in ("timestamp_utc", "end_date"):
                    v = data.get(key)
                    if isinstance(v, str):
                        try:
                            data[key] = datetime.fromisoformat(v.replace("Z", "+00:00"))
                        except ValueError:
                            data[key] = None if key == "end_date" else datetime.utcnow()
                item = SentimentItem(**data)
                await handler(item)
            except Exception as e:
                logger.error(
                    f"[SENTIMENT-CONSUMER] failed to process msg: {e}",
                    exc_info=True,
                )

    async def stop(self) -> None:
        if self._consumer:
            await self._consumer.stop()
            logger.info("[SENTIMENT-CONSUMER] stopped")
