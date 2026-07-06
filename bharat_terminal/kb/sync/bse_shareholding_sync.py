"""
BSE shareholding-pattern sync — populates `shareholding_patterns` for the
BSE-only universe from BSE's public XBRL index.

Pipeline per scrip:
  1. GET /BseIndiaAPI/api/GetCorXbrlDetails_ng/w?Flag=23&scripcode=X
     &fromdate=YYYY/MM/DD&todate=YYYY/MM/DD → rows with `xbrlurl`
     (e.g. "/XBRL1/500002_..._SHP.xml") and `xbrldate` (filing date).
  2. Download https://www.bseindia.com{xbrlurl}. The document uses the same
     SEBI shareholding taxonomy as NSE's filings (verified live 2026-07-03:
     ShareholdingOfPromoterAndPromoterGroup / InstitutionsForeign / ... tags
     present), so parsing is delegated to the proven
     nse_shareholding_sync.parse_shp_xbrl.
  3. quarter_end: the XBRL index does not carry it; derive it as the MODAL
     xbrli:instant in the document (the reporting quarter dominates — prior
     quarters appear once each as comparatives).
  4. Upsert on (symbol, quarter_end), source="BSE_XBRL_SHP".
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.kb.models import ShareholdingPattern
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import (
    complete_sync_log, create_async_session, start_sync_log,
)
from bharat_terminal.kb.sync.bse_sync import BSE_HEADERS
from bharat_terminal.kb.sync.nse_shareholding_sync import parse_shp_xbrl

logger = logging.getLogger(__name__)

XBRL_INDEX_API = "https://api.bseindia.com/BseIndiaAPI/api/GetCorXbrlDetails_ng/w"
XBRL_HOST = "https://www.bseindia.com"
SHP_FLAG = "23"  # GetCorXbrlDropdown_ng category: 23 = Shareholding

_INSTANT_RE = re.compile(r"<xbrli:instant>(\d{4}-\d{2}-\d{2})</xbrli:instant>")


def quarter_end_from_xbrl(xml: str) -> Optional[date]:
    """Modal xbrli:instant = the reporting quarter end (comparative quarters
    appear once each). None if the document carries no instants."""
    dates = _INSTANT_RE.findall(xml)
    if not dates:
        return None
    modal, _count = Counter(dates).most_common(1)[0]
    try:
        return datetime.strptime(modal, "%Y-%m-%d").date()
    except ValueError:
        return None


async def sync_scrip(
    scrip_code: str, symbol: str, client: httpx.AsyncClient, limiter: RateLimiter,
    quarters: int,
) -> List[Dict[str, Any]]:
    """Fetch + parse up to `quarters` recent SHP XBRLs for one scrip."""
    window_days = quarters * 92 + 45  # filing lag margin
    todt = datetime.now().date()
    fromdt = todt - timedelta(days=window_days)
    params = {
        "Flag": SHP_FLAG,
        "scripcode": scrip_code,
        "fromdate": fromdt.strftime("%Y/%m/%d"),
        "todate": todt.strftime("%Y/%m/%d"),
    }
    async with limiter:
        r = await client.get(XBRL_INDEX_API, params=params)
    try:
        entries = (r.json() or {}).get("Table") or []
    except Exception:
        logger.warning(f"[BSE-SHP] {symbol} ({scrip_code}): non-JSON index response")
        return []

    rows: List[Dict[str, Any]] = []
    seen_quarters: set = set()
    for rec in entries[: quarters * 2]:  # revisions can duplicate a quarter
        xbrl_path = (rec.get("xbrlurl") or "").strip()
        if not xbrl_path:
            continue
        url = xbrl_path if xbrl_path.startswith("http") else f"{XBRL_HOST}{xbrl_path}"
        try:
            async with limiter:
                xr = await client.get(url)
            xr.raise_for_status()
            xml = xr.text
            q_end = quarter_end_from_xbrl(xml)
            if not q_end:
                logger.warning(f"[BSE-SHP] {symbol}: no quarter end in {url}")
                continue
            if q_end in seen_quarters:  # index is newest-first; keep the latest revision
                continue
            parsed = parse_shp_xbrl(xml)
        except Exception as e:
            logger.warning(f"[BSE-SHP] {symbol} ({scrip_code}): XBRL fetch/parse failed: {e}")
            continue

        # Cross-validation: promoter + public should ≈ 100 (data-accuracy rule).
        prom, pub = parsed.get("promoter_pct"), parsed.get("public_pct")
        if prom is not None and pub is not None and abs((prom + pub) - 100) > 1.5:
            logger.warning(
                f"[BSE-SHP] {symbol} {q_end}: promoter+public={prom + pub:.1f} "
                f"(≠100) — storing but flag")
        seen_quarters.add(q_end)
        rows.append({
            "symbol": symbol,
            "quarter_end": q_end,
            "source": "BSE_XBRL_SHP",
            "raw_data": rec,
            **parsed,
        })
        if len(seen_quarters) >= quarters:
            break
    return rows


async def upsert_rows(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        for v in rows:
            stmt = pg_insert(ShareholdingPattern.__table__).values(**v)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "quarter_end"],
                set_={k: stmt.excluded[k] for k in v if k not in ("symbol", "quarter_end")},
            )
            await session.execute(stmt)
        await session.commit()
    return len(rows)


async def run(pairs: List[Tuple[str, str]], quarters: int = 4) -> Dict[str, int]:
    """pairs: (bse_scrip_code, symbol) — from bse_sync.bse_universe()."""
    limiter = RateLimiter(rate=2.0)
    stats = {"symbols": 0, "rows": 0, "empty": 0, "failed": 0}

    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        log_id = await start_sync_log(session, "shareholding", "bse")
        await session.commit()

    async with httpx.AsyncClient(timeout=30, headers=BSE_HEADERS) as client:
        for scrip_code, symbol in pairs:
            try:
                rows = await sync_scrip(scrip_code, symbol, client, limiter, quarters)
                n = await upsert_rows(rows)
            except Exception as e:
                stats["failed"] += 1
                logger.warning(f"[BSE-SHP] {symbol} ({scrip_code}) failed: {e}")
                continue
            stats["symbols"] += 1
            stats["rows"] += n
            if not n:
                stats["empty"] += 1
            else:
                logger.info(f"[BSE-SHP] {symbol}: {n} quarters upserted")

    async with SessionFactory() as session:
        await complete_sync_log(
            session, log_id,
            "success" if stats["failed"] < max(1, len(pairs) // 2) else "failed",
            records_processed=stats["rows"], records_failed=stats["failed"],
            metadata=stats,
        )
        await session.commit()
    logger.info(f"[BSE-SHP] DONE: {stats}")
    return stats


async def run_universe(quarters: int = 4) -> Dict[str, int]:
    from bharat_terminal.kb.sync.bse_sync import bse_universe
    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        pairs = await bse_universe(session)
    if not pairs:
        logger.info("[BSE-SHP] no BSE-only companies onboarded; nothing to do")
        return {"symbols": 0, "rows": 0, "empty": 0, "failed": 0}
    return await run(pairs, quarters=quarters)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    asyncio.run(run_universe())
