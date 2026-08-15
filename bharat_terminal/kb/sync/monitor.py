"""
Continuous KB update monitor for Bharat Terminal.

Long-running async worker that keeps the knowledge base fresh:
  - Event-driven financial sync: polls NSE board meetings calendar to discover
    result announcements, then syncs only those specific companies.
  - Daily price sync after market close (16:30 IST).
  - Weekly full sweep as a safety net (Saturday 2:00 AM IST).

Uses IST (Asia/Kolkata) scheduling via zoneinfo.
Handles graceful shutdown via SIGINT/SIGTERM.
"""
import asyncio
import logging
import os
import signal
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.heartbeat import heartbeat_loop as _hb_loop
from bharat_terminal.kb.models import ResultEvent
from bharat_terminal.kb.sync.event_calendar import (
    fetch_result_calendar,
)
from bharat_terminal.kb.sync.nse_financial_results import create_nse_client
from bharat_terminal.kb.sync.pipeline import (
    sync_financials,
    sync_prices,
    sync_dcf_all,
    sync_symbol,
)
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import create_async_session

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


class Monitor:
    def __init__(self):
        self._shutdown = asyncio.Event()
        # IST date of the most recent successful daily_price_sync. Used by
        # daily_signal_eval to gate on real sync completion instead of a fixed
        # clock offset — see daily_signal_eval.
        self._price_sync_date: date | None = None

    def _handle_signal(self, sig):
        logger.info(f"Received {signal.Signals(sig).name}, initiating graceful shutdown")
        self._shutdown.set()

    async def _sleep_until(self, target: datetime) -> bool:
        """Sleep until target IST datetime. Returns False if shutdown requested."""
        now = datetime.now(IST)
        delta = (target - now).total_seconds()
        if delta <= 0:
            return True
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=delta)
            return False  # Shutdown was signaled
        except asyncio.TimeoutError:
            return True  # Timer expired normally

    async def _sleep_seconds(self, seconds: float) -> bool:
        """Sleep for N seconds. Returns False if shutdown requested."""
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    def _next_occurrence(self, hour: int, minute: int, weekday: int = None) -> datetime:
        """
        Compute next occurrence of HH:MM IST (optionally on a specific weekday).
        weekday: 0=Mon, 5=Sat, etc. None=any day.
        """
        now = datetime.now(IST)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if weekday is not None:
            days_ahead = weekday - now.weekday()
            if days_ahead < 0 or (days_ahead == 0 and target <= now):
                days_ahead += 7
            target += timedelta(days=days_ahead)
        else:
            if target <= now:
                target += timedelta(days=1)

        return target

    # ── Event-Driven Financial Sync ────────────────────────────────────────

    async def _refresh_calendar(self) -> int:
        """
        Fetch the NSE board meetings calendar for the next 14 days,
        upsert result events into the DB, and return count of new events.
        """
        SessionFactory = create_async_session()
        limiter = RateLimiter(rate=3.0)
        new_events = 0

        client = await create_nse_client()
        try:
            today = date.today()
            entries = await fetch_result_calendar(
                client, limiter,
                from_date=today,
                to_date=today + timedelta(days=14),
            )
        finally:
            await client.aclose()

        async with SessionFactory() as session:
            # Load known symbols to skip FK violations
            from bharat_terminal.kb.models import Company
            known = await session.execute(select(Company.symbol))
            known_symbols = {row[0] for row in known.fetchall()}

            for entry in entries:
                if entry["symbol"] not in known_symbols:
                    continue
                stmt = pg_insert(ResultEvent.__table__).values(
                    symbol=entry["symbol"],
                    result_date=entry["result_date"],
                    description=entry["description"],
                    sync_status="pending",
                ).on_conflict_do_nothing(constraint="uq_result_event")
                result = await session.execute(stmt)
                if result.rowcount > 0:
                    new_events += 1
            await session.commit()

        logger.info(f"[calendar] Refreshed: {len(entries)} events found, {new_events} new")
        return new_events

    async def _sync_due_events(self) -> dict:
        """
        Find result events where result_date <= today and sync_status = 'pending',
        then run sync_symbol for each.
        """
        SessionFactory = create_async_session()
        today = date.today()
        stats = {"synced": 0, "failed": 0, "no_data": 0}

        async with SessionFactory() as session:
            result = await session.execute(
                select(ResultEvent).where(
                    ResultEvent.result_date <= today,
                    ResultEvent.sync_status == "pending",
                ).order_by(ResultEvent.result_date)
            )
            events = result.scalars().all()

        if not events:
            logger.debug("[event_sync] No pending result events due")
            return stats

        logger.info(f"[event_sync] {len(events)} pending result events to sync")

        for event in events:
            symbol = event.symbol
            try:
                result = await sync_symbol(symbol)
                parsed = result.get("parsed", 0)

                async with SessionFactory() as session:
                    evt = (await session.execute(
                        select(ResultEvent).where(ResultEvent.id == event.id)
                    )).scalar_one()

                    if parsed > 0:
                        evt.sync_status = "synced"
                        stats["synced"] += 1
                    elif result.get("status") == "error":
                        evt.sync_status = "failed"
                        stats["failed"] += 1
                    else:
                        # No new filings yet — XBRL may not be uploaded.
                        # Keep as pending if result_date is recent (within 3 days),
                        # otherwise mark no_data.
                        if (today - event.result_date).days <= 3:
                            logger.info(f"[event_sync] {symbol}: no XBRL yet, will retry")
                        else:
                            evt.sync_status = "no_data"
                            stats["no_data"] += 1

                    evt.synced_at = datetime.now(IST)
                    await session.commit()

                logger.info(f"[event_sync] {symbol}: parsed={parsed}")

            except Exception as e:
                logger.error(f"[event_sync] {symbol} failed: {e}")
                stats["failed"] += 1
                async with SessionFactory() as session:
                    evt = (await session.execute(
                        select(ResultEvent).where(ResultEvent.id == event.id)
                    )).scalar_one()
                    evt.sync_status = "failed"
                    evt.synced_at = datetime.now(IST)
                    await session.commit()

        logger.info(f"[event_sync] Done: {stats}")
        return stats

    async def event_driven_sync(self):
        """
        Main event-driven loop:
          1. Refresh calendar at 9:00 AM IST (before market opens)
          2. Check for due results at 20:00 IST (after results are typically filed)
          3. Retry check at 23:00 IST (late filings)
        """
        while not self._shutdown.is_set():
            # Morning: refresh the calendar
            morning = self._next_occurrence(hour=9, minute=0)
            logger.info(f"[event_sync] Next calendar refresh at {morning.isoformat()}")
            if not await self._sleep_until(morning):
                break

            try:
                await self._refresh_calendar()
            except Exception as e:
                logger.error(f"[event_sync] Calendar refresh failed: {e}")

            # Evening: sync companies that announced results today
            evening = self._next_occurrence(hour=20, minute=0)
            logger.info(f"[event_sync] Next event sync at {evening.isoformat()}")
            if not await self._sleep_until(evening):
                break

            try:
                await self._sync_due_events()
            except Exception as e:
                logger.error(f"[event_sync] Evening sync failed: {e}")

            # Late night: retry for late filings
            late = self._next_occurrence(hour=23, minute=0)
            logger.info(f"[event_sync] Late retry at {late.isoformat()}")
            if not await self._sleep_until(late):
                break

            try:
                await self._sync_due_events()
            except Exception as e:
                logger.error(f"[event_sync] Late retry failed: {e}")

    # ── Daily Price Sync ───────────────────────────────────────────────────

    async def daily_price_sync(self):
        """Download bhavcopy after NSE publishes it (~18:00 IST). Runs at 19:00
        IST: the old 16:30 slot fired BEFORE the same-day bhavcopy existed, so
        every run 404'd on the current day and price_points lagged a full
        trading day. The backfill loop still catches any missed prior days."""
        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=19, minute=0)
            while target.weekday() >= 5:
                target += timedelta(days=1)

            logger.info(f"[price_sync] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break

            try:
                logger.info("[price_sync] Starting daily price sync")
                result = await sync_prices(days=2)
                # Record completion so daily_signal_eval can gate on it. Only
                # set on success: a failed sync must NOT unblock signal scoring
                # against stale prices.
                self._price_sync_date = datetime.now(IST).date()
                logger.info(f"[price_sync] Complete: {result}")
            except Exception as e:
                logger.error(f"[price_sync] Failed: {e}")

    async def daily_market_deals(self):
        """Bulk & block deals after the exchanges publish them (~19:30–20:00 IST).
        Runs at 20:30 IST. --days 3 window catches any late BSE revisions;
        upserts are idempotent. Market-maker / HFT churn is filtered at write."""
        from bharat_terminal.kb.sync.market_deals_sync import sync as market_deals_sync
        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=20, minute=30)
            while target.weekday() >= 5:
                target += timedelta(days=1)
            logger.info(f"[market_deals] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break
            try:
                logger.info("[market_deals] Starting bulk/block deal sync")
                result = await market_deals_sync(days=3, dry_run=False)
                logger.info(f"[market_deals] Complete: {result}")
            except Exception as e:
                logger.error(f"[market_deals] Failed: {e}")

    # ── Weekly Safety-Net Sweep ────────────────────────────────────────────

    async def weekly_sweep(self):
        """
        Safety net: full Nifty500 sweep on Saturday 2:00 AM IST.
        Catches anything the event-driven sync missed (companies not in calendar,
        late filings beyond the retry window, etc).
        """
        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=2, minute=0, weekday=5)  # Saturday

            logger.info(f"[weekly_sweep] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break

            try:
                logger.info("[weekly_sweep] Starting weekly Nifty500 sweep")
                result = await sync_financials(tier="nifty500")
                logger.info(f"[weekly_sweep] Financials: {result}")

                dcf_result = await sync_dcf_all()
                logger.info(f"[weekly_sweep] DCF: {dcf_result}")
            except Exception as e:
                logger.error(f"[weekly_sweep] Failed: {e}")

    # ── Daily participant-OI sync ─────────────────────────────────────────

    async def daily_participant_oi(self):
        """Download the previous day's NSE FII/DII/Pro/Client F&O CSV at 19:00 IST."""
        from bharat_terminal.kb.sync.participant_oi_sync import sync_participant_oi
        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=19, minute=0)
            # Skip Sat/Sun — NSE doesn't publish on non-trading days
            while target.weekday() >= 5:
                target += timedelta(days=1)

            logger.info(f"[participant_oi] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break
            try:
                result = await sync_participant_oi()
                logger.info(f"[participant_oi] Complete: {result}")
            except Exception as e:
                logger.error(f"[participant_oi] Failed: {e}")

    # ── Weekly shareholding / corporate-actions / ownership sweep ──────────

    async def weekly_shareholding_sweep(self):
        """Refresh NSE shareholding patterns, corporate actions and ownership
        edges for the research universe (Saturday 3:00 AM IST, after the
        financials sweep). Shareholding is quarterly and corp-actions change
        slowly, so weekly cadence is ample."""
        from bharat_terminal.kb.sync import (
            nse_shareholding_sync,
            nse_corporate_actions_sync,
            nse_ownership_sync,
        )
        from bharat_terminal.research.universe import RESEARCH_UNIVERSE

        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=3, minute=0, weekday=5)  # Saturday
            logger.info(f"[shp_sweep] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break

            symbols = sorted(RESEARCH_UNIVERSE)
            try:
                logger.info(f"[shp_sweep] Starting NSE shareholding/CA/ownership "
                            f"sweep ({len(symbols)} symbols)")
                logger.info(f"[shp_sweep] shareholding: {await nse_shareholding_sync.run(symbols)}")
                logger.info(f"[shp_sweep] corporate_actions: {await nse_corporate_actions_sync.run(symbols)}")
                logger.info(f"[shp_sweep] ownership: {await nse_ownership_sync.run(symbols)}")
            except Exception as e:
                logger.error(f"[shp_sweep] Failed: {e}")

    # ── BSE-only universe jobs ─────────────────────────────────────────────

    async def bse_daily_price_sync(self):
        """BSE bhavcopy for the BSE-only universe at 19:15 IST weekdays —
        after the 19:00 NSE slot so the two downloads don't overlap.
        Deliberately does NOT set self._price_sync_date: the signal-eval gate
        stays keyed to the NSE sync, so a BSE failure can neither block nor
        falsely unblock signal scoring."""
        from bharat_terminal.kb.sync.pipeline import sync_bse_prices
        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=19, minute=15)
            while target.weekday() >= 5:
                target += timedelta(days=1)

            logger.info(f"[bse_price_sync] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break
            try:
                result = await sync_bse_prices(days=2)
                logger.info(f"[bse_price_sync] Complete: {result}")
            except Exception as e:
                logger.error(f"[bse_price_sync] Failed: {e}")

            # Same-day structured results for BSE-only filers: one market-wide
            # index request (2-day window covers late-evening filings).
            try:
                from bharat_terminal.kb.sync.bse_financial_results import run_market_scan
                logger.info(f"[bse_price_sync] results scan: {await run_market_scan(since_days=2)}")
            except Exception as e:
                logger.error(f"[bse_price_sync] results scan failed: {e}")

    async def weekly_bse_sweep(self):
        """BSE-only universe sweep, Saturday 4:00 AM IST (after the 02:00
        financials and 03:00 NSE SHP sweeps so BSE traffic doesn't overlap
        NSE sweeps on the same event loop): corporate actions → shareholding
        patterns → structured-results safety net. Each step isolated — one
        source failing must not starve the others."""
        from bharat_terminal.kb.sync import (
            bse_corporate_actions_sync,
            bse_financial_results,
            bse_shareholding_sync,
        )
        from bharat_terminal.kb.sync.bse_sync import bse_universe
        from bharat_terminal.kb.sync.utils import create_async_session

        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=4, minute=0, weekday=5)  # Saturday
            logger.info(f"[bse_sweep] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break

            SessionFactory = create_async_session()
            async with SessionFactory() as session:
                pairs = await bse_universe(session)
            if not pairs:
                logger.info("[bse_sweep] no BSE-only companies onboarded; skipping")
                continue

            logger.info(f"[bse_sweep] Starting BSE sweep ({len(pairs)} scrips)")
            try:
                logger.info(f"[bse_sweep] corporate_actions: "
                            f"{await bse_corporate_actions_sync.run(pairs)}")
            except Exception as e:
                logger.error(f"[bse_sweep] corporate_actions failed: {e}")
            try:
                logger.info(f"[bse_sweep] shareholding: "
                            f"{await bse_shareholding_sync.run(pairs)}")
            except Exception as e:
                logger.error(f"[bse_sweep] shareholding failed: {e}")
            try:
                logger.info(f"[bse_sweep] financial_results (safety net): "
                            f"{await bse_financial_results.run(pairs, since_days=10)}")
            except Exception as e:
                logger.error(f"[bse_sweep] financial_results failed: {e}")

    # ── Main ───────────────────────────────────────────────────────────────

    # Fallback if daily_price_sync never confirms completion (crash / hang):
    # how long after 19:00 IST signal_eval waits before giving up for the night.
    _SIGNAL_EVAL_MAX_WAIT_MIN = 120
    _SIGNAL_EVAL_POLL_SEC = 60

    async def daily_signal_eval(self):
        """Score trade-signal forward returns nightly (weekdays) — runs only
        AFTER daily_price_sync has CONFIRMED completion for the current IST day,
        so it always scores against that day's fresh EOD prices even if the
        bhavcopy download runs long. This replaces the old fixed 19:30 offset,
        which silently scored against stale prices whenever the 19:00 sync
        overran 30 minutes.

        Ingests new signals from impact_reports and evaluates realised 1/5/10/20
        trading-day returns into signal_outcomes. Replaces the fragile host-cron
        job that the VM's crontab never fired.

        If the price sync is not confirmed within _SIGNAL_EVAL_MAX_WAIT_MIN of
        19:00 IST, it SKIPS tonight rather than scoring against stale prices —
        evaluate is idempotent and re-runnable, so the next successful run
        recovers any skipped signals with no data loss."""
        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=19, minute=0)
            while target.weekday() >= 5:
                target += timedelta(days=1)
            logger.info(f"[signal_eval] Next run at {target.isoformat()} "
                        f"(gated on daily_price_sync completion)")
            if not await self._sleep_until(target):
                break

            # Wait for daily_price_sync to record success for this IST day.
            run_day = target.date()
            deadline = target + timedelta(minutes=self._SIGNAL_EVAL_MAX_WAIT_MIN)
            while self._price_sync_date != run_day:
                if datetime.now(IST) >= deadline:
                    logger.warning(
                        f"[signal_eval] price sync for {run_day} not confirmed "
                        f"within {self._SIGNAL_EVAL_MAX_WAIT_MIN} min of 19:00 "
                        f"IST — skipping tonight (evaluate is idempotent; the "
                        f"next successful run will catch up)"
                    )
                    break
                logger.info("[signal_eval] waiting for daily_price_sync to "
                            "complete before scoring...")
                if not await self._sleep_seconds(self._SIGNAL_EVAL_POLL_SEC):
                    return  # shutdown requested during the wait
            else:
                # Loop exited normally → price sync confirmed for run_day.
                try:
                    from bharat_terminal.research.score_signals import ingest, evaluate
                    logger.info(f"[signal_eval] price sync confirmed for "
                                f"{run_day}; scoring signals")
                    await ingest(since_days=30, dry_run=False)
                    await evaluate(dry_run=False)
                    logger.info("[signal_eval] Done")
                except Exception as e:
                    logger.error(f"[signal_eval] Failed: {e}", exc_info=True)

    async def daily_results_sentiment_eval(self):
        """QA check (weekdays): results-filing sentiment vs same-day price move,
        cumulative since the 2026-07-16 scenario-rubric fix, compared against
        the frozen pre-fix baseline (SWSOLAR postmortem). Same price-sync gate
        as daily_signal_eval — the eval reads today's close from price_points,
        so scoring before the sync lands would silently drop today's filings.
        Logs the confusion matrix + persists it to sync_log
        (sync_type='results_sentiment_eval'). Idempotent and re-runnable."""
        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=19, minute=5)
            while target.weekday() >= 5:
                target += timedelta(days=1)
            logger.info(f"[results_eval] Next run at {target.isoformat()} "
                        f"(gated on daily_price_sync completion)")
            if not await self._sleep_until(target):
                break

            run_day = target.date()
            deadline = target + timedelta(minutes=self._SIGNAL_EVAL_MAX_WAIT_MIN)
            while self._price_sync_date != run_day:
                if datetime.now(IST) >= deadline:
                    logger.warning(
                        f"[results_eval] price sync for {run_day} not confirmed "
                        f"— skipping tonight (eval is cumulative; tomorrow's "
                        f"run covers today's filings)")
                    break
                if not await self._sleep_seconds(self._SIGNAL_EVAL_POLL_SEC):
                    return
            else:
                try:
                    from bharat_terminal.research.results_sentiment_eval import evaluate
                    await evaluate(dry_run=False)
                    logger.info("[results_eval] Done")
                except Exception as e:
                    logger.error(f"[results_eval] Failed: {e}", exc_info=True)

    async def heartbeat_watchdog(self):
        """Active alerting on stale worker heartbeats (2026-08-15).

        WHY: heartbeats were previously read only by the pull-only
        /admin/health — papertrade was dead for three full trading days
        (Aug 10-12: zero equity snapshots, marks frozen at Aug 7) and
        nothing told anyone. kb-monitor is the right watcher: it runs 24/7
        and cannot watch itself die (its own beat is watched by /admin).

        Every 60s during the live session (plus a settle grace after the
        09:15 floor), each key in HEARTBEAT_WATCHDOG_KEYS must exist in
        Redis. `papertrade` proves the process is scheduled (TTL 180s);
        `papertrade:mtm` is stamped only after a SUCCESSFUL MTM cycle body,
        so a loop that runs but fails every cycle also trips the alert.
        Alert spam is bounded by guardrails' per-kind cooldown.
        """
        keys = [k.strip() for k in os.getenv(
            "HEARTBEAT_WATCHDOG_KEYS", "papertrade,papertrade:mtm").split(",")
            if k.strip()]
        if not keys:
            return
        from bharat_terminal.analysis.market_hours import (SESSION_FLOOR,
                                                           is_session_live)
        import redis.asyncio as aioredis
        client = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://redis:6379/0"),
            decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        grace = timedelta(minutes=5)
        while not self._shutdown.is_set():
            if not await self._sleep_seconds(60):
                return
            now = datetime.now(IST)
            if not is_session_live(now):
                continue
            floor_dt = datetime.combine(now.date(), SESSION_FLOOR, tzinfo=IST)
            if now < floor_dt + grace:
                continue                     # let services settle post-open
            for k in keys:
                try:
                    alive = await client.get(f"hb:{k}")
                except Exception as e:
                    logger.warning(f"[WATCHDOG] redis read failed: {e}")
                    break
                if alive is None:
                    msg = (f"hb:{k} absent during the live session — worker "
                           f"loop dead or its cycles are failing (the "
                           f"Aug 10-12 papertrade outage class)")
                    logger.error(f"[WATCHDOG] {msg}")
                    try:
                        from bharat_terminal.alerts import get_monitor
                        get_monitor().alert_now("HEARTBEAT_STALLED", msg)
                    except Exception as e:
                        logger.error(f"[WATCHDOG] alert failed: {e}")
            await self._cycle_beat("watchdog", 300)

    async def _cycle_beat(self, loop_name: str, ttl_s: int) -> None:
        """Per-loop last-completed-cycle stamp (hb:kb-monitor:<loop>).

        The service-level heartbeat is a dedicated task, so it keeps beating
        while a SIBLING coroutine is silently hung — which is exactly how the
        2026-08-13 memory_index stall (dead DB connection, unbounded await)
        hid behind a green /admin/health for 14h. A cycle stamp with a TTL of
        ~3 intervals expires when the loop stops completing cycles and shows
        as stalled. Stamped after every cycle, INCLUDING failed ones — an
        erroring loop is alive and already logs; only a hang goes quiet."""
        from bharat_terminal.heartbeat import abeat
        await abeat(f"kb-monitor:{loop_name}", ttl_s)

    async def memory_index_refresh(self):
        """Keep the derived `memory_items` event index fresh: incremental refresh
        of the recent window (embeds only new/changed events) + a retention prune.
        Runs on a fixed interval (MEMORY_INDEX_REFRESH_MINUTES) rather than a clock
        time — news flows all day, so the memory should track it continuously."""
        interval = int(os.getenv("MEMORY_INDEX_REFRESH_MINUTES", "20")) * 60
        from bharat_terminal.kb.memory import index as memory_index
        while not self._shutdown.is_set():
            try:
                await memory_index.refresh()
                await memory_index.prune()
            except Exception as e:
                logger.error(f"[memory_index] refresh failed: {e}", exc_info=True)
            await self._cycle_beat("memory_index", ttl_s=interval * 3)
            if not await self._sleep_seconds(interval):
                break

    async def memory_digest_refresh(self):
        """Rebuild the sector/market narrative digests on an interval
        (MEMORY_DIGEST_REFRESH_HOURS). Skips scopes whose aggregate is unchanged,
        so steady-state cost is near zero. Gated by MEMORY_DIGEST_ENABLED."""
        if os.getenv("MEMORY_DIGEST_ENABLED", "1") != "1":
            logger.info("[memory_digest] disabled (MEMORY_DIGEST_ENABLED=0)")
            return
        interval = int(os.getenv("MEMORY_DIGEST_REFRESH_HOURS", "3")) * 3600
        from bharat_terminal.kb.memory import digest
        while not self._shutdown.is_set():
            try:
                await digest.refresh_all()
            except Exception as e:
                logger.error(f"[memory_digest] refresh failed: {e}", exc_info=True)
            await self._cycle_beat("memory_digest", ttl_s=interval * 3)
            if not await self._sleep_seconds(interval):
                break

    # ── Dossier auto-refresh ───────────────────────────────────────────────

    async def refresh_stale_dossiers(self) -> dict:
        """Regenerate dossiers whose financials have advanced past their vintage.

        A dossier's `source_quarter` is the newest financial_results period it
        was built on. When a company reports a new quarter (synced by
        _sync_due_events above), its dossier silently goes stale — the project's
        characteristic failure mode. This finds exactly those and refreshes them
        incrementally. Idempotent: only genuinely-stale rows are selected, so a
        quiet night is a no-op at zero cost. Capped per run (env
        DOSSIER_REFRESH_MAX_PER_RUN, default 100) so a heavy results day cannot
        drain LLM credits unattended; the remainder is picked up the next night.
        """
        from sqlalchemy import text

        max_per_run = int(os.getenv("DOSSIER_REFRESH_MAX_PER_RUN", "100"))
        concurrency = int(os.getenv("DOSSIER_REFRESH_CONCURRENCY", "4"))

        SessionFactory = create_async_session()
        async with SessionFactory() as session:
            symbols = (await session.execute(text("""
                SELECT d.symbol
                FROM company_dossiers d
                JOIN (SELECT symbol, max(period_end) AS mx
                      FROM financial_results GROUP BY symbol) lf
                  ON lf.symbol = d.symbol
                WHERE lf.mx > d.source_quarter
                ORDER BY lf.mx DESC
                LIMIT :lim
            """), {"lim": max_per_run})).scalars().all()

        stats = {"stale": len(symbols), "refreshed": 0, "failed": 0}
        if not symbols:
            logger.info("[dossier_refresh] No dossiers stale vs financials")
            return stats

        logger.info(
            f"[dossier_refresh] {len(symbols)} dossiers stale vs financials "
            f"(cap {max_per_run}, concurrency {concurrency}) — refreshing"
        )
        from bharat_terminal.research import dossier_agent
        sem = asyncio.Semaphore(concurrency)

        async def _one(sym: str) -> None:
            async with sem:
                try:
                    res = await dossier_agent.research_company(
                        sym, refresh_kind="incremental_quarter")
                    stats["refreshed" if res else "failed"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    logger.error(f"[dossier_refresh] {sym} failed: {e}")

        await asyncio.gather(*(_one(s) for s in symbols))
        logger.info(f"[dossier_refresh] Done: {stats}")
        return stats

    # ── Admin-panel table retention ────────────────────────────────────────

    async def retention_prune_loop(self):
        """Daily 03:30 IST: bound the admin-panel activity/alert tables.
        user_activity_events > 90d and alert_events > 365d are deleted
        (migration 035; sampling already bounds ingest rate — this bounds
        total size). Fail-open pre-migration: missing tables log and skip."""
        from sqlalchemy import text as _text

        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=3, minute=30)
            logger.info(f"[retention_prune] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break
            SessionFactory = create_async_session()
            for table, keep_days in (("user_activity_events", 90),
                                     ("alert_events", 365)):
                try:
                    async with SessionFactory() as session:
                        res = await session.execute(_text(
                            f"DELETE FROM {table} "
                            f"WHERE ts < now() - interval '{keep_days} days'"))
                        await session.commit()
                    logger.info(f"[retention_prune] {table}: "
                                f"{res.rowcount} rows pruned (> {keep_days}d)")
                except Exception as e:
                    logger.warning(f"[retention_prune] {table} skipped: {e}")

    async def dossier_refresh_loop(self):
        """Daily at 23:30 IST — after event_sync's 23:00 late-filing retry, so
        the day's freshly-reported financials are already in the DB before we
        check dossier staleness. Does NOT run on startup (avoids a mass refresh
        on every container restart); the first pass fires at the next 23:30."""
        while not self._shutdown.is_set():
            target = self._next_occurrence(hour=23, minute=30)
            logger.info(f"[dossier_refresh] Next run at {target.isoformat()}")
            if not await self._sleep_until(target):
                break
            try:
                await self.refresh_stale_dossiers()
            except Exception as e:
                logger.error(f"[dossier_refresh] loop error: {e}", exc_info=True)

    async def run(self):
        """Run all monitor tasks concurrently."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_signal, sig)

        logger.info(
            "KB Monitor started — event-driven financial sync, "
            "daily prices, participant OI, weekly safety-net sweep"
        )

        # Run initial calendar refresh on startup
        try:
            await self._refresh_calendar()
            await self._sync_due_events()
        except Exception as e:
            logger.error(f"Initial sync failed: {e}")

        # Try to pull yesterday's participant OI on boot — idempotent
        try:
            from bharat_terminal.kb.sync.participant_oi_sync import sync_participant_oi
            await sync_participant_oi()
        except Exception as e:
            logger.warning(f"Initial participant_oi sync failed: {e}")

        tasks = [
            asyncio.create_task(self.event_driven_sync(),      name="event_sync"),
            asyncio.create_task(self.daily_price_sync(),       name="price_sync"),
            asyncio.create_task(self.daily_participant_oi(),   name="participant_oi"),
            asyncio.create_task(self.weekly_sweep(),           name="weekly_sweep"),
            asyncio.create_task(self.weekly_shareholding_sweep(), name="shp_sweep"),
            asyncio.create_task(self.daily_signal_eval(),      name="signal_eval"),
            asyncio.create_task(self.memory_index_refresh(),   name="memory_index"),
            asyncio.create_task(self.memory_digest_refresh(),  name="memory_digest"),
            asyncio.create_task(self.bse_daily_price_sync(),   name="bse_price_sync"),
            asyncio.create_task(self.weekly_bse_sweep(),       name="bse_weekly_sweep"),
            asyncio.create_task(self.daily_market_deals(),     name="market_deals"),
            asyncio.create_task(self.daily_results_sentiment_eval(), name="results_eval"),
            asyncio.create_task(self.dossier_refresh_loop(),   name="dossier_refresh"),
            asyncio.create_task(self.retention_prune_loop(),   name="retention_prune"),
            asyncio.create_task(self.heartbeat_watchdog(),     name="hb_watchdog"),
            # Liveness beat for /admin/health: every sync loop above sleeps
            # for hours between scheduled runs, so a dedicated 60s beat is the
            # only honest "this process is alive" signal.
            asyncio.create_task(_hb_loop("kb-monitor"),        name="heartbeat"),
        ]

        await self._shutdown.wait()
        logger.info("Shutdown requested, cancelling tasks...")

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("KB Monitor stopped")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    monitor = Monitor()
    await monitor.run()
