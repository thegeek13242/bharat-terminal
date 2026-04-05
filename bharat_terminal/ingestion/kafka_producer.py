import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from aiokafka import AIOKafkaProducer
from bharat_terminal.types import NewsItem

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_RAW_NEWS = "raw.news.in"
TOPIC_DLQ = "raw.news.dlq"


class NewsKafkaProducer:
    def __init__(self):
        self._producer: Optional[AIOKafkaProducer] = None

    async def start(self):
        self._producer = AIOKafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            acks="all",                            # Wait for all replicas — exactly-once where possible
            enable_idempotence=True,               # Producer-side idempotence
            compression_type="gzip",
            linger_ms=50,                          # Small batching window
            request_timeout_ms=30000,
        )
        await self._producer.start()
        logger.info(f"Kafka producer connected to {KAFKA_BOOTSTRAP_SERVERS}")

    async def publish(self, item: NewsItem) -> bool:
        if not self._producer:
            raise RuntimeError("Producer not started — call await producer.start() first")

        data = item.model_dump()
        data["timestamp_utc"] = item.timestamp_utc.isoformat()

        try:
            await self._producer.send_and_wait(
                TOPIC_RAW_NEWS,
                value=data,
                key=item.source.encode("utf-8"),  # Partition by source for ordered consumption
                headers=[
                    ("source", item.source.encode()),
                    ("ingest_latency_ms", str(item.ingest_latency_ms).encode()),
                ],
            )
            logger.debug(f"Published {item.id} from {item.source} to {TOPIC_RAW_NEWS}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish {item.id}: {e}")
            await self._publish_dlq(item, str(e))
            return False

    async def _publish_dlq(self, item: NewsItem, error: str):
        """Publish failed items to the dead-letter queue for later inspection/replay."""
        if not self._producer:
            logger.critical(f"Cannot DLQ {item.id}: producer not available")
            return
        try:
            data = item.model_dump()
            data["timestamp_utc"] = item.timestamp_utc.isoformat()
            data["_dlq_error"] = error
            data["_dlq_timestamp"] = datetime.now(timezone.utc).isoformat()
            await self._producer.send_and_wait(TOPIC_DLQ, value=data)
            logger.warning(f"Sent {item.id} to DLQ: {error}")
        except Exception as e:
            logger.critical(f"DLQ publish failed for {item.id}: {e}")

    async def stop(self):
        if self._producer:
            await self._producer.stop()
            logger.info("Kafka producer stopped")
