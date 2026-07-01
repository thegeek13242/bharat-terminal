import asyncio
import logging
import signal
import time

from bharat_terminal.analysis.kafka_consumer import NewsKafkaConsumer
from bharat_terminal.analysis.kafka_publisher import ImpactKafkaPublisher
from bharat_terminal.analysis.pipeline import process_news_item
from bharat_terminal.analysis.batch_processor import process_batch, BATCH_MAX_SIZE, BATCH_WINDOW_S
from bharat_terminal.analysis.market_hours import is_market_hours
from bharat_terminal.analysis.llm_logger import get_llm_logger
from bharat_terminal.analysis.guardrails import (
    get_monitor,
    ITEM_PROCESSING_TIMEOUT_S,
    BATCH_PROCESSING_TIMEOUT_S,
    ITEM_COST_BUDGET_USD,
    WATCHDOG_INTERVAL_S,
    STALL_NO_PUBLISH_MIN,
    STALL_LAG_THRESHOLD,
    CONSUMER_LAG_ALERT,
)
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
    # Watchdog state: monotonic timestamp of the last successful publish, used to
    # detect a stalled consumer (no output while work is pending).
    _state = {"last_publish": time.monotonic()}

    async def _flush_batch():
        """Process and publish the current batch buffer."""
        nonlocal _batch_buf, _batch_deadline
        if not _batch_buf:
            return
        items, _batch_buf = _batch_buf, []
        _batch_deadline   = asyncio.get_event_loop().time() + BATCH_WINDOW_S
        logger.info(f"[BATCH] Flushing {len(items)} items")
        try:
            # Hard time budget: a batch can never block the consumer indefinitely.
            reports = await asyncio.wait_for(
                process_batch(items), timeout=BATCH_PROCESSING_TIMEOUT_S
            )
            for report in reports:
                if report is not None:
                    await publisher.publish(report)
                    _state["last_publish"] = time.monotonic()
                    logger.info(
                        f"[BATCH] Published: relevant={report.relevant}, "
                        f"companies={len(report.company_impacts)}, "
                        f"signals={len(report.trade_signals)}, "
                        f"latency={report.processing_latency_ms:.0f}ms"
                    )
        except asyncio.TimeoutError:
            get_monitor().record_breach(
                "BATCH_TIMEOUT",
                f"batch of {len(items)} exceeded {BATCH_PROCESSING_TIMEOUT_S:.0f}s budget",
            )
            logger.error(
                f"[BATCH] Timed out (> {BATCH_PROCESSING_TIMEOUT_S:.0f}s) on {len(items)} "
                f"items — dropping batch to keep the consumer moving"
            )
        except Exception as e:
            get_monitor().record_breach("PIPELINE_EXCEPTION", f"batch flush failed: {e}")
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
            cost_before = get_llm_logger().total_cost_usd
            try:
                # Hard time budget per item: a single stuck item can never block
                # the consumer (which previously froze the whole feed for hours).
                report = await asyncio.wait_for(
                    process_news_item(news_item), timeout=ITEM_PROCESSING_TIMEOUT_S
                )
                await publisher.publish(report)
                _state["last_publish"] = time.monotonic()
                logger.info(
                    f"[RT] Published: relevant={report.relevant}, "
                    f"companies={len(report.company_impacts)}, "
                    f"signals={len(report.trade_signals)}, "
                    f"latency={report.processing_latency_ms:.0f}ms"
                )
            except asyncio.TimeoutError:
                get_monitor().record_breach(
                    "ITEM_TIMEOUT",
                    f"{news_item.id} exceeded {ITEM_PROCESSING_TIMEOUT_S:.0f}s budget",
                )
                logger.error(
                    f"[RT] Item timed out (> {ITEM_PROCESSING_TIMEOUT_S:.0f}s), skipping: "
                    f"{news_item.id}"
                )
            except Exception as e:
                get_monitor().record_breach(
                    "PIPELINE_EXCEPTION", f"{news_item.id}: {e}")
                logger.error(f"[RT] Pipeline failed for {news_item.id}: {e}", exc_info=True)
            finally:
                # RT items process one at a time, so this delta is the item's cost.
                item_cost = get_llm_logger().total_cost_usd - cost_before
                if item_cost > ITEM_COST_BUDGET_USD:
                    get_monitor().record_breach(
                        "ITEM_COST_BUDGET",
                        f"{news_item.id} cost ${item_cost:.3f} > budget ${ITEM_COST_BUDGET_USD:.2f}",
                    )

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

    # ── critical-condition watchdog ───────────────────────────────────────────
    async def _watchdog():
        """Alert on a stalled consumer or a dangerously high backlog — the
        failure class that previously froze the feed unnoticed for hours."""
        while not shutdown_event.is_set():
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            try:
                lag = await consumer.lag()
                idle_s = time.monotonic() - _state["last_publish"]
                # Stall: nothing published for too long while work is pending
                # (or during market hours, when steady flow is expected).
                if idle_s >= STALL_NO_PUBLISH_MIN * 60 and (
                    is_market_hours() or lag >= STALL_LAG_THRESHOLD
                ):
                    get_monitor().alert_now(
                        "PIPELINE_STALL",
                        f"no analysis output for {idle_s / 60:.0f}min with {lag} "
                        f"messages pending — the consumer appears stuck",
                    )
                # Backlog: keeping up poorly; risk of dropping news before the
                # enriched topic's 48h retention evicts it.
                elif lag >= CONSUMER_LAG_ALERT:
                    get_monitor().alert_now(
                        "CONSUMER_LAG_HIGH",
                        f"{lag} messages behind (threshold {CONSUMER_LAG_ALERT}) — "
                        f"analysis cannot keep up; data-loss risk as retention expires",
                    )
            except Exception as e:
                logger.debug(f"watchdog check failed: {e}")

    consumer_task = asyncio.create_task(consumer.consume(handle_news))
    timer_task    = asyncio.create_task(_batch_timer())
    watchdog_task = asyncio.create_task(_watchdog())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        [consumer_task, timer_task, watchdog_task, shutdown_task],
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
    try:
        asyncio.run(main())
    except Exception as e:
        try:
            from bharat_terminal.alerts import get_monitor
            get_monitor().alert_now("ANALYSIS_CRASH", f"analysis service crashed: {str(e)[:200]}")
        except Exception:
            pass
        raise
