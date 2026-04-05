"""
Kafka consumer that relays ImpactReport events to WebSocket clients.
Runs as a background task in the FastAPI app.
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from aiokafka import AIOKafkaConsumer
from bharat_terminal.api.ws_manager import manager

logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_ANALYSED = "analysed.impact.in"

_consumer_task: asyncio.Task = None


async def start_kafka_relay():
    """Start consuming ImpactReports and broadcast to WebSocket clients."""
    global _consumer_task
    _consumer_task = asyncio.create_task(_relay_loop())
    logger.info("Kafka relay started")


async def stop_kafka_relay():
    global _consumer_task
    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass


async def _relay_loop():
    consumer = AIOKafkaConsumer(
        TOPIC_ANALYSED,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="frontend-consumers",
        auto_offset_reset="latest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        enable_auto_commit=True,
    )

    try:
        await consumer.start()
        logger.info(f"Relay consuming from {TOPIC_ANALYSED}")

        async for msg in consumer:
            try:
                report = msg.value

                # Only relay relevant news
                if not report.get("relevant", True):
                    continue

                event = {
                    "type": "impact_report",
                    "data": report,
                    "timestamp": datetime.utcnow().isoformat(),
                }

                await manager.broadcast(event)

            except Exception as e:
                logger.error(f"Relay error: {e}", exc_info=True)

    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()
