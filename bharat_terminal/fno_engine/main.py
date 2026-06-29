"""
fno-engine entrypoint.

  raw.fno.in → consumer → OI-aware persister → derivative_ticks (Postgres)

Stateless across restarts (per-contract state rebuilds from a single
heartbeat per contract within HEARTBEAT_SECONDS). Idempotent under
at-least-once redelivery: composite primary key on derivative_ticks +
ON CONFLICT DO NOTHING absorbs replays.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from bharat_terminal.fno_engine.consumer import FnoKafkaConsumer
from bharat_terminal.fno_engine.persister import FnoPersister

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    consumer = FnoKafkaConsumer()
    persister = FnoPersister()

    await consumer.start()
    await persister.start()

    shutdown = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    consumer_task = asyncio.create_task(consumer.consume(persister.handle), name="fno-consume")
    shutdown_task = asyncio.create_task(shutdown.wait(), name="fno-shutdown")

    logger.info("[FNO-ENGINE] running")
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
    await persister.stop()
    logger.info("[FNO-ENGINE] stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        try:
            from bharat_terminal.alerts import get_monitor
            get_monitor().alert_now("FNO_CRASH", f"fno-engine service crashed: {str(e)[:200]}")
        except Exception:
            pass
        raise
