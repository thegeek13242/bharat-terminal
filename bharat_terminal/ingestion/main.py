import asyncio
import logging
import os
import signal
from typing import Optional
from urllib.parse import urlparse

import redis.asyncio as aioredis

from bharat_terminal.ingestion.kafka_producer import NewsKafkaProducer
from bharat_terminal.ingestion.dedup import DedupFilter
from bharat_terminal.ingestion.article_fetcher import ArticleFetcher
from bharat_terminal.ingestion.adapters.nse_filings import NSEFilingsAdapter
from bharat_terminal.ingestion.adapters.bse_filings import BSEFilingsAdapter
from bharat_terminal.ingestion.adapters.economic_times import EconomicTimesAdapter
from bharat_terminal.ingestion.adapters.mint import MintAdapter
from bharat_terminal.ingestion.adapters.ndtv_profit import NDTVProfitAdapter
from bharat_terminal.ingestion.adapters.moneycontrol import MoneyControlAdapter
from bharat_terminal.ingestion.adapters.reuters_india import ReutersIndiaAdapter
from bharat_terminal.ingestion.adapters.pti import PTIAdapter
from bharat_terminal.ingestion.adapters.polymarket import PolymarketAdapter
from bharat_terminal.ingestion.adapters.angelone_mcx import AngelOneMCXAdapter
from bharat_terminal.types import NewsItem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# Domains we will NOT try to body-fetch:
#   • Google News redirect URLs — require JS / resolve to different publishers.
#     Downstream can resolve these separately if they become important.
#   • NSE/BSE announcement pages — these are symbol lookup pages, not articles;
#     useful text lives in the PDF attachment referenced by the filing.
_SKIP_FETCH_DOMAINS = {
    "news.google.com",
    "www.google.com",
    "www.nseindia.com",
    "www.bseindia.com",
}


def _should_fetch_body(item: NewsItem) -> bool:
    """Decide whether to enqueue this item for full-body extraction."""
    # Filings are structured JSON; no article page to scrape.
    if item.tier == "tier1":
        return False
    if item.tier == "sentiment":
        return False
    if not item.url:
        return False
    try:
        domain = urlparse(item.url).netloc.lower()
    except Exception:
        return False
    if any(domain.endswith(d) for d in _SKIP_FETCH_DOMAINS):
        return False
    return True


async def _build_redis_client() -> Optional[aioredis.Redis]:
    """Connect to Redis with a quick ping. Returns None if unavailable (degraded mode)."""
    try:
        client = aioredis.from_url(REDIS_URL, decode_responses=False)
        pong = await client.ping()
        if pong:
            logger.info(f"Redis connected at {REDIS_URL}")
            return client
    except Exception as e:
        logger.warning(
            f"Redis unavailable at {REDIS_URL} ({e}); running without dedup/cache"
        )
    return None


