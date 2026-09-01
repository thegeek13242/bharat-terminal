"""Official SEBI/AMFI industry classification for the whole universe.

WHY (2026-08-27): `companies.sector_nse` covered 750 of 3,998 rows and
`industry_nse` none. Those 750 came from NSE's index-constituent CSVs,
which list exactly 752 symbols — that source is EXHAUSTED and structurally
cannot reach the ~1,631 BSE-only companies, i.e. precisely the uncovered
tail where the direction-signals thesis places the edge.

BSE's company-page XHR serves the official 4-level tree per scrip:

    Sector      -> macro_sector     "Commodities"
    IndustryNew -> sector           "Construction Materials"
    IGroup      -> industry         "Cement & Cement Products"
    ISubGroup   -> basic_industry   "Cement & Cement Products"

(`ListofScripData` also exposes an INDUSTRY field — verified 0% populated
across 4,979 scrips, so it is not an alternative.)

Unblocks E1 estimator #4 (peer read-across needs industry peers),
sector-adjusted abnormal returns for the Stage 5 benchmark model, and E3
sector context.

Idempotent and resumable: only rows whose classification is missing (or
--refresh) are fetched, so an interrupted sweep resumes where it stopped.
Politely paced; one request per scrip.

  python -m bharat_terminal.kb.sync.bse_sector_sync --dry-run --limit 20
  python -m bharat_terminal.kb.sync.bse_sector_sync --execute
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

API = ("https://api.bseindia.com/BseIndiaAPI/api/ComHeadernew/w"
       "?quotetype=EQ&scripcode={code}&seriesid=")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}
DELAY_S = float(os.getenv("BSE_SECTOR_DELAY_S", "0.8"))
TIMEOUT = httpx.Timeout(20.0, connect=8.0)
RETRIES = 3
SOURCE = "BSE_ComHeadernew"

_TODO_SQL = """
SELECT symbol, bse_scrip_code FROM companies
WHERE bse_scrip_code IS NOT NULL AND is_active
  {only_missing}
ORDER BY symbol
{limit}
"""
_UPSERT_SQL = """
UPDATE companies
SET macro_sector = $2, sector = $3, industry = $4, basic_industry = $5,
    sector_source = $6, sector_updated_at = now()
WHERE symbol = $1
"""


# BSE writes these placeholders for unclassified scrips (verified live
# 2026-08-27 on BSE-listed DEBT instruments, e.g. scrip 543145 "08ADR",
# which returns industry "-"). Treating them as data would mark thousands
# of rows classified with junk and they would never be retried.
_NULLISH = {"", "-", "--", "NA", "N.A.", "N/A", "NIL", "NONE", "0"}


def _clean(v) -> Optional[str]:
    t = (v or "").strip()
    return None if t.upper() in _NULLISH else t


def parse_header(payload: dict) -> Optional[dict]:
    """Pull the 4 classification levels out of a ComHeadernew response.
    Returns None when the scrip carries no usable classification (debt
    instruments, delisted, odd segments) — the caller then records nothing
    rather than writing blanks that would block a future retry."""
    if not isinstance(payload, dict):
        return None
    out = {
        "macro_sector": _clean(payload.get("Sector")),
        "sector": _clean(payload.get("IndustryNew")),
        "industry": _clean(payload.get("IGroup")),
        "basic_industry": _clean(payload.get("ISubGroup")),
    }
    # The legacy flat `Industry` field is the fallback for the two mid
    # levels when the newer tree is absent on older scrips.
    legacy = _clean(payload.get("Industry"))
    if not out["industry"] and legacy:
        out["industry"] = legacy
    return out if any(out.values()) else None


async def fetch_one(client: httpx.AsyncClient, code: str) -> Optional[dict]:
    for attempt in range(1, RETRIES + 1):
        try:
            r = await client.get(API.format(code=code))
            r.raise_for_status()
            return parse_header(r.json())
        except Exception as e:
            if attempt == RETRIES:
                logger.warning(f"[SECTOR] {code}: {type(e).__name__}: {e}")
                return None
            await asyncio.sleep(1.5 * attempt)
    return None


async def run(execute: bool, limit: Optional[int], refresh: bool) -> int:
    import asyncpg
    dsn = (os.getenv("DATABASE_URL") or "").replace(
        "postgresql+asyncpg://", "postgresql://")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    sql = _TODO_SQL.format(
        only_missing="" if refresh else "AND industry IS NULL",
        limit=f"LIMIT {int(limit)}" if limit else "")
    conn = await asyncpg.connect(dsn=dsn, timeout=30, command_timeout=60)
    try:
        rows = await conn.fetch(sql)
        logger.info(f"[SECTOR] {len(rows)} companies to classify "
                    f"({'refresh' if refresh else 'missing only'})")
        stats = {"fetched": 0, "written": 0, "no_data": 0}
        async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT) as client:
            for i, r in enumerate(rows, 1):
                code = str(r["bse_scrip_code"]).split(".")[0]
                got = await fetch_one(client, code)
                stats["fetched"] += 1
                if not got:
                    stats["no_data"] += 1
                else:
                    if execute:
                        await conn.execute(
                            _UPSERT_SQL, r["symbol"], got["macro_sector"],
                            got["sector"], got["industry"],
                            got["basic_industry"], SOURCE)
                    stats["written"] += 1
                    if i <= 5 or i % 250 == 0:
                        logger.info(
                            f"[SECTOR] {i}/{len(rows)} {r['symbol']}: "
                            f"{got['macro_sector']} > {got['sector']} > "
                            f"{got['industry']}")
                await asyncio.sleep(DELAY_S)
        logger.info(f"[SECTOR] {'EXECUTE' if execute else 'DRY-RUN'} done: {stats}")
    finally:
        await conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="fetch + report, write nothing (default)")
    mode.add_argument("--execute", action="store_true", help="write to companies")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch companies that already have a classification")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(run(a.execute, a.limit, a.refresh))


if __name__ == "__main__":
    sys.exit(main())
