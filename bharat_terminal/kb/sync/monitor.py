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
import signal
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

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
                logger.info(f"[price_sync] Complete: {result}")
            except Exception as e:
                logger.error(f"[price_sync] Failed: {e}")

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

    # ── Main ───────────────────────────────────────────────────────────────

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
