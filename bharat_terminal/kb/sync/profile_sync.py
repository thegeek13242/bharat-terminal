"""
Company profile sync — populates company master fields from OFFICIAL NSE sources only.

No third-party data vendors. Sources (all on the NSE archives host, same one used
for bhavcopy and filings):
  - EQUITY_L.csv                  → ISIN + face value, full equity universe (~2,365)
  - ind_niftytotalmarket_list.csv → NSE Sector classification ("Industry" column),
                                    the broad investable universe (~755 large/mid/small).

Fields this populates on `companies`:
  - isin        (from EQUITY_L; authoritative)
  - sector_nse  (from the index "Industry" column = NSE Sector level)
  - face_value  (from EQUITY_L; not stored on companies but returned for callers)

Deliberately NOT populated here (no official archive source):
  - industry_nse: the finer NSE Basic-Industry level lives only behind the
    Akamai-protected /api/quote-equity endpoint (HTTP 403 from this network).
  - mcap_cr / description_200w / embedding: handled elsewhere.
"""
import csv
import io
import logging
from typing import Dict, Optional, Any

import httpx
from sqlalchemy import select, update, text

from bharat_terminal.kb.models import Company
from bharat_terminal.kb.sync.utils import create_async_session

logger = logging.getLogger(__name__)

NSE_ARCHIVES = "https://nsearchives.nseindia.com"
EQUITY_MASTER_URL = f"{NSE_ARCHIVES}/content/equities/EQUITY_L.csv"
# Nifty Total Market = the broad investable universe (large + mid + small cap).
INDEX_SECTOR_URL = f"{NSE_ARCHIVES}/content/indices/ind_niftytotalmarket_list.csv"

# A homepage hit first sets the cookies the archives host expects.
NSE_HOME = "https://www.nseindia.com/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/octet-stream,*/*",
    "Referer": NSE_HOME,
}


async def _get_csv(client: httpx.AsyncClient, url: str) -> list:
    """Download a CSV and return a list of dict rows (keys stripped)."""
    resp = await client.get(url)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in raw.items()})
    return rows


async def fetch_equity_master(client: httpx.AsyncClient) -> Dict[str, Dict[str, Any]]:
    """symbol → {isin, face_value, name} from EQUITY_L.csv (official NSE master)."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in await _get_csv(client, EQUITY_MASTER_URL):
        sym = r.get("SYMBOL", "")
        isin = r.get("ISIN NUMBER", "") or None
        if not sym:
            continue
        try:
            fv = float(r.get("FACE VALUE", "") or 0) or None
        except ValueError:
            fv = None
        out[sym] = {"isin": isin, "face_value": fv, "name": r.get("NAME OF COMPANY")}
    return out


async def fetch_index_sectors(client: httpx.AsyncClient) -> Dict[str, str]:
    """symbol → NSE Sector, from the Nifty Total Market constituent list."""
    out: Dict[str, str] = {}
    for r in await _get_csv(client, INDEX_SECTOR_URL):
        sym = r.get("Symbol", "")
        sector = r.get("Industry", "")  # NSE labels the Sector level "Industry" here
        if sym and sector:
            out[sym] = sector
    return out


async def sync_company_profiles() -> Dict[str, int]:
    """
    Update companies.isin and companies.sector_nse from official NSE files.
    Only writes a field when we have a value for it (never nulls existing data).
    Returns counts for reporting.
    """
    stats = {"isin_set": 0, "sector_set": 0, "symbols_seen": 0, "no_match": 0}

    async with httpx.AsyncClient(
        timeout=30, headers=_HEADERS, follow_redirects=True
    ) as client:
        # Warm cookies on the main host before pulling from the archives host.
        try:
            await client.get(NSE_HOME, timeout=15)
        except Exception:
            pass
        equity = await fetch_equity_master(client)
        sectors = await fetch_index_sectors(client)

    logger.info(
        "profile_sync: EQUITY_L=%d symbols, index sectors=%d symbols",
        len(equity), len(sectors),
    )

    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        symbols = [
            row[0] for row in (await session.execute(select(Company.symbol))).all()
        ]
        for sym in symbols:
            stats["symbols_seen"] += 1
            values: Dict[str, Any] = {}
            em = equity.get(sym)
            if em and em.get("isin"):
                values["isin"] = em["isin"]
            sec = sectors.get(sym)
            if sec:
                values["sector_nse"] = sec
            if not values:
                stats["no_match"] += 1
                continue
            await session.execute(
                update(Company.__table__).where(Company.symbol == sym).values(**values)
            )
            if "isin" in values:
                stats["isin_set"] += 1
            if "sector_nse" in values:
                stats["sector_set"] += 1
        await session.commit()

    logger.info("profile_sync done: %s", stats)
    return stats


# Market cap is computed internally from official inputs — no external feed:
#   shares      = paid_up_capital (₹cr, from XBRL) / face_value
#   mcap_cr     = shares * latest_close (from NSE bhavcopy)
#             = paid_up_capital_cr * close / face_value
# Sanity bounds reject filings whose secondary fields are garbage (e.g. FaceValue
# 5441, or an absurd paid-up capital) so a bad share count never yields a wrong mcap;
# such rows are left NULL rather than written wrong.
_MCAP_SQL = text(
    """
    WITH latest_fin AS (
      SELECT DISTINCT ON (symbol) symbol, paid_up_capital_cr, face_value
      FROM financial_results
      WHERE paid_up_capital_cr IS NOT NULL AND face_value IS NOT NULL
      ORDER BY symbol, period_end DESC
    ),
    latest_px AS (
      SELECT DISTINCT ON (symbol) symbol, close
      FROM price_points WHERE close > 0 ORDER BY symbol, date DESC
    ),
    computed AS (
      SELECT f.symbol,
             round((f.paid_up_capital_cr * p.close / f.face_value)::numeric, 2) AS mcap
      FROM latest_fin f JOIN latest_px p USING (symbol)
      WHERE f.face_value BETWEEN 1 AND 1000 AND f.paid_up_capital_cr > 0
    )
    UPDATE companies c SET mcap_cr = computed.mcap, updated_at = now()
    FROM computed
    WHERE c.symbol = computed.symbol
      AND computed.mcap BETWEEN 1 AND 3000000
    """
)


async def sync_market_caps() -> Dict[str, int]:
    """Recompute companies.mcap_cr from latest financials × latest bhavcopy close."""
    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        result = await session.execute(_MCAP_SQL)
        await session.commit()
        n = result.rowcount or 0
    logger.info("sync_market_caps: updated %d companies", n)
    return {"mcap_set": n}


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(sync_company_profiles()))
    print(asyncio.run(sync_market_caps()))
