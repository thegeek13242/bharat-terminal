"""Kafka consumer for raw.commodity.in → CommodityTick stream."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Awaitable, Callable, Optional

from aiokafka import AIOKafkaConsumer

from bharat_terminal.types import CommodityTick

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_RAW_COMMODITY = "raw.commodity.in"
CONSUMER_GROUP = "commodity-engine"


class CommodityKafkaConsumer:
    def __init__(self, topic: str = TOPIC_RAW_COMMODITY, group: str = CONSUMER_GROUP):
        self._topic = topic
        self._group = group
        self._consumer: Optional[AIOKafkaConsumer] = None

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            group_id=self._group,
            auto_offset_reset="latest",                  # ticks are useless once stale
            enable_auto_commit=True,
            auto_commit_interval_ms=1000,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            max_poll_records=200,                         # ticks are small + frequent
            fetch_max_wait_ms=50,
        )
        await self._consumer.start()
        logger.info(f"[COMMODITY-CONSUMER] connected; listening on {self._topic}")

    async def consume(
        self, handler: Callable[[CommodityTick], Awaitable[None]]
    ) -> None:
        assert self._consumer is not None, "start() before consume()"
        async for msg in self._consumer:
            try:
                data = msg.value
                ts = data.get("timestamp_utc")
                if isinstance(ts, str):
                    try:
                        data["timestamp_utc"] = datetime.fromisoformat(
                            ts.replace("Z", "+00:00")
                        )
                    except ValueError:
                        data["timestamp_utc"] = datetime.utcnow()
                tick = CommodityTick(**data)
                await handler(tick)
            except Exception as e:
                logger.error(
                    f"[COMMODITY-CONSUMER] failed to process msg: {e}",
                    exc_info=True,
                )

    async def stop(self) -> None:
        if self._consumer:
            await self._consumer.stop()
            logger.info("[COMMODITY-CONSUMER] stopped")
