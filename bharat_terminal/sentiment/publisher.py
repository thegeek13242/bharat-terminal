"""Kafka publisher for analysed.sentiment.in (SentimentSnapshot stream)."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from aiokafka import AIOKafkaProducer

from bharat_terminal.types import SentimentSnapshot

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_ANALYSED_SENTIMENT = "analysed.sentiment.in"


class SentimentSnapshotPublisher:
    def __init__(self, topic: str = TOPIC_ANALYSED_SENTIMENT):
        self._topic = topic
        self._producer: Optional[AIOKafkaProducer] = None

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
            acks="all",
            enable_idempotence=True,
            compression_type="gzip",
        )
        await self._producer.start()
        logger.info(f"[SENTIMENT-PUBLISHER] connected; producing to {self._topic}")

    async def publish(self, snap: SentimentSnapshot) -> None:
        assert self._producer is not None, "start() before publish()"
        data = snap.model_dump()
        # JSON-friendly datetimes
        for key in ("timestamp_utc", "created_at"):
            v = data.get(key)
            if isinstance(v, datetime):
                data[key] = v.isoformat()

        headers = [
            ("source", snap.source.encode("utf-8")),
            ("theme", snap.macro_theme.encode("utf-8")),
            ("direction", snap.direction.encode("utf-8")),
            ("magnitude", str(snap.magnitude).encode("utf-8")),
        ]

        await self._producer.send_and_wait(
            self._topic,
            value=data,
            key=snap.market_id.encode("utf-8"),
            headers=headers,
        )
        logger.debug(
            f"[SENTIMENT-PUBLISHER] published {snap.macro_theme} "
            f"({snap.direction} m{snap.magnitude}) for {snap.market_id}"
        )

    async def stop(self) -> None:
        if self._producer:
            await self._producer.stop()
            logger.info("[SENTIMENT-PUBLISHER] stopped")
