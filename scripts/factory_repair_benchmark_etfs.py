"""Repair the benchmark ETF price gap (2025-05-09 -> 2026-06-03).

BACKGROUND (verified 2026-09-12/13): `sync_prices` filters each bhavcopy
against symbols present in `companies`. The benchmark ETFs only entered
`companies` on 2026-07-03, so daily sync never carried them; their history came
entirely from bulk backfills, and the 264-session stretch no backfill covered is
simply missing. `sync_prices` also skips any date already holding >100 rows, and
every gap date holds ~2,004 — so a date-scoped re-run cannot repair this. The
repair has to be symbol-scoped, which is what this script does.

SAFETY
  * Writes ONLY the four allowlisted benchmark symbols. Any other row in the
    bhavcopy is discarded before the insert.
  * INSERT ... ON CONFLICT DO NOTHING. No UPDATE, no DELETE: an existing row is
    never overwritten, so a re-run is harmless.
  * Dry-run is the DEFAULT (there is no --dry-run flag): it samples a few dates
    to prove the source still serves them, and writes nothing.
  * Writing requires --apply, which is a founder-approved action per instance.

    # dry-run (default)
    docker compose run --rm --no-deps \
      -v "$PWD/bharat_terminal:/app/bharat_terminal:ro" \
      -v "$PWD/scripts:/app/scripts:ro" \
      --entrypoint python kb-service scripts/factory_repair_benchmark_etfs.py --sample 4
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from typing import Dict, List, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg

from bharat_terminal.kb.sync.prices_sync import fetch_bhavcopy

# The only symbols this script may write. Everything else in a bhavcopy is
# dropped, so a bad fetch cannot widen the blast radius.
BENCHMARKS: Tuple[str, ...] = ("NIFTYBEES", "JUNIORBEES", "BANKBEES", "MID150BEES")

GAP_START = date(2025, 5, 9)
GAP_END = date(2026, 6, 3)

REQUEST_INTERVAL_S = 1.0          # be polite to the archives host


def dsn() -> str:
    raw = os.getenv("DATABASE_URL", "")
    if not raw:
        raise SystemExit("DATABASE_URL is not set")
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def market_sessions(conn, start: date, end: date) -> List[date]:
    rows = await conn.fetch(
        "SELECT DISTINCT date FROM price_points WHERE date BETWEEN $1 AND $2 ORDER BY date",
        start, end)
    return [r["date"] for r in rows]


async def existing_dates(conn, symbol: str, start: date, end: date) -> Set[date]:
    rows = await conn.fetch(
        "SELECT date FROM price_points WHERE symbol = $1 AND date BETWEEN $2 AND $3",
        symbol, start, end)
    return {r["date"] for r in rows}


async def coverage(conn) -> List[Tuple[str, int, date, date]]:
    rows = await conn.fetch(
        """SELECT symbol, count(*) n, min(date) lo, max(date) hi
           FROM price_points WHERE symbol = ANY($1::text[]) GROUP BY symbol ORDER BY symbol""",
        list(BENCHMARKS))
    return [(r["symbol"], r["n"], r["lo"], r["hi"]) for r in rows]


async def insert_rows(conn, rows: Sequence[dict]) -> int:
    """Insert-only. An existing (symbol, date) is left exactly as it was."""
    if not rows:
        return 0
    result = await conn.executemany(
        """INSERT INTO price_points (symbol, date, open, high, low, close, volume)
           VALUES ($1, $2, $3, $4, $5, $6, $7)
           ON CONFLICT ON CONSTRAINT uq_price_point DO NOTHING""",
        [(r["symbol"], r["date"], r.get("open"), r.get("high"), r.get("low"),
          r["close"], r.get("volume")) for r in rows])
    return len(rows) if result is None else len(rows)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write (founder-approved); default is dry-run")
    ap.add_argument("--sample", type=int, default=3,
                    help="dry-run: how many dates to actually fetch as proof")
    ap.add_argument("--limit", type=int, default=0, help="cap dates processed (0 = all)")
    args = ap.parse_args()

    conn = await asyncpg.connect(dsn(), timeout=30, command_timeout=120)
    try:
        print("=== BEFORE")
        for sym, n, lo, hi in await coverage(conn):
            print(f"  {sym:<12} {n:>5} bars   {lo} -> {hi}")

        sessions = await market_sessions(conn, GAP_START, GAP_END)
        print(f"\nmarket sessions in window {GAP_START}..{GAP_END}: {len(sessions)}")

        missing: Dict[str, Set[date]] = {}
        for sym in BENCHMARKS:
            have = await existing_dates(conn, sym, GAP_START, GAP_END)
            missing[sym] = {d for d in sessions if d not in have}
            print(f"  {sym:<12} missing {len(missing[sym])} of {len(sessions)}")

        dates_needed = sorted(set().union(*missing.values())) if missing else []
        if args.limit:
            dates_needed = dates_needed[:args.limit]
        if not dates_needed:
            print("\nnothing to repair")
            return 0

        plan = len(dates_needed)
        print(f"\ndates to fetch: {plan}  (~{plan * REQUEST_INTERVAL_S / 60:.1f} min at "
              f"{REQUEST_INTERVAL_S}s/request)")

        if not args.apply:
            dates_needed = dates_needed[:args.sample]
            print(f"DRY RUN — fetching only {len(dates_needed)} date(s) as proof; "
                  f"nothing will be written\n")

        fetched = inserted = failed = 0
        empty_dates: List[date] = []
        for i, d in enumerate(dates_needed, 1):
            try:
                prices = await fetch_bhavcopy(d)
            except Exception as e:
                failed += 1
                print(f"  [{i}/{len(dates_needed)}] {d} FETCH FAILED: {e}")
                await asyncio.sleep(REQUEST_INTERVAL_S)
                continue

            wanted = [p for p in prices
                      if p["symbol"] in BENCHMARKS and p["date"] in missing[p["symbol"]]]
            fetched += 1
            if not wanted:
                empty_dates.append(d)
            if args.apply and wanted:
                inserted += await insert_rows(conn, wanted)
            if i <= 5 or i % 25 == 0:
                print(f"  [{i}/{len(dates_needed)}] {d}: {len(wanted)} benchmark row(s)"
                      + (" (would insert)" if not args.apply else ""))
            await asyncio.sleep(REQUEST_INTERVAL_S)

        print(f"\nfetched {fetched} date(s), failed {failed}, "
              f"{'inserted' if args.apply else 'would insert'} {inserted} row(s)")
        if empty_dates:
            print(f"dates with no benchmark rows in the bhavcopy: {len(empty_dates)} "
                  f"(first: {empty_dates[:3]})")

        if args.apply:
            print("\n=== AFTER")
            for sym, n, lo, hi in await coverage(conn):
                print(f"  {sym:<12} {n:>5} bars   {lo} -> {hi}")
            still = {s: len({d for d in sessions
                             if d not in await existing_dates(conn, s, GAP_START, GAP_END)})
                     for s in BENCHMARKS}
            print(f"still missing in window: {still}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
