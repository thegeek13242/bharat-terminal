import asyncio
import logging
import signal
import os

from bharat_terminal.analysis.kafka_consumer import NewsKafkaConsumer
from bharat_terminal.analysis.kafka_publisher import ImpactKafkaPublisher
from bharat_terminal.analysis.pipeline import process_news_item
from bharat_terminal.analysis.batch_processor import process_batch, BATCH_MAX_SIZE, BATCH_WINDOW_S
from bharat_terminal.analysis.market_hours import is_market_hours
from bharat_terminal.types import NewsItem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    consumer  = NewsKafkaConsumer()
    publisher = ImpactKafkaPublisher()

    await consumer.start()
    await publisher.start()

    shutdown_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: shutdown_event.set())

    # ── off-hours batch buffer ────────────────────────────────────────────────
    _batch_buf: list[NewsItem] = []
    _batch_deadline: float     = 0.0        # epoch seconds

    async def _flush_batch():
        """Process and publish the current batch buffer."""
        nonlocal _batch_buf, _batch_deadline
        if not _batch_buf:
            return
        items, _batch_buf = _batch_buf, []
        _batch_deadline   = asyncio.get_event_loop().time() + BATCH_WINDOW_S
        logger.info(f"[BATCH] Flushing {len(items)} items")
        try:
            reports = await process_batch(items)
            for report in reports:
                if report is not None:
                    await publisher.publish(report)
                    logger.info(
                        f"[BATCH] Published: relevant={report.relevant}, "
                        f"companies={len(report.company_impacts)}, "
                        f"signals={len(report.trade_signals)}, "
                        f"latency={report.processing_latency_ms:.0f}ms"
                    )
        except Exception as e:
            logger.error(f"[BATCH] Flush failed: {e}", exc_info=True)

    # ── per-item real-time handler ────────────────────────────────────────────
    async def handle_news(news_item: NewsItem):
        nonlocal _batch_buf, _batch_deadline

        if is_market_hours():
            # ── real-time mode ────────────────────────────────────────────────
            # If we're transitioning from off-hours, flush any queued items first
            if _batch_buf:
                logger.info("[BATCH→RT] Market opened — flushing queued batch")
                await _flush_batch()

            logger.info(f"[RT] Processing: [{news_item.source}] {news_item.headline[:80]}")
            try:
                report = await process_news_item(news_item)
                await publisher.publish(report)
                logger.info(
                    f"[RT] Published: relevant={report.relevant}, "
                    f"companies={len(report.company_impacts)}, "
                    f"signals={len(report.trade_signals)}, "
                    f"latency={report.processing_latency_ms:.0f}ms"
                )
            except Exception as e:
                logger.error(f"[RT] Pipeline failed for {news_item.id}: {e}", exc_info=True)

        else:
            # ── batch mode ────────────────────────────────────────────────────
            _batch_buf.append(news_item)
            now = asyncio.get_event_loop().time()

            # Initialise deadline on first item after entering off-hours
            if _batch_deadline == 0.0 or _batch_deadline < now:
                _batch_deadline = now + BATCH_WINDOW_S

            logger.debug(
                f"[BATCH] Buffered {len(_batch_buf)}/{BATCH_MAX_SIZE} "
                f"(window closes in {max(0, _batch_deadline - now):.0f}s)"
            )

            if len(_batch_buf) >= BATCH_MAX_SIZE:
                logger.info(f"[BATCH] Buffer full ({BATCH_MAX_SIZE}) — flushing")
                await _flush_batch()

    # ── background batch timer ────────────────────────────────────────────────
    async def _batch_timer():
        """Periodically flush the batch buffer if the window has expired."""
        while not shutdown_event.is_set():
            await asyncio.sleep(30)          # check every 30 s
            now = asyncio.get_event_loop().time()
            if _batch_buf and now >= _batch_deadline:
                logger.info(
                    f"[BATCH] Window expired — flushing {len(_batch_buf)} items"
                )
                await _flush_batch()

    consumer_task = asyncio.create_task(consumer.consume(handle_news))
    timer_task    = asyncio.create_task(_batch_timer())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        [consumer_task, timer_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Flush any remaining items on graceful shutdown
    if _batch_buf:
        logger.info(f"[BATCH] Shutdown — flushing {len(_batch_buf)} buffered items")
        await _flush_batch()

    for task in pending:
        task.cancel()

    await consumer.stop()
    await publisher.stop()
    logger.info("Analysis service stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
