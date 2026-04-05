import asyncio
import logging
import signal

from bharat_terminal.ingestion.kafka_producer import NewsKafkaProducer
from bharat_terminal.ingestion.adapters.nse_filings import NSEFilingsAdapter
from bharat_terminal.ingestion.adapters.bse_filings import BSEFilingsAdapter
from bharat_terminal.ingestion.adapters.economic_times import EconomicTimesAdapter
from bharat_terminal.ingestion.adapters.mint import MintAdapter
from bharat_terminal.ingestion.adapters.ndtv_profit import NDTVProfitAdapter
from bharat_terminal.ingestion.adapters.moneycontrol import MoneyControlAdapter
from bharat_terminal.ingestion.adapters.reuters_india import ReutersIndiaAdapter
from bharat_terminal.ingestion.adapters.pti import PTIAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    producer = NewsKafkaProducer()
    await producer.start()

    adapters = [
        NSEFilingsAdapter(),
        BSEFilingsAdapter(),
        EconomicTimesAdapter(),
        MintAdapter(),
        NDTVProfitAdapter(),
        MoneyControlAdapter(),
        ReutersIndiaAdapter(),
        PTIAdapter(),
    ]

    shutdown_event = asyncio.Event()

    def _handle_shutdown(sig):
        logger.info(f"Received {sig.name}, shutting down gracefully...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: _handle_shutdown(s))

    async def adapter_task(adapter):
        try:
            await adapter.run(producer.publish)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                f"Adapter {adapter.source_name} crashed: {e}", exc_info=True
            )
        finally:
            await adapter.close()

    tasks = [asyncio.create_task(adapter_task(a)) for a in adapters]
    logger.info(f"Started {len(tasks)} adapter tasks")

    await shutdown_event.wait()

    logger.info("Cancelling adapter tasks...")
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)
    await producer.stop()
    logger.info("Ingestion service stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
