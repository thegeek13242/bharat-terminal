import json
import logging
import os
from datetime import datetime
from typing import Optional
from aiokafka import AIOKafkaProducer
from bharat_terminal.types import ImpactReport

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_ANALYSED = "analysed.impact.in"


class ImpactKafkaPublisher:
    def __init__(self):
        self._producer: Optional[AIOKafkaProducer] = None

    async def start(self):
        self._producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
            acks="all",
            enable_idempotence=True,
            compression_type="gzip",
        )
        await self._producer.start()

    async def publish(self, report: ImpactReport):
        data = report.model_dump()
        # Serialize datetimes
        for key in ["created_at"]:
            if key in data and isinstance(data[key], datetime):
                data[key] = data[key].isoformat()
        if "news_item" in data:
            ni = data["news_item"]
            if isinstance(ni.get("timestamp_utc"), datetime):
                ni["timestamp_utc"] = ni["timestamp_utc"].isoformat()

        await self._producer.send_and_wait(
            TOPIC_ANALYSED,
            value=data,
            key=report.news_id.encode("utf-8"),
        )
        logger.debug(f"Published ImpactReport {report.id} for news {report.news_id}")

    async def stop(self):
        if self._producer:
            await self._producer.stop()
