import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from aiokafka import AIOKafkaProducer
from bharat_terminal.types import NewsItem, SentimentItem

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_RAW_NEWS = "raw.news.in"
TOPIC_ENRICHED_NEWS = "raw.news.enriched.in"   # Articles with full_body extracted
TOPIC_SENTIMENT = "raw.sentiment.in"           # Prediction-market probabilities
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

    def _serialize(self, item: NewsItem) -> dict:
        data = item.model_dump()
        data["timestamp_utc"] = item.timestamp_utc.isoformat()
        return data

    async def publish(self, item: NewsItem) -> bool:
        """Publish a raw news item (headline + RSS summary) to raw.news.in."""
        if not self._producer:
            raise RuntimeError("Producer not started — call await producer.start() first")

        data = self._serialize(item)

        try:
            await self._producer.send_and_wait(
                TOPIC_RAW_NEWS,
                value=data,
                key=item.source.encode("utf-8"),  # Partition by source for ordered consumption
                headers=[
                    ("source", item.source.encode()),
                    ("tier", item.tier.encode()),
                    ("ingest_latency_ms", str(item.ingest_latency_ms).encode()),
                ],
            )
            logger.debug(f"Published {item.id} from {item.source} to {TOPIC_RAW_NEWS}")
            return True
        except Exception as e:
            logger.error(f"Failed to publish {item.id}: {e}")
            await self._publish_dlq(item, str(e))
            return False

    async def publish_enriched(self, item: NewsItem) -> bool:
        """
        Publish an enriched news item (with full_body) to raw.news.enriched.in.

        Downstream analysis pipelines that need full text subscribe to this
        topic; signal generators that only need headline/tier subscribe to
        raw.news.in.
        """
        if not self._producer:
            raise RuntimeError("Producer not started")

        data = self._serialize(item)
        try:
            await self._producer.send_and_wait(
                TOPIC_ENRICHED_NEWS,
                value=data,
                key=item.source.encode("utf-8"),
                headers=[
                    ("source", item.source.encode()),
                    ("tier", item.tier.encode()),
                    ("body_fetch_status", item.body_fetch_status.encode()),
                    (
                        "body_word_count",
                        str(item.body_word_count or 0).encode(),
                    ),
                ],
            )
            logger.debug(
                f"Enriched {item.id} ({item.body_fetch_status}, "
                f"{item.body_word_count or 0} words) -> {TOPIC_ENRICHED_NEWS}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to publish enriched {item.id}: {e}")
            await self._publish_dlq(item, f"enriched: {e}")
            return False

    async def publish_sentiment(self, item: SentimentItem) -> bool:
        """Publish a prediction-market probability observation to raw.sentiment.in."""
        if not self._producer:
            raise RuntimeError("Producer not started")

        data = item.model_dump()
        data["timestamp_utc"] = item.timestamp_utc.isoformat()
        if item.end_date:
            data["end_date"] = item.end_date.isoformat()

        try:
            delta = item.delta_1h if item.delta_1h is not None else 0.0
            await self._producer.send_and_wait(
                TOPIC_SENTIMENT,
                value=data,
                key=item.market_id.encode("utf-8"),   # Partition by market for ordered delta stream
                headers=[
                    ("source", item.source.encode()),
                    ("market_id", item.market_id.encode()),
                    ("probability", f"{item.probability:.4f}".encode()),
                    ("delta_1h", f"{delta:+.4f}".encode()),
                ],
            )
            logger.debug(
                f"Sentiment [{item.source}] {item.market_id} p={item.probability:.3f} "
                f"Δ1h={delta:+.3f} -> {TOPIC_SENTIMENT}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to publish sentiment {item.market_id}: {e}")
            return False

    async def _publish_dlq(self, item: NewsItem, error: str):
        """Publish failed items to the dead-letter queue for later inspection/replay."""
        if not self._producer:
            logger.critical(f"Cannot DLQ {item.id}: producer not available")
            return
        try:
            data = self._serialize(item)
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
