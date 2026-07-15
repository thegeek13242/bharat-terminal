"""Named-holder (>=1%) capture → investor_holdings (migration 021).

The SHP XBRL we already fetch for every company names each holder >1% of the
company with their percentage. `nse_ownership_sync.parse_named_holders_full`
extracts them; this module persists them so both lenses work:
  - stock-centric:   who notable holds a stock + QoQ change (API /companies/{sym}/holders)
  - investor-centric: an investor's positions across companies (API /investors/{norm})

Two population paths:
  1. backfill(): re-fetch the XBRL from the `xbrlurl` already stored in
     shareholding_patterns.raw_data (no re-discovery) for the newest N quarters
     per company. This is the deploy-today populator.
  2. capture_from_xbrl(): a live hook the NSE/BSE SHP sweeps call with the XBRL
     text they already downloaded — zero extra fetch, rides existing schedules.

Conservative (data-accuracy rule): a holder row is written only with a parseable
holding_pct; shares are frequently absent in the >1% table and stored NULL when
so rather than fabricated.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.kb.marquee import norm_investor, resolve_investor
from bharat_terminal.kb.models import InvestorHolding
from bharat_terminal.kb.sync.bse_shareholding_sync import XBRL_HOST
from bharat_terminal.kb.sync.bse_sync import BSE_HEADERS
from bharat_terminal.kb.sync.nse_financial_results import create_nse_client
from bharat_terminal.kb.sync.nse_ownership_sync import parse_named_holders_full
from bharat_terminal.kb.sync.nse_shareholding_sync import SHP_REFERER
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import (
    complete_sync_log, create_async_session, start_sync_log,
)

logger = logging.getLogger(__name__)


def _rows_from_parsed(
    symbol: str, quarter_end: date, parsed: List[Dict[str, Any]], source: str,
) -> List[Dict[str, Any]]:
    """parse_named_holders_full output → investor_holdings insert dicts.

    Keeps only holders with a parseable pct and a non-trivial name; de-dupes on
    holder_name (the XBRL occasionally repeats a name across sub-contexts) keeping
    the largest pct."""
    best: Dict[str, Dict[str, Any]] = {}
    for h in parsed:
        name = (h.get("name") or "").strip()
        pct = h.get("holding_pct")
        # Drop 0%-holding names (promoter-group members currently holding
        # nothing) — noise in a "notable holders" view; exits surface via the
        # QoQ full-join instead.
        if not name or pct is None or pct <= 0:
            continue
        prev = best.get(name)
        if prev is not None and (prev["holding_pct"] or 0) >= pct:
            continue
        best[name] = {
            "symbol": symbol,
            "quarter_end": quarter_end,
            "holder_name": name,
            "holder_name_norm": norm_investor(name),
            "category": h.get("category"),
            "holding_pct": pct,
            "shares_held": h.get("shares"),
            "source": source,
        }
    return list(best.values())


async def upsert_holdings(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        for v in rows:
            stmt = pg_insert(InvestorHolding.__table__).values(**v)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_investor_holding",
                set_={k: stmt.excluded[k] for k in v
                      if k not in ("symbol", "quarter_end", "holder_name")},
            )
            await session.execute(stmt)
        await session.commit()
    return len(rows)


async def capture_from_xbrl(
    symbol: str, quarter_end: date, xml: str, source: str,
) -> int:
    """Live hook: called by the SHP sweeps with the XBRL they already fetched.
    Fail-open — a named-holder parse failure must never break the SHP upsert."""
    try:
        parsed = parse_named_holders_full(xml)
        rows = _rows_from_parsed(symbol, quarter_end, parsed, source)
        return await upsert_holdings(rows)
    except Exception as e:  # noqa: BLE001 - live path, never propagate
        logger.warning(f"[HOLD] {symbol} {quarter_end}: named-holder capture failed: {e}")
        return 0


def _xbrl_url(raw_data: Optional[dict], source: str) -> Optional[str]:
    """Reconstruct the fetchable XBRL URL from the stored SHP raw_data."""
    if not isinstance(raw_data, dict):
        return None
    if source == "BSE_XBRL_SHP":
        path = (raw_data.get("xbrlurl") or "").strip()
        if not path:
            return None
        return path if path.startswith("http") else f"{XBRL_HOST}{path}"
    # NSE (and default): full URL stored under 'xbrl'
    url = (raw_data.get("xbrl") or "").strip()
    return url or None


async def backfill(quarters: int = 2, dry_run: bool = True,
                   symbols: Optional[List[str]] = None) -> Dict[str, int]:
    """Populate investor_holdings from the newest `quarters` SHP submissions per
    company, re-fetching each XBRL from its stored url. dry_run reports counts +
    a marquee sample without writing."""
    SessionFactory = create_async_session()
    # newest N quarters per symbol, carrying the source + stored raw_data
    params: Dict[str, Any] = {"q": quarters}
    sym_filter = ""
    if symbols:
        sym_filter = "WHERE symbol = ANY(:syms)"
        params["syms"] = symbols
    q = text(f"""
        SELECT symbol, quarter_end, source, raw_data
        FROM (
            SELECT symbol, quarter_end, source, raw_data,
                   row_number() OVER (PARTITION BY symbol ORDER BY quarter_end DESC) AS rn
            FROM shareholding_patterns
            {sym_filter}
        ) t
        WHERE rn <= :q
        ORDER BY symbol, quarter_end DESC
    """)
    async with SessionFactory() as session:
        res = await session.execute(q, params)
        src_rows = res.mappings().all()

    stats = {"submissions": len(src_rows), "fetched": 0, "holder_rows": 0,
             "written": 0, "marquee_rows": 0, "no_url": 0, "failed": 0}
    marquee_sample: List[str] = []

    nse_client = await create_nse_client()
    bse_client = httpx.AsyncClient(timeout=30, headers=BSE_HEADERS)
    limiter = RateLimiter(rate=2.0)
    # NSE throttles a single cookie session after sustained polling (~30min);
    # a fresh session is instant. Recycle the client every N NSE fetches so the
    # session never ages into the throttle. BSE has no such behaviour.
    nse_recycle_every = 200
    nse_fetches = 0

    log_id: Optional[int] = None
    if not dry_run:
        async with SessionFactory() as session:
            log_id = await start_sync_log(session, "investor_holdings_backfill", "SHP_XBRL")
            await session.commit()

    try:
        for r in src_rows:
            url = _xbrl_url(r["raw_data"], r["source"])
            if not url:
                stats["no_url"] += 1
                continue
            try:
                if r["source"] == "BSE_XBRL_SHP":
                    async with limiter:
                        xr = await bse_client.get(url)
                else:
                    # Proactively recycle the NSE session before it ages out.
                    if nse_fetches and nse_fetches % nse_recycle_every == 0:
                        await nse_client.aclose()
                        nse_client = await create_nse_client()
                    nse_fetches += 1
                    try:
                        async with limiter:
                            xr = await nse_client.get(url, headers={"Referer": SHP_REFERER})
                    except Exception:
                        # Session likely throttled — one retry with a fresh client.
                        await nse_client.aclose()
                        nse_client = await create_nse_client()
                        async with limiter:
                            xr = await nse_client.get(url, headers={"Referer": SHP_REFERER})
                xr.raise_for_status()
                parsed = parse_named_holders_full(xr.text)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[HOLD] {r['symbol']} {r['quarter_end']}: {type(e).__name__} {e}")
                stats["failed"] += 1
                continue
            stats["fetched"] += 1
            rows = _rows_from_parsed(r["symbol"], r["quarter_end"], parsed, r["source"])
            stats["holder_rows"] += len(rows)
            for hr in rows:
                m = resolve_investor(hr["holder_name"])
                if m:
                    stats["marquee_rows"] += 1
                    if len(marquee_sample) < 25:
                        marquee_sample.append(
                            f"{m['display']} → {r['symbol']} {r['quarter_end']} "
                            f"{hr['holding_pct']}%")
            if not dry_run:
                stats["written"] += await upsert_holdings(rows)
    finally:
        await nse_client.aclose()
        await bse_client.aclose()
        if log_id is not None:
            async with SessionFactory() as session:
                await complete_sync_log(
                    session, log_id, "completed",
                    records_processed=stats["written"], records_failed=stats["failed"],
                    metadata=stats)
                await session.commit()

    logger.info(f"[HOLD] backfill {'DRY-RUN' if dry_run else 'EXECUTE'}: {stats}")
    if marquee_sample:
        logger.info("[HOLD] marquee sample:\n  " + "\n  ".join(marquee_sample))
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(prog="investor_holdings_backfill")
    ap.add_argument("--quarters", type=int, default=2)
    ap.add_argument("--execute", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--symbols", help="comma-separated; default = all in shareholding_patterns")
    args = ap.parse_args()
    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols else None)
    asyncio.run(backfill(quarters=args.quarters, dry_run=not args.execute, symbols=syms))


if __name__ == "__main__":
    main()
