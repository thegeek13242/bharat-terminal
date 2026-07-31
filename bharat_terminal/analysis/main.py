import asyncio
import logging
import os
import signal
import time

from bharat_terminal.analysis.kafka_consumer import NewsKafkaConsumer
from bharat_terminal.analysis.kafka_publisher import ImpactKafkaPublisher
from bharat_terminal.analysis.pipeline import process_news_item
from bharat_terminal.analysis.batch_processor import process_batch, BATCH_MAX_SIZE, BATCH_WINDOW_S
from bharat_terminal.analysis.market_hours import is_market_hours
from bharat_terminal.analysis.llm_logger import begin_task_cost, current_task_cost
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

# Bounded concurrency for market-hours real-time items. Sequential processing
# (~20-35s/item) made bursty filing clusters queue for minutes; N items in
# flight cuts the tail without unbounded LLM fan-out. Off-hours items keep the
# original sequential/batch behaviour. Backpressure: the consume loop blocks on
# the semaphore when all slots are busy (aiokafka heartbeats run in the
# background, and max_poll_interval_ms=900s far exceeds the item timeout).
RT_CONCURRENCY = int(os.getenv("RT_CONCURRENCY", "3"))

# C1a — one delayed retry for items whose stage-3 LLM call failed (marker:
# router_meta.stage3_error, set by stage3_impact's exception handler). The
# failed report still publishes (empty impacts), then the item re-runs once
# after the delay: Vertex 429 storms clear in minutes and MALFORMED_FUNCTION_
# CALL flakes rarely repeat. In-process only — a restart during the delay
# loses the retry, which beats today's 100% permanent loss (2.2% of relevant
# reports, incl. results-day commentary press releases).
STAGE3_RETRY = os.getenv("STAGE3_RETRY", "1") == "1"
STAGE3_RETRY_DELAY_S = float(os.getenv("STAGE3_RETRY_DELAY_S", "240"))


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

    # ── per-item real-time processing ─────────────────────────────────────────
    _rt_sem = asyncio.Semaphore(RT_CONCURRENCY)
    _rt_tasks: set[asyncio.Task] = set()
    _stage3_retried: set[str] = set()

    async def _retry_stage3_later(news_item: NewsItem):
        await asyncio.sleep(STAGE3_RETRY_DELAY_S)
        logger.info(f"[RT] Stage-3 retry starting: {news_item.id}")
        await _rt_sem.acquire()
        task = asyncio.create_task(_spawn_rt(news_item))
        _rt_tasks.add(task)
        task.add_done_callback(_rt_tasks.discard)

    async def _process_rt(news_item: NewsItem):
        """Full per-item pipeline run. Safe to run concurrently: cost is
        attributed via a task-local bucket (see llm_logger), publishes are
        independent per item, and offsets auto-commit regardless."""
        logger.info(f"[RT] Processing: [{news_item.source}] {news_item.headline[:80]}")
        begin_task_cost()
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
            err = (report.router_meta or {}).get("stage3_error")
            if STAGE3_RETRY and err and news_item.id not in _stage3_retried:
                if len(_stage3_retried) > 4096:   # bound; re-retry of ancient
                    _stage3_retried.clear()       # ids is harmless
                _stage3_retried.add(news_item.id)
                logger.warning(
                    f"[RT] stage3_error on {news_item.id} — one retry in "
                    f"{STAGE3_RETRY_DELAY_S:.0f}s: {err[:120]}")
                rt = asyncio.create_task(_retry_stage3_later(news_item))
                _rt_tasks.add(rt)
                rt.add_done_callback(_rt_tasks.discard)
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
            # Task-local bucket: correct even with concurrent items in flight.
            item_cost = current_task_cost() or 0.0
            if item_cost > ITEM_COST_BUDGET_USD:
                get_monitor().record_breach(
                    "ITEM_COST_BUDGET",
                    f"{news_item.id} cost ${item_cost:.3f} > budget ${ITEM_COST_BUDGET_USD:.2f}",
                )

    async def _spawn_rt(news_item: NewsItem):
        try:
            await _process_rt(news_item)
        finally:
            _rt_sem.release()

    # ── per-item handler ──────────────────────────────────────────────────────
    async def handle_news(news_item: NewsItem):
        nonlocal _batch_buf, _batch_deadline

        # Exchange filings always take the full per-item pipeline, even off
        # hours: results PDFs land in the evening, and the batch-triage path
        # produces no grounded filing summary (and shallow impacts). Filings
        # are the product's tier-1 source — accuracy over batching.
        filings_full = os.getenv("FILINGS_ALWAYS_FULL_PIPELINE", "1") == "1"
        is_filing = news_item.source in ("NSE_FILINGS", "BSE_FILINGS")

        if is_market_hours() or (filings_full and is_filing):
            # ── real-time mode ────────────────────────────────────────────────
            # If we're transitioning from off-hours, flush any queued items first
            if _batch_buf:
                logger.info("[BATCH→RT] Market opened — flushing queued batch")
                await _flush_batch()

            # Bounded concurrency for ALL real-time items (market hours and
            # off-hours filings alike — results-evening filing bursts were the
            # worst measured queueing). Blocks here (natural backpressure) only
            # when all RT_CONCURRENCY slots are busy.
            await _rt_sem.acquire()
            task = asyncio.create_task(_spawn_rt(news_item))
            _rt_tasks.add(task)
            task.add_done_callback(_rt_tasks.discard)

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
                # Stall = nothing published while work is PENDING. The old
                # `or is_market_hours()` branch assumed steady market-hours
                # flow — true in the news-firehose era, false since the
                # primary-source doctrine (2026-07-19) made filings-only flow
                # bursty: quiet pre-open/midday stretches are normal, and that
                # branch paged CRITICAL on every 15-min lull (2026-07-22
                # phantom-stall postmortem; upstream-dead detection now
                # belongs to ingestion's per-adapter liveness watchdog).
                if idle_s >= STALL_NO_PUBLISH_MIN * 60 and lag >= STALL_LAG_THRESHOLD:
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

    # Drain in-flight real-time items on graceful shutdown
    if _rt_tasks:
        logger.info(f"[RT] Shutdown — waiting for {len(_rt_tasks)} in-flight item(s)")
        await asyncio.wait(_rt_tasks, timeout=ITEM_PROCESSING_TIMEOUT_S)

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
