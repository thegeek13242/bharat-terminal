import asyncio
import logging
import signal
import os

from bharat_terminal.analysis.kafka_consumer import NewsKafkaConsumer
from bharat_terminal.analysis.kafka_publisher import ImpactKafkaPublisher
from bharat_terminal.analysis.pipeline import process_news_item
from bharat_terminal.types import NewsItem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    consumer = NewsKafkaConsumer()
    publisher = ImpactKafkaPublisher()

    await consumer.start()
    await publisher.start()

    shutdown_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: shutdown_event.set())

    async def handle_news(news_item: NewsItem):
        logger.info(f"Processing: [{news_item.source}] {news_item.headline[:80]}")
        try:
            report = await process_news_item(news_item)
            await publisher.publish(report)
            logger.info(
                f"Published ImpactReport: relevant={report.relevant}, "
                f"companies={len(report.company_impacts)}, "
                f"signals={len(report.trade_signals)}, "
                f"latency={report.processing_latency_ms:.0f}ms"
            )
        except Exception as e:
            logger.error(f"Pipeline failed for {news_item.id}: {e}", exc_info=True)

    consumer_task = asyncio.create_task(consumer.consume(handle_news))
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        [consumer_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()

    await consumer.stop()
    await publisher.stop()
    logger.info("Analysis service stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
