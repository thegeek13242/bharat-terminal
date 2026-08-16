import asyncio
import logging
import os
import signal
from typing import Optional
from urllib.parse import urlparse

import redis.asyncio as aioredis

from bharat_terminal.heartbeat import abeat
from bharat_terminal.ingestion.liveness import (respawn_candidates,
                                                stalled_adapters)
from bharat_terminal.ingestion.kafka_producer import NewsKafkaProducer
from bharat_terminal.ingestion.dedup import DedupFilter, url_fingerprint
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
#   • in.investing.com — article pages sit behind a Cloudflare JS challenge that
#     hard-403s every server-side fetch, so body extraction is impossible. The
#     RSS feeds carry no <description> either, so Investing.com is a headline-only
#     macro source; don't waste fetch attempts on it.
_SKIP_FETCH_DOMAINS = {
    "news.google.com",
    "www.google.com",
    "www.nseindia.com",
    "www.bseindia.com",
    "in.investing.com",
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
        # Socket timeouts (2026-07-20 postmortem): a wedged connection must
        # raise, not park callers forever. No blocking redis commands are used
        # in this service, so a hard socket_timeout is safe.
        client = aioredis.from_url(REDIS_URL, decode_responses=False,
                                   socket_timeout=10, socket_connect_timeout=5)
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
            # MCX commodity tick adapter (AngelOne SmartWebSocketV2).
            angelone = AngelOneMCXAdapter(
                publish_callback=producer.publish_commodity_tick,
            )
            await angelone.start()
        except Exception as e:
            logger.error(
                f"[ANGELONE] failed to start: {e}", exc_info=True
            )
            angelone = None
    else:
        logger.info(
            "[ANGELONE] credentials not set — skipping MCX tick adapter"
        )

    # Ingest pipeline: dedup → publish raw → enqueue for body fetch
    async def handle_item(item: NewsItem) -> None:
        if dedup:
            try:
                is_dup, reason = await dedup.is_duplicate(item)
                if is_dup:
                    # "content" = the other exchange's copy of a dual-listed
                    # filing — INFO so the cross-exchange win stays observable.
                    log = logger.info if reason == "content" else logger.debug
                    log(f"Dedup [{reason}] {item.source}: {item.headline[:70]}")
                    return
                # Hash computed WITHOUT persisting — dedup marks are written
                # only after publish succeeds (2026-07-20 stall postmortem:
                # mark-before-publish turned hangs into permanent item loss).
                item = item.model_copy(
                    update={"dedup_hash": url_fingerprint(item.url)})
            except Exception as e:
                # Never let dedup failures block ingest — log and fall through
                logger.warning(f"Dedup check failed for {item.id}: {e}")

        await producer.publish(item)

        # Every item must reach raw.news.enriched.in — that is the stream the
        # analysis pipeline consumes. Items eligible for body extraction go via
        # the ArticleFetcher (which publishes the enriched copy on success AND
        # failure, and a body-less copy if its queue is saturated). Everything
        # else — filings, sentiment, no-URL, skip-fetch domains, or when the
        # fetcher is disabled — is published body-less here so it is still
        # analysed (on its headline + RSS summary).
        if fetcher and _should_fetch_body(item):
            await fetcher.enqueue(item)
        else:
            enriched = item.model_copy(update={
                "full_body": None,
                "body_word_count": 0,
                "body_fetch_status": "skipped",
                "body_extraction_method": item.body_extraction_method or "rss",
            })
            await producer.publish_enriched(enriched)

        # Persist the dedup marks (URL fingerprint + simhash) only now, after
        # every publish above has succeeded. A duplicate arriving in the tiny
        # window before persistence would double-publish — accepted trade:
        # duplicates are visible and harmless, silent loss is neither.
        if dedup:
            try:
                await dedup.mark_seen(item)
            except Exception as e:
                logger.warning(f"Dedup persist failed for {item.id}: {e}")

    adapters = [
        NSEFilingsAdapter(redis_client=redis_client),
    ]
    # Primary-source doctrine (founder decision 2026-07-19): third-party news
    # is lagged echo of the filings we already process first-hand (measured
    # over 30d: 47%+ of its high-magnitude events were filing echoes; much of
    # the rest was recommendation listicles) — so wire/media adapters are OFF
    # by default. Code kept intact; re-enable any of them without a rebuild
    # via INGESTION_EXTRA_SOURCES (comma-separated source names).
    _third_party = {
        "ECONOMIC_TIMES": EconomicTimesAdapter,
        "MINT": MintAdapter,
        "MARKETS_FEED": NDTVProfitAdapter,
        "INVESTING_IN": MoneyControlAdapter,
        "REUTERS_INDIA": ReutersIndiaAdapter,
        "PTI": PTIAdapter,
    }
    _extra = {s.strip().upper()
              for s in os.getenv("INGESTION_EXTRA_SOURCES", "").split(",")
              if s.strip()}
    for _name, _cls in _third_party.items():
        if _name in _extra:
            adapters.append(_cls(redis_client=redis_client))
            logger.info(f"[{_name}] third-party source ENABLED via INGESTION_EXTRA_SOURCES")
        else:
            logger.info(f"[{_name}] disabled (primary-source doctrine)")
    # BSE-only universe filings. Env kill switch: every announcement enters the
    # full LLM pipeline, so a runaway-cost situation must be stoppable with an
    # env flip + restart, not a rebuild.
    if os.getenv("BSE_FILINGS_ENABLED", "1") != "0":
        adapters.append(BSEFilingsAdapter(redis_client=redis_client))
    else:
        logger.info("[BSE_FILINGS] disabled via BSE_FILINGS_ENABLED=0")

    # SIGUSR1 → dump every asyncio task's stack to the log. Added during the
    # 2026-07-20 pipeline-stall incident: adapter coroutines were parking
    # forever with zero log output, and thread-level tools (py-spy) cannot see
    # awaiting tasks. `docker kill -s USR1 bharat-ingestion` at any time.
    def _dump_tasks(sig=None, frame=None):
        import traceback
        tasks = asyncio.all_tasks(loop)
        logger.warning(f"[taskdump] {len(tasks)} task(s)")
        for t in tasks:
            # Task.get_stack() returns ONE frame for suspended coroutines —
            # walk the cr_await chain manually to see the true parked leaf.
            lines, obj, depth = [], t.get_coro(), 0
            while obj is not None and depth < 12:
                fr = getattr(obj, "cr_frame", None) or getattr(obj, "gi_frame", None)
                if fr is not None:
                    lines.append(f"    {fr.f_code.co_filename.split('/')[-1]}"
                                 f":{fr.f_lineno} {fr.f_code.co_name}")
                nxt = getattr(obj, "cr_await", None) or getattr(obj, "gi_yieldfrom", None)
                if nxt is None:
                    lines.append(f"    -> awaiting {type(obj).__name__}: {obj!r:.90}")
                obj, depth = nxt, depth + 1
            logger.warning(f"[taskdump] {t.get_name()} done={t.done()}\n"
                           + "\n".join(lines or ["    <no frames>"]))

    shutdown_event = asyncio.Event()

    def _handle_shutdown(sig):
        logger.info(f"Received {sig.name}, shutting down gracefully...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: _handle_shutdown(s))
    loop.add_signal_handler(signal.SIGUSR1, _dump_tasks)

    async def adapter_task(adapter):
        try:
            await adapter.run(handle_item)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # A crashed adapter stops a news source entirely — critical.
            try:
                from bharat_terminal.alerts import get_monitor
                get_monitor().alert_now(
                    "INGESTION_ADAPTER_CRASH",
                    f"adapter {adapter.source_name} crashed: {str(e)[:200]}",
                )
            except Exception:
                pass
            logger.error(
                f"Adapter {adapter.source_name} crashed: {e}", exc_info=True
            )
        finally:
            await adapter.close()

    async def liveness_watchdog():
        """Per-adapter stall detection (2026-07-20 postmortem) + SELF-HEAL
        (2026-08-16, BSE wedge after the EC2 hard crash): the crash alert
        only fires on exceptions, and a silently parked loop previously got
        an alert but still needed a human `docker restart`. Now: alert at
        the stall floor, and past the (higher) respawn floor cancel the
        parked adapter task and respawn it — cancellation lands at the
        wedged await regardless of which await it is, and adapter.close()
        in adapter_task's finally discards the wedged HTTP session. Respawns
        are rate-limited per adapter (liveness.respawn_candidates) so a
        genuinely broken source degrades to a visible respawn-heartbeat,
        never a flap loop."""
        import time as _time
        stall_after = float(os.getenv("ADAPTER_STALL_ALERT_S", "300"))
        respawn_after = float(os.getenv("ADAPTER_RESPAWN_AFTER_S", "1200"))
        respawn_min_gap = float(
            os.getenv("ADAPTER_RESPAWN_MIN_INTERVAL_S", "600"))
        hb_every = float(os.getenv("ADAPTER_HEARTBEAT_LOG_S", "900"))
        last_hb = 0.0
        last_respawn: dict = {}
        while True:
            await asyncio.sleep(60)
            # Liveness beat for /admin/health (per-adapter stalls are alerted
            # separately below; this proves the process loop itself).
            await abeat("ingestion")
            now = _time.time()
            for name, idle in stalled_adapters(adapters, now, stall_after):
                logger.error(f"[liveness] {name} loop stalled — no cycle "
                             f"for {idle:.0f}s")
                try:
                    from bharat_terminal.alerts import get_monitor
                    get_monitor().alert_now(
                        f"INGESTION_ADAPTER_STALLED_{name}",
                        f"{name} adapter loop has not completed a cycle for "
                        f"{idle:.0f}s. Items are safe (post-publish seen "
                        f"marking) but ingestion for this source is DOWN. "
                        f"docker kill -s USR1 bharat-ingestion for task stacks.")
                except Exception:
                    pass
            for adapter, idle in respawn_candidates(
                    adapters, now, respawn_after, last_respawn,
                    respawn_min_gap):
                name = adapter.source_name
                last_respawn[name] = now
                logger.error(f"[liveness] {name} parked {idle:.0f}s — "
                             f"cancelling and respawning the adapter task")
                old = adapter_tasks.get(name)
                if old is not None and not old.done():
                    old.cancel()
                    try:
                        # CancelledError lands at the parked await; the
                        # adapter_task's finally closes the (possibly
                        # wedged) HTTP session on the way out.
                        await asyncio.wait_for(old, timeout=15)
                    except Exception:
                        pass    # timeout/cancel/crash — respawn regardless
                adapter.last_cycle_ts = now     # fresh grace period
                adapter_tasks[name] = asyncio.create_task(
                    adapter_task(adapter), name=f"adapter-{name}")
                try:
                    from bharat_terminal.alerts import get_monitor
                    get_monitor().alert_now(
                        f"INGESTION_ADAPTER_RESPAWNED_{name}",
                        f"{name} adapter was parked for {idle:.0f}s and has "
                        f"been auto-respawned (self-heal, 2026-08-16). If "
                        f"this repeats every ~{respawn_min_gap / 60:.0f} min "
                        f"the source itself is broken — investigate, do not "
                        f"just watch the respawns.")
                except Exception:
                    pass
            if now - last_hb >= hb_every:
                last_hb = now
                beats = ", ".join(
                    f"{a.source_name}={now - getattr(a, 'last_cycle_ts', now):.0f}s"
                    for a in adapters)
                logger.info(f"[liveness] adapter cycles: {beats}")

    adapter_tasks: dict = {
        a.source_name: asyncio.create_task(
            adapter_task(a), name=f"adapter-{a.source_name}")
        for a in adapters
    }
    watchdog_task = asyncio.create_task(
        liveness_watchdog(), name="liveness-watchdog")
    tasks = list(adapter_tasks.values()) + [watchdog_task]
    logger.info(
        f"Started {len(tasks)} news adapter tasks + 1 sentiment adapter "
        f"(dedup={'on' if dedup else 'off'}, fetcher={'on' if fetcher else 'off'}, "
        f"polymarket=on)"
    )

    await shutdown_event.wait()

    logger.info("Cancelling adapter tasks...")
    # adapter_tasks, not the boot-time `tasks` list: a self-heal respawn
    # replaces entries, and shutdown must cancel the CURRENT tasks.
    live_tasks = list(adapter_tasks.values()) + [watchdog_task]
    for task in live_tasks:
        task.cancel()

    await asyncio.gather(*live_tasks, return_exceptions=True)

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
    try:
        asyncio.run(main())
    except Exception as e:
        try:
            from bharat_terminal.alerts import get_monitor
            get_monitor().alert_now("INGESTION_CRASH", f"ingestion service crashed: {str(e)[:200]}")
        except Exception:
            pass
        raise
