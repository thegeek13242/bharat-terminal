"""
Commodity-engine entrypoint.

  raw.commodity.in
        │
        ▼
  CommodityShockDetector  (sliding-window z-score)
        │ shock event
        ▼
  signal_generator.build_impact_report
        │ ImpactReport
        ├──▶ analysed.impact.in           (UI / WebSocket relay)
        └──▶ analysed.commodity_shock.in  (audit log)

Stateless across restarts: tick history rebuilds from new ticks within
~1 baseline window. Idempotent under at-least-once redelivery (the
detector dedupes via cooldown).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections import Counter

from bharat_terminal.commodity.consumer import CommodityKafkaConsumer
from bharat_terminal.commodity.detector import CommodityShockDetector
from bharat_terminal.commodity.publisher import CommodityEnginePublisher
from bharat_terminal.commodity.signal_generator import build_impact_report
from bharat_terminal.types import CommodityTick

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


async def main() -> None:
    consumer = CommodityKafkaConsumer()
    publisher = CommodityEnginePublisher()
    detector = CommodityShockDetector(
        window_minutes=_env_int("COMMODITY_WINDOW_MIN", 10),
        pct_threshold=_env_float("COMMODITY_PCT_THRESHOLD", 0.012),
        z_threshold=_env_float("COMMODITY_Z_THRESHOLD", 3.0),
        baseline_window_minutes=_env_int("COMMODITY_BASELINE_MIN", 60),
        cooldown_seconds=_env_float("COMMODITY_COOLDOWN_SEC", 300.0),
    )

    await consumer.start()
    await publisher.start()

    stats = Counter()
    LOG_EVERY_TICKS = 500

    async def handle(tick: CommodityTick) -> None:
        stats["ticks"] += 1
        stats[f"tick:{tick.commodity}"] += 1

        shock = detector.evaluate(tick)
        if shock is None:
            if stats["ticks"] % LOG_EVERY_TICKS == 0:
                top = ", ".join(
                    f"{k.split(':',1)[1]}={v}"
                    for k, v in stats.most_common(20)
                    if k.startswith("tick:")
                )
                logger.info(
                    f"[COMMODITY-ENGINE] ticks={stats['ticks']} "
                    f"shocks={stats['shocks']} | per-commodity: {top}"
                )
            return

        stats["shocks"] += 1
        stats[f"shock:{shock.commodity}:{shock.direction}"] += 1

        # Fire shock + signals in parallel — independent topics.
        # build_impact_report is async (hits the KB for calibrated betas).
        try:
            report = await build_impact_report(shock)
            await asyncio.gather(
                publisher.publish_shock(shock),
                publisher.publish_impact(report),
            )
            logger.info(
                f"[COMMODITY-ENGINE] EMITTED shock+impact "
                f"{shock.commodity} {shock.direction} "
                f"z={shock.z_score:+.2f} → "
                f"{len(report.trade_signals)} signals "
                f"({len(report.company_impacts)} impacts)"
            )
        except Exception as e:
            logger.error(
                f"[COMMODITY-ENGINE] publish error for {shock.commodity}: {e}",
                exc_info=True,
            )

    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    consumer_task = asyncio.create_task(consumer.consume(handle), name="commodity-consume")
    shutdown_task = asyncio.create_task(shutdown.wait(), name="commodity-shutdown")

    logger.info("[COMMODITY-ENGINE] running")
    done, pending = await asyncio.wait(
        [consumer_task, shutdown_task],
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
    logger.info("[COMMODITY-ENGINE] stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
