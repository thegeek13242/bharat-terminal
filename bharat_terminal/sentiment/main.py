"""
Sentiment-worker entrypoint.

  raw.sentiment.in  ──▶  classify (taxonomy)  ──▶  analysed.sentiment.in

Stateless. Idempotent under at-least-once redelivery: snapshots are keyed by
market_id, so a replay just over-writes the same key in downstream caches.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections import Counter

from bharat_terminal.sentiment.classifier import classify
from bharat_terminal.sentiment.consumer import SentimentKafkaConsumer
from bharat_terminal.sentiment.publisher import SentimentSnapshotPublisher
from bharat_terminal.sentiment.snapshot_consumer import SentimentImpactBridgeConsumer
from bharat_terminal.types import SentimentItem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    consumer = SentimentKafkaConsumer()
    publisher = SentimentSnapshotPublisher()
    # Feature 3(b): a second consumer turns classified snapshots into company
    # ImpactReports on analysed.impact.in (Polymarket → company-level signals).
    bridge = SentimentImpactBridgeConsumer()

    await consumer.start()
    await publisher.start()
    await bridge.start()

    # ── Cycle stats: log every N messages ────────────────────────────────────
    stats = Counter()
    LOG_EVERY = 25

    async def handle(item: SentimentItem) -> None:
        snap = classify(item)
        stats["seen"] += 1
        if snap is None:
            stats["skipped"] += 1
        else:
            await publisher.publish(snap)
            stats["published"] += 1
            stats[f"theme:{snap.macro_theme}"] += 1
            stats[f"dir:{snap.direction}"] += 1

        if stats["seen"] % LOG_EVERY == 0:
            top_themes = ", ".join(
                f"{k.split(':',1)[1]}={v}"
                for k, v in stats.most_common(20)
                if k.startswith("theme:")
            )
            logger.info(
                f"[SENTIMENT-WORKER] seen={stats['seen']} "
                f"published={stats['published']} skipped={stats['skipped']} "
                f"| themes: {top_themes or '(none yet)'}"
            )

    # ── Lifecycle ────────────────────────────────────────────────────────────
    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    from bharat_terminal.heartbeat import heartbeat_loop
    consumer_task = asyncio.create_task(consumer.consume(handle), name="sentiment-consume")
    bridge_task = asyncio.create_task(bridge.run(), name="sentiment-impact-bridge")
    shutdown_task = asyncio.create_task(shutdown.wait(), name="sentiment-shutdown")
    # Liveness beat for /admin/health (event-driven service, no periodic tick).
    hb_task = asyncio.create_task(heartbeat_loop("sentiment"), name="sentiment-heartbeat")

    logger.info("[SENTIMENT-WORKER] running (classifier + impact bridge)")
    done, pending = await asyncio.wait(
        [consumer_task, bridge_task, shutdown_task, hb_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    for t in pending:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    await consumer.stop()
    await publisher.stop()
    await bridge.stop()
    logger.info("[SENTIMENT-WORKER] stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        try:
            from bharat_terminal.alerts import get_monitor
            get_monitor().alert_now("SENTIMENT_CRASH", f"sentiment service crashed: {str(e)[:200]}")
        except Exception:
            pass
        raise
