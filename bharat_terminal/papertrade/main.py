"""Paper-trading service entrypoint — internal automated-trading pilot.

WHY a separate service instead of a hook inside analysis: the analysis
pipeline must stay a pure producer of ImpactReports; trading decisions get
their own consumer group ("papertrade") on analysed.impact.in so they can be
restarted, replayed or killed without touching the analysis path, and so a
trading bug can never stall the product pipeline. auto_offset_reset="latest"
is deliberate: on a fresh group there is nothing to catch up on — trading
yesterday's news at today's prices would fabricate an edge.

Structure mirrors analysis/main.py: one consume loop plus background loops
(60s mark-to-market + exits, 300s equity snapshot; both market-hours-only —
LTPs don't move outside the session) and graceful SIGTERM shutdown.
"""
import asyncio
import json
import logging
import os
import signal

from aiokafka import AIOKafkaConsumer

from bharat_terminal.analysis.market_hours import is_market_hours
from bharat_terminal.papertrade import marks
from bharat_terminal.papertrade.engine import PaperEngine
from bharat_terminal.papertrade.rules import RulesEngine
from bharat_terminal.types import ImpactReport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger(__name__)

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_ANALYSED = "analysed.impact.in"
CONSUMER_GROUP = "papertrade"

MTM_INTERVAL_S = 60
SNAPSHOT_INTERVAL_S = 300


async def main():
    engine = await PaperEngine.create()
    rules = RulesEngine(engine.pool, marks.get_fno_universe)

    # Warm the F&O universe before consuming: a cold fetch is a ~35MB download
    # from a slow CDN (up to minutes) and must not happen inside a trade
    # decision. Failure is non-fatal — symbols then default to CASH intraday.
    try:
        fno = await marks.get_fno_universe()
        logger.info(f"F&O universe warmed: {len(fno)} symbols")
    except Exception as e:
        logger.warning(f"F&O universe warm-up failed (non-fatal): {e}")

    consumer = AIOKafkaConsumer(
        TOPIC_ANALYSED,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=CONSUMER_GROUP,
        auto_offset_reset="latest",
        enable_auto_commit=True,
        auto_commit_interval_ms=1000,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    )
    await consumer.start()
    logger.info(f"Consumer connected. Listening on {TOPIC_ANALYSED} "
                f"(group {CONSUMER_GROUP})")

    shutdown_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # ── per-report handler ────────────────────────────────────────────────────
    async def handle_report(data: dict):
        try:
            report = ImpactReport(**data)
        except Exception as e:
            logger.error(f"Malformed ImpactReport skipped: {e}")
            return
        try:
            intents = await rules.evaluate_report(report)
        except Exception as e:
            logger.error(f"rules.evaluate_report failed for report "
                         f"{data.get('id')}: {e}", exc_info=True)
            return
        if not intents:
            return

        live = [i for i in intents if i.rejected_reason is None]
        market_open = is_market_hours()
        ltps: dict[str, float] = {}
        if live and market_open:
            try:
                ltps = await marks.get_ltps({i.symbol for i in live})
            except Exception as e:
                logger.warning(f"LTP fetch failed — intents fill as no_ltp: {e}")

        for intent in intents:
            try:
                if intent.rejected_reason:
                    # e.g. stale_event: recorded, never silently dropped.
                    await engine.record_rejected(intent, intent.rejected_reason)
                elif not market_open:
                    # Off-session (most results filings land after 15:30 IST):
                    # the pre-news LTP would be a fabricated fill — queue as
                    # PENDING; the MTM loop fills it at the first LTP after
                    # the next open.
                    await engine.queue_position(intent)
                else:
                    await engine.open_position(intent, ltps.get(intent.symbol))
            except Exception as e:
                logger.error(f"engine action failed for {intent.strategy}/"
                             f"{intent.symbol}: {e}", exc_info=True)

    # ── loops ─────────────────────────────────────────────────────────────────
    async def consume_loop():
        async for msg in consumer:
            try:
                await handle_report(msg.value)
            except Exception as e:
                logger.error(f"Failed to process message: {e}", exc_info=True)
            if shutdown_event.is_set():
                break

    async def mtm_loop():
        """Fill queued PENDING signals, then mark OPEN positions and apply
        exit rules (stop / holding period / the >=15:15 IST cash-short
        squareoff) every 60s during market hours."""
        while not shutdown_event.is_set():
            await asyncio.sleep(MTM_INTERVAL_S)
            if not is_market_hours():
                continue
            try:
                symbols = await engine.open_symbols()
                pending = await engine.pending_symbols()
                todo = symbols | pending
                if not todo:
                    continue
                ltps = await marks.get_ltps(todo)
                filled = (await engine.fill_pending(ltps)) if pending else 0
                marked = await engine.mark_open_positions(ltps)
                closed = await engine.run_exits(ltps)
                logger.info(f"[MTM] open={len(symbols)} pending={len(pending)} "
                            f"filled={filled} marked={marked} closed={closed}")
            except Exception as e:
                logger.error(f"[MTM] cycle failed: {e}", exc_info=True)

    async def snapshot_loop():
        while not shutdown_event.is_set():
            await asyncio.sleep(SNAPSHOT_INTERVAL_S)
            if not is_market_hours():
                continue
            try:
                await engine.snapshot_equity()
            except Exception as e:
                logger.error(f"[EQ] snapshot failed: {e}", exc_info=True)

    consumer_task = asyncio.create_task(consume_loop())
    mtm_task = asyncio.create_task(mtm_loop())
    snapshot_task = asyncio.create_task(snapshot_loop())
    shutdown_task = asyncio.create_task(shutdown_event.wait())

    done, pending = await asyncio.wait(
        [consumer_task, mtm_task, snapshot_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()

    await consumer.stop()
    await marks.close()
    await engine.close()
    logger.info("Paper-trading service stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
