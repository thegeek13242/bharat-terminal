"""One-shot backfill: result_events for a past date window (AWS-migration gap).

The Aug 9-11 2026 ingestion gap (GCP stopped, AWS not yet live) left
result_events empty for two trading days in results season. The monitor's
_refresh_calendar only ever looks today..+14, so this driver replicates it
(monitor.py:96-137, including the known-symbols FK guard) with an explicit
past window. Idempotent via uq_result_event ON CONFLICT DO NOTHING.

After seeding, rows with result_date <= today and sync_status='pending' are
picked up by the running kb-monitor's _sync_due_events cycle, which pulls the
XBRL into financial_results/nse_filings. NOTE monitor.py:184: events older
than 3 days with no XBRL found are marked no_data rather than retried — run
this promptly, or follow up with sync-symbol per symbol.

Run (kb image):
  docker compose run --rm --no-deps kb-migrate \
    python -m scripts.backfill_result_events_window 2026-08-09 2026-08-11
"""
import asyncio
import logging
import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.kb.models import Company, ResultEvent
from bharat_terminal.kb.sync.event_calendar import fetch_result_calendar
from bharat_terminal.kb.sync.nse_financial_results import create_nse_client
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import create_async_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_result_events")


async def main(from_date: date, to_date: date) -> None:
    SessionFactory = create_async_session()
    limiter = RateLimiter(rate=3.0)

    client = await create_nse_client()
    try:
        entries = await fetch_result_calendar(
            client, limiter, from_date=from_date, to_date=to_date)
    finally:
        await client.aclose()
    logger.info("calendar window %s..%s: %d result entries fetched",
                from_date, to_date, len(entries))

    new_events = skipped_fk = 0
    async with SessionFactory() as session:
        known = await session.execute(select(Company.symbol))
        known_symbols = {row[0] for row in known.fetchall()}
        for entry in entries:
            if entry["symbol"] not in known_symbols:
                skipped_fk += 1
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

    logger.info("done: %d fetched, %d new rows inserted, %d skipped (unknown symbol)",
                len(entries), new_events, skipped_fk)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: backfill_result_events_window.py FROM_DATE TO_DATE (YYYY-MM-DD)")
    asyncio.run(main(date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])))
