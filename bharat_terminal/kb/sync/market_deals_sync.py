"""Bulk & block deal sync (NSE + BSE) → market_deals (migration 022).

Intra-quarter smart-money flow that lands same-day, unlike the quarterly SHP.
Sources (all verified 2026-07-13):
  NSE bulk : nsearchives .../content/equities/bulk.csv   (today only)
  NSE block: nsearchives .../content/equities/block.csv  (today only)
  BSE bulk : api BulkDealData_ng/w?DealType=1&FDate=&TDate=  (date range)
  BSE block: api BlockDeal_Beta/w                            (recent)

Market-maker / HFT churn is flagged via kb.market_makers.compute_exclusions
(FLAG not delete). client_name_norm uses marquee.norm_investor so a client's
deals join to the same identity as their SHP holdings in the Smart Money tab.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.kb.market_makers import compute_exclusions
from bharat_terminal.kb.marquee import norm_investor, resolve_investor
from bharat_terminal.kb.models import MarketDeal
from bharat_terminal.kb.sync.bse_sync import BSE_HEADERS
from bharat_terminal.kb.sync.utils import (
    complete_sync_log, create_async_session, start_sync_log,
)

logger = logging.getLogger(__name__)

NSE_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"}
NSE_BULK = "https://nsearchives.nseindia.com/content/equities/bulk.csv"
NSE_BLOCK = "https://nsearchives.nseindia.com/content/equities/block.csv"
BSE_BULK = "https://api.bseindia.com/BseIndiaAPI/api/BulkDealData_ng/w"
BSE_BLOCK = "https://api.bseindia.com/BseIndiaAPI/api/BlockDeal_Beta/w"

_SIDE = {"BUY": "BUY", "SELL": "SELL", "B": "BUY", "S": "SELL", "P": "BUY"}


def _side(raw: str) -> Optional[str]:
    return _SIDE.get((raw or "").strip().upper())


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _deal(deal_date, exchange, deal_type, symbol, company_name, client, side, qty, price):
    q = int(qty) if qty is not None else None
    p = price
    return {
        "deal_date": deal_date, "exchange": exchange, "deal_type": deal_type,
        "symbol": symbol, "company_name": company_name,
        "client_name": client.strip(), "client_name_norm": norm_investor(client),
        "side": side, "quantity": q, "price": p,
        "value_cr": round(q * p / 1e7, 2) if (q and p) else None,
    }


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d %b %Y", "%d/%m/%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt).date()
        except ValueError:
            continue
    return None


def parse_nse_csv(txt: str, deal_type: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(txt)):
        side = _side(row.get("Buy/Sell", ""))
        dt = _parse_date(row.get("Date", ""))
        client = row.get("Client Name", "")
        if not side or not dt or not client.strip():
            continue
        out.append(_deal(
            dt, "NSE", deal_type, (row.get("Symbol") or "").strip().upper(),
            (row.get("Security Name") or "").strip(), client, side,
            _num(row.get("Quantity Traded")),
            _num(row.get("Trade Price / Wght. Avg. Price"))))
    return out


def parse_bse_json(rows: List[dict], deal_type: str,
                   scrip_to_symbol: Dict[str, str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        side = _side(r.get("TRANSACTION_TYPE", ""))
        dt = _parse_date(r.get("DEAL_DATE", ""))
        client = r.get("CLIENT_NAME", "")
        if not side or not dt or not client.strip():
            continue
        scrip = str(r.get("SCRIP_CODE") or "").strip()
        symbol = scrip_to_symbol.get(scrip)  # NSE-style symbol if in our universe
        name = (r.get("scripname") or r.get("ScripName") or "").strip()
        out.append(_deal(dt, "BSE", deal_type, symbol, name, client, side,
                         _num(r.get("QUANTITY")), _num(r.get("PRICE"))))
    return out


async def _bse_scrip_map() -> Dict[str, str]:
    SessionFactory = create_async_session()
    async with SessionFactory() as s:
        rows = (await s.execute(text(
            "SELECT bse_scrip_code, symbol FROM companies "
            "WHERE bse_scrip_code IS NOT NULL"))).all()
    return {str(code): sym for code, sym in rows}


async def _fetch(days: int) -> List[Dict[str, Any]]:
    deals: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30) as nse:
        for url, dt in ((NSE_BULK, "bulk"), (NSE_BLOCK, "block")):
            try:
                r = await nse.get(url, headers=NSE_HEADERS)
                r.raise_for_status()
                got = parse_nse_csv(r.text, dt)
                deals += got
                logger.info(f"[DEALS] NSE {dt}: {len(got)} rows")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DEALS] NSE {dt} fetch failed: {e}")

    scrip_map = await _bse_scrip_map()
    fd = (date.today() - timedelta(days=days)).strftime("%d/%m/%Y")
    td = date.today().strftime("%d/%m/%Y")
    async with httpx.AsyncClient(timeout=30, headers=BSE_HEADERS) as bse:
        for url, dt, params in (
            (BSE_BULK, "bulk", {"DealType": "1", "sc_code": "", "FDate": fd, "TDate": td}),
            (BSE_BLOCK, "block", {"DealType": "1", "sc_code": "", "FDate": fd, "TDate": td}),
        ):
            try:
                r = await bse.get(url, params=params)
                rows = r.json().get("Table", []) if "json" in r.headers.get("content-type", "") else []
                got = parse_bse_json(rows, dt, scrip_map)
                deals += got
                logger.info(f"[DEALS] BSE {dt}: {len(got)} rows")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[DEALS] BSE {dt} fetch failed: {e}")
    return deals


async def _upsert(deals: List[Dict[str, Any]]) -> int:
    if not deals:
        return 0
    cols = ("deal_date", "exchange", "deal_type", "symbol", "company_name",
            "client_name", "client_name_norm", "side", "quantity", "price",
            "value_cr", "marquee_key", "is_excluded", "exclude_reason")
    SessionFactory = create_async_session()
    async with SessionFactory() as s:
        for d in deals:
            v = {k: d.get(k) for k in cols}
            stmt = pg_insert(MarketDeal.__table__).values(**v)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_market_deal",
                set_={k: stmt.excluded[k] for k in
                      ("company_name", "client_name_norm", "value_cr",
                       "marquee_key", "is_excluded", "exclude_reason")})
            await s.execute(stmt)
        await s.commit()
    return len(deals)


async def sync(days: int = 3, dry_run: bool = True) -> Dict[str, int]:
    deals = await _fetch(days)
    compute_exclusions(deals)  # sets is_excluded + exclude_reason per leg
    for d in deals:
        m = resolve_investor(d["client_name"])
        d["marquee_key"] = m["key"] if m else None

    kept = [d for d in deals if not d["is_excluded"]]
    stats = {
        "fetched": len(deals),
        "kept": len(kept),
        "excluded_mm": sum(1 for d in deals if d["exclude_reason"] == "market_maker"),
        "excluded_churn": sum(1 for d in deals if d["exclude_reason"] == "churn"),
        "marquee": sum(1 for d in kept if d["marquee_key"]),
        "written": 0,
    }
    if not dry_run:
        SessionFactory = create_async_session()
        async with SessionFactory() as s:
            log_id = await start_sync_log(s, "market_deals_sync", "NSE_BSE_DEALS")
            await s.commit()
        stats["written"] = await _upsert(deals)
        async with SessionFactory() as s:
            await complete_sync_log(s, log_id, "completed",
                                    records_processed=stats["written"], metadata=stats)
            await s.commit()

    logger.info(f"[DEALS] sync {'DRY-RUN' if dry_run else 'EXECUTE'}: {stats}")
    sample = [d for d in kept if d["marquee_key"]][:12]
    for d in sample:
        logger.info(f"[DEALS]   {d['deal_date']} {d['symbol']} {d['side']} "
                    f"{d['client_name'][:34]} ({d['exchange']} {d['deal_type']})")
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(prog="market_deals_sync")
    ap.add_argument("--days", type=int, default=3, help="BSE lookback window (NSE is today-only)")
    ap.add_argument("--execute", action="store_true", help="write (default: dry-run)")
    args = ap.parse_args()
    asyncio.run(sync(days=args.days, dry_run=not args.execute))


if __name__ == "__main__":
    main()
