"""
BSE corporate-actions sync — populates `corporate_actions` for the BSE-only
universe from api.bseindia.com (/CorporateAction/w?scripcode=).

Mirrors nse_corporate_actions_sync.py and reuses its subject classifier and
value extractor (the purpose vocabulary is the same: "Final Dividend",
"Bonus issue ...", etc.). Upsert conflict key (symbol, ex_date, action_type)
is shared with NSE rows; `source="BSE"` marks provenance.

Response shape (verified live 2026-07-03): {"Table": [...summary...],
"Table2": [{"Ex_date": "30 Apr 2026", "BCRD": "RD 02/05/2026",
"purpose": "Final Dividend", "Details": "29.59", "PAYMENT_DATE": ...}]}.
Table2 is the detailed feed — Ex_date is explicit; BCRD carries the record
date / book-closure range behind a two-letter prefix.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.kb.models import CorporateAction
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import (
    complete_sync_log, create_async_session, start_sync_log,
)
from bharat_terminal.kb.sync.bse_sync import BSE_HEADERS
from bharat_terminal.kb.sync.nse_corporate_actions_sync import _classify, _extract_value

logger = logging.getLogger(__name__)

CA_API = "https://api.bseindia.com/BseIndiaAPI/api/CorporateAction/w"

_DMY_SLASH_RE = re.compile(r"(\d{2}/\d{2}/\d{4})")


def _parse_bse_date(s: Optional[str]) -> Optional[date]:
    """Ex_date format: '30 Apr 2026'."""
    if not s or s.strip() in ("-", "N/A", ""):
        return None
    try:
        return datetime.strptime(s.strip(), "%d %b %Y").date()
    except ValueError:
        return None


def _parse_bcrd(s: Optional[str]) -> Optional[date]:
    """BCRD carries the record date / book-closure start behind a prefix,
    e.g. 'RD 02/05/2026' or 'BC 12/04/2026 - 15/04/2026' — take the first
    dd/mm/yyyy token."""
    if not s:
        return None
    m = _DMY_SLASH_RE.search(s)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


async def sync_scrip(
    scrip_code: str, symbol: str, client: httpx.AsyncClient, limiter: RateLimiter,
) -> List[Dict[str, Any]]:
    async with limiter:
        r = await client.get(CA_API, params={"scripcode": scrip_code})
    try:
        data = r.json()
    except Exception:
        logger.warning(f"[BSE-CA] {symbol} ({scrip_code}): non-JSON response")
        return []
    records = data.get("Table2") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return []

    rows: List[Dict[str, Any]] = []
    seen = set()
    for rec in records:
        purpose = rec.get("purpose") or ""
        atype = _classify(purpose)
        if not atype:
            continue
        ex = _parse_bse_date(rec.get("Ex_date"))
        if not ex:
            continue
        key = (symbol, ex, atype)
        if key in seen:  # de-dupe within the feed before upsert
            continue
        seen.add(key)

        # Details is the numeric amount as a string for dividends/buybacks.
        value: Optional[float] = None
        details_raw = (str(rec.get("Details")) if rec.get("Details") is not None else "").strip()
        try:
            value = float(details_raw)
        except ValueError:
            value = _extract_value(purpose, atype)

        rows.append({
            "symbol": symbol,
            "ex_date": ex,
            "record_date": _parse_bcrd(rec.get("BCRD")),
            "action_type": atype,
            "value": value,
            "details": rec,
            "source": "BSE",
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


async def run(pairs: List[Tuple[str, str]]) -> Dict[str, int]:
    """pairs: (bse_scrip_code, symbol) — from bse_sync.bse_universe()."""
    limiter = RateLimiter(rate=2.0)
    stats = {"symbols": 0, "rows": 0, "empty": 0, "failed": 0}

    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        log_id = await start_sync_log(session, "corporate_actions", "bse")
        await session.commit()

    async with httpx.AsyncClient(timeout=30, headers=BSE_HEADERS) as client:
        for scrip_code, symbol in pairs:
            try:
                rows = await sync_scrip(scrip_code, symbol, client, limiter)
                n = await upsert_rows(rows)
            except Exception as e:
                stats["failed"] += 1
                logger.warning(f"[BSE-CA] {symbol} ({scrip_code}) failed: {e}")
                continue
            stats["symbols"] += 1
            stats["rows"] += n
            if not n:
                stats["empty"] += 1
            else:
                logger.info(f"[BSE-CA] {symbol}: {n} actions upserted")

    async with SessionFactory() as session:
        await complete_sync_log(
            session, log_id,
            "success" if stats["failed"] < max(1, len(pairs) // 2) else "failed",
            records_processed=stats["rows"], records_failed=stats["failed"],
            metadata=stats,
        )
        await session.commit()
    logger.info(f"[BSE-CA] DONE: {stats}")
    return stats


async def run_universe() -> Dict[str, int]:
    """Convenience entry: run over the onboarded BSE-only universe."""
    from bharat_terminal.kb.sync.bse_sync import bse_universe
    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        pairs = await bse_universe(session)
    if not pairs:
        logger.info("[BSE-CA] no BSE-only companies onboarded; nothing to do")
        return {"symbols": 0, "rows": 0, "empty": 0, "failed": 0}
    return await run(pairs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    asyncio.run(run_universe())