async def main():
    producer = NewsKafkaProducer()
    await producer.start()

    redis_client = await _build_redis_client()
    dedup: Optional[DedupFilter] = DedupFilter(redis_client) if redis_client else None

    fetcher: Optional[ArticleFetcher] = None
    if redis_client:
        fetcher = ArticleFetcher(
            redis_client=redis_client,
            publish_callback=producer.publish_enriched,
        )
        await fetcher.start()

    # Sentiment layer — Polymarket prediction markets → raw.sentiment.in
    # Volume floor is env-configurable so we can tune for signal vs. coverage
    # without rebuilding. Default is $1M (strict: only the most liquid Fed /
    # commodity markets). Live Gamma data shows in-universe markets typically
    # cap at ~$300K on quiet days, so on those days we'll emit nothing — that
    # is the intended signal-over-noise tradeoff.
    poly_min_volume_24h = float(os.getenv("POLYMARKET_MIN_VOLUME_24H", "1000000"))
    poly_min_volume_total = float(os.getenv("POLYMARKET_MIN_VOLUME_TOTAL", "5000000"))
    polymarket = PolymarketAdapter(
        publish_callback=producer.publish_sentiment,
        redis_client=redis_client,
        min_volume_24h=poly_min_volume_24h,
        min_volume_total=poly_min_volume_total,
    )
    await polymarket.start()
    logger.info(
        f"[POLYMARKET] strict-universe filter: Fed + commodities, "
        f"min_volume_24h=${poly_min_volume_24h:,.0f}, "
        f"min_volume_total=${poly_min_volume_total:,.0f}"
    )

    # Commodity tick layer — Angel One SmartAPI MCX feed → raw.commodity.in
    # Skipped silently if creds aren't configured, so dev environments without
    # a broker account still boot cleanly.
    angelone: Optional[AngelOneMCXAdapter] = None
    if all(
        os.getenv(k)
        for k in (
            "ANGEL_ONE_API_KEY",
            "ANGEL_ONE_CLIENT_CODE",
            "ANGEL_ONE_PASSWORD",
            "ANGEL_ONE_TOTP_SECRET",
        )
    ):
        try:
            # Single adapter, dual subscription: MCX commodities (existing)
            # plus NSE F&O stock + index futures with SnapQuote mode for OI.
            # If you want to disable F&O ingestion, set ANGEL_ONE_ENABLE_FNO=0.
            enable_fno = os.getenv("ANGEL_ONE_ENABLE_FNO", "1") == "1"
            angelone = AngelOneMCXAdapter(
                publish_callback=producer.publish_commodity_tick,
                publish_derivative_callback=(
                    producer.publish_derivative_tick if enable_fno else None
                ),
            )
            await angelone.start()
        except Exception as e:
            logger.error(
                f"[ANGELONE] failed to start: {e}", exc_info=True
            )
            angelone = None
    else:
        logger.info(
            "[ANGELONE] credentials not set — skipping MCX/NFO tick adapter"
        )

    # Ingest pipeline: dedup → publish raw → enqueue for body fetch
    async def handle_item(item: NewsItem) -> None:
        if dedup:
            try:
                is_dup, reason = await dedup.is_duplicate(item)
                if is_dup:
                    logger.debug(
                        f"Dedup [{reason}] {item.source}: {item.headline[:70]}"
                    )
                    return
                dedup_hash = await dedup.mark_seen(item)
                item = item.model_copy(update={"dedup_hash": dedup_hash})
            except Exception as e:
                # Never let dedup failures block ingest — log and fall through
                logger.warning(f"Dedup check failed for {item.id}: {e}")

        await producer.publish(item)

        if fetcher and _should_fetch_body(item):
            await fetcher.enqueue(item)

    adapters = [
        NSEFilingsAdapter(redis_client=redis_client),
        BSEFilingsAdapter(redis_client=redis_client),
        EconomicTimesAdapter(redis_client=redis_client),
        MintAdapter(redis_client=redis_client),
        NDTVProfitAdapter(redis_client=redis_client),
        MoneyControlAdapter(redis_client=redis_client),
        ReutersIndiaAdapter(redis_client=redis_client),
        PTIAdapter(redis_client=redis_client),
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
            await adapter.run(handle_item)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                f"Adapter {adapter.source_name} crashed: {e}", exc_info=True
            )
        finally:
            await adapter.close()

    tasks = [asyncio.create_task(adapter_task(a)) for a in adapters]
    logger.info(
        f"Started {len(tasks)} news adapter tasks + 1 sentiment adapter "
        f"(dedup={'on' if dedup else 'off'}, fetcher={'on' if fetcher else 'off'}, "
        f"polymarket=on)"
    )

    await shutdown_event.wait()

    logger.info("Cancelling adapter tasks...")
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)

    await polymarket.stop()

    if angelone:
        await angelone.stop()

    if fetcher:
        await fetcher.stop()

    await producer.stop()

    if redis_client:
        await redis_client.aclose()

    logger.info("Ingestion service stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
