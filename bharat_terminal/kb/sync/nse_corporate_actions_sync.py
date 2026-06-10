"""
NSE corporate-actions sync — populates `corporate_actions` from NSE
(/api/corporates-corporateActions). Dividends, bonuses, splits, buybacks, etc.

Per symbol: GET the corporate-actions feed → classify each record's `subject`
into an action_type, extract a numeric `value` where present (e.g. dividend ₹/sh),
keep ex/record dates, upsert on (symbol, ex_date, action_type).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.kb.models import CorporateAction
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import create_async_session
from bharat_terminal.kb.sync.nse_financial_results import (
    NSE_BASE, create_nse_client, refresh_nse_cookies,
)

logger = logging.getLogger(__name__)

CA_API = f"{NSE_BASE}/api/corporates-corporateActions"
CA_REFERER = f"{NSE_BASE}/get-quotes/equity"


def _classify(subject: str) -> Optional[str]:
    """Map an NSE corporate-action subject line to an action_type, or None to skip."""
    s = (subject or "").lower()
    if "dividend" in s:
        return "DIVIDEND"
    if "buy back" in s or "buyback" in s or "buy-back" in s:
        return "BUYBACK"
    if "bonus" in s:
        return "BONUS"
    if "split" in s or "face value" in s:
        return "SPLIT"
    if "rights" in s:
        return "RIGHTS"
    if "scheme of arrangement" in s or "demerger" in s or "amalgamation" in s:
        return "RESTRUCTURE"
    return None  # AGM / board meetings / other non-capital actions → skip


def _extract_value(subject: str, action_type: str) -> Optional[float]:
    """Best-effort numeric value: dividend ₹/share, split ratio, etc."""
    if action_type in ("DIVIDEND", "BUYBACK"):
        m = re.search(r"(?:rs\.?|inr|₹)\s*([\d]+(?:\.\d+)?)", subject, re.I)
        if m:
            return float(m.group(1))
    return None


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s or s in ("-", "N/A"):
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


async def sync_symbol(
    symbol: str, client: httpx.AsyncClient, limiter: RateLimiter,
) -> List[Dict[str, Any]]:
    symbol = symbol.upper().strip()
    async with limiter:
        r = await client.get(CA_API, params={"index": "equities", "symbol": symbol},
                             headers={"Referer": f"{CA_REFERER}?symbol={symbol}"})
    if "application/json" not in r.headers.get("content-type", ""):
        await refresh_nse_cookies(client)
        async with limiter:
            r = await client.get(CA_API, params={"index": "equities", "symbol": symbol},
                                 headers={"Referer": f"{CA_REFERER}?symbol={symbol}"})
    try:
        records = r.json()
    except Exception:
        logger.warning(f"[CA] {symbol}: non-JSON response")
        return []
    if not isinstance(records, list):
        return []

    rows: List[Dict[str, Any]] = []
    seen = set()
    for rec in records:
        subject = rec.get("subject") or ""
        atype = _classify(subject)
        if not atype:
            continue
        ex = _parse_date(rec.get("exDate"))
        if not ex:
            continue
        key = (symbol, ex, atype)
        if key in seen:  # de-dupe within the feed before upsert
            continue
        seen.add(key)
        rows.append({
            "symbol": symbol,
            "ex_date": ex,
            "record_date": _parse_date(rec.get("recDate")),
            "action_type": atype,
            "value": _extract_value(subject, atype),
            "details": rec,
            "source": "NSE",
        })
    return rows


async def upsert_rows(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        for v in rows:
            stmt = pg_insert(CorporateAction.__table__).values(**v)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "ex_date", "action_type"],
                set_={k: stmt.excluded[k] for k in v
                      if k not in ("symbol", "ex_date", "action_type")},
            )
            await session.execute(stmt)
        await session.commit()
    return len(rows)


async def run(symbols: List[str]) -> Dict[str, int]:
    client = await create_nse_client()
    limiter = RateLimiter(rate=2.0)
    stats = {"symbols": 0, "rows": 0, "empty": 0}
    try:
        for sym in symbols:
            rows = await sync_symbol(sym, client, limiter)
            n = await upsert_rows(rows)
            stats["symbols"] += 1
            stats["rows"] += n
            if not n:
                stats["empty"] += 1
            logger.info(f"[CA] {sym}: {n} actions upserted")
    finally:
        await client.aclose()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(prog="nse_corporate_actions_sync")
    ap.add_argument("--symbols", help="Comma-separated; default = research universe")
    args = ap.parse_args()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        from bharat_terminal.research.universe import RESEARCH_UNIVERSE
        symbols = sorted(RESEARCH_UNIVERSE)
    stats = asyncio.run(run(symbols))
    logger.info(f"[CA] DONE: {stats}")


if __name__ == "__main__":
    main()
