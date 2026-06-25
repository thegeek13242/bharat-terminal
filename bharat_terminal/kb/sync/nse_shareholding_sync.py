"""
NSE shareholding-pattern sync — populates `shareholding_patterns` from the
AUTHORITATIVE NSE source (no third-party vendors).

Pipeline per symbol:
  1. GET /api/corporate-share-holdings-master?symbol=X  → list of quarterly
     submissions, each with a `date` and an `xbrl` link.
  2. Download the SEBI shareholding XBRL (taxonomy `in-bse-shp`) and parse the
     category percentages from its dimensional contexts (CategoryOfShareholdersAxis):
        promoter_pct  ← ShareholdingOfPromoterAndPromoterGroup
        public_pct    ← PublicShareholding
        fii_pct       ← InstitutionsForeign  (Foreign Portfolio Investors)
        dii_pct       ← InstitutionsDomestic
        num_shareholders ← NumberOfShareholders (total)
     Percentages are stored in the XBRL as fractions (0.50 = 50%); we ×100.
  3. Upsert into shareholding_patterns on (symbol, quarter_end).

Reuses the proven NSE client/cookie handshake from nse_financial_results.
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

from bharat_terminal.kb.models import ShareholdingPattern
from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import create_async_session
from bharat_terminal.kb.sync.nse_financial_results import (
    NSE_BASE, create_nse_client, refresh_nse_cookies,
)

logger = logging.getLogger(__name__)

SHP_MASTER_API = f"{NSE_BASE}/api/corporate-share-holdings-master"
SHP_REFERER = f"{NSE_BASE}/companies-listing/corporate-filings-shareholding-pattern"

# CategoryOfShareholdersAxis member (local name, sans 'Member') → our column.
_CATEGORY_MAP = {
    "ShareholdingOfPromoterAndPromoterGroup": "promoter_pct",
    "PublicShareholding": "public_pct",
    "InstitutionsForeign": "fii_pct",
    "InstitutionsDomestic": "dii_pct",
}


def _local_facts(xml: str, localtag: str) -> Dict[str, str]:
    """contextRef → value for an XBRL tag, ignoring the namespace prefix."""
    pat = rf'<[\w-]+:{localtag} contextRef="([^"]+)"[^>]*>([^<]+)</[\w-]+:{localtag}>'
    return {cref: val.strip() for cref, val in re.findall(pat, xml)}


def parse_shp_xbrl(xml: str) -> Dict[str, Any]:
    """Parse a SEBI shareholding-pattern XBRL into our flat columns.

    Returns dict with promoter_pct/public_pct/fii_pct/dii_pct (percent, 0-100),
    promoter_pledge_pct (or None), num_shareholders (or None). Values absent in
    the filing are left None rather than guessed (data-accuracy rule).
    """
    # context id → category member local name (sans 'Member')
    ctx_cat: Dict[str, str] = {}
    for cid, body in re.findall(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>', xml, re.S):
        m = re.search(r'CategoryOfShareholdersAxis"[^>]*>\s*[\w-]+:(\w+?)Member', body)
        if m:
            ctx_cat[cid] = m.group(1)

    pct = _local_facts(xml, "ShareholdingAsAPercentageOfTotalNumberOfShares")
    nsh = _local_facts(xml, "NumberOfShareholders")

    def pct_raw(cat: str) -> Optional[float]:
        """Raw category percentage from the XBRL (NOT yet scaled)."""
        cid = f"{cat}_ContextI"
        if cid in pct:
            try:
                return float(pct[cid])
            except ValueError:
                return None
        vals = []
        for c, v in pct.items():
            if ctx_cat.get(c) == cat:
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
        return max(vals) if vals else None

    # Filings are inconsistent: some store percentages as fractions (0.50 = 50%),
    # others as whole numbers (50.0). Derive the scale from the grand total
    # (ShareholdingPattern ≈ 1.0 → ×100; ≈ 100 → ×1) so every value lands as 0-100.
    total = pct_raw("ShareholdingPattern")
    scale = 100.0 if (total is not None and total <= 1.5) else 1.0

    def pct_for(cat: str) -> Optional[float]:
        raw = pct_raw(cat)
        return round(raw * scale, 2) if raw is not None else None

    out: Dict[str, Any] = {col: pct_for(cat) for cat, col in _CATEGORY_MAP.items()}

    # Total shareholder count (the ShareholdingPattern/total context).
    total_holders = None
    for cid in (f"ShareholdingPattern_ContextI", "Total_ContextI"):
        if cid in nsh:
            try:
                total_holders = int(float(nsh[cid]))
            except ValueError:
                pass
            break
    if total_holders is None:
        # Fallback: holders at the total category, else promoter+public.
        for c, v in nsh.items():
            if ctx_cat.get(c) in ("ShareholdingPattern", "Total"):
                try:
                    total_holders = int(float(v)); break
                except ValueError:
                    pass
    out["num_shareholders"] = total_holders

    # Promoter pledge % — best-effort; only if the filing carries a pledge-% tag.
    pledge = _local_facts(
        xml, "SharesPledgedOrOtherwiseEncumberedAsAPercentageOfTotalNumberOfShares")
    pl = None
    for c, v in pledge.items():
        if ctx_cat.get(c, "").startswith("ShareholdingOfPromoter"):
            try:
                pl = round(float(v) * scale, 2); break
            except ValueError:
                pass
    out["promoter_pledge_pct"] = pl
    return out


def _parse_quarter_end(s: str) -> Optional[date]:
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


async def sync_symbol(
    symbol: str, client: httpx.AsyncClient, limiter: RateLimiter, quarters: int,
) -> List[Dict[str, Any]]:
    """Fetch + parse the latest `quarters` shareholding filings for one symbol."""
    symbol = symbol.upper().strip()
    async with limiter:
        r = await client.get(SHP_MASTER_API,
                             params={"index": "equities", "symbol": symbol},
                             headers={"Referer": SHP_REFERER})
    if "application/json" not in r.headers.get("content-type", ""):
        await refresh_nse_cookies(client)
        async with limiter:
            r = await client.get(SHP_MASTER_API,
                                 params={"index": "equities", "symbol": symbol},
                                 headers={"Referer": SHP_REFERER})
    try:
        records = r.json()
    except Exception:
        logger.warning(f"[SHP] {symbol}: non-JSON master response")
        return []
    if not isinstance(records, list) or not records:
        logger.info(f"[SHP] {symbol}: no shareholding submissions")
        return []

    rows: List[Dict[str, Any]] = []
    for rec in records[:quarters]:
        q_end = _parse_quarter_end(rec.get("date") or "")
        xbrl_url = rec.get("xbrl")
        if not q_end or not xbrl_url:
            continue
        try:
            async with limiter:
                xr = await client.get(xbrl_url, headers={"Referer": SHP_REFERER})
            parsed = parse_shp_xbrl(xr.text)
        except Exception as e:
            logger.warning(f"[SHP] {symbol} {q_end}: XBRL fetch/parse failed: {e}")
            continue

        # Cross-validation: promoter + public should ≈ 100.
        prom, pub = parsed.get("promoter_pct"), parsed.get("public_pct")
        if prom is not None and pub is not None and abs((prom + pub) - 100) > 1.5:
            logger.warning(
                f"[SHP] {symbol} {q_end}: promoter+public={prom+pub:.1f} "
                f"(≠100) — storing but flag")
        rows.append({
            "symbol": symbol,
            "quarter_end": q_end,
            "source": "NSE_XBRL_SHP",
            "raw_data": rec,
            **parsed,
        })
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


async def run(symbols: List[str], quarters: int = 4) -> Dict[str, int]:
    client = await create_nse_client()
    limiter = RateLimiter(rate=2.0)  # be gentle with NSE
    stats = {"symbols": 0, "rows": 0, "empty": 0}
    try:
        for sym in symbols:
            rows = await sync_symbol(sym, client, limiter, quarters)
            n = await upsert_rows(rows)
            stats["symbols"] += 1
            stats["rows"] += n
            if not n:
                stats["empty"] += 1
            logger.info(f"[SHP] {sym}: {n} quarters upserted")
    finally:
        await client.aclose()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(prog="nse_shareholding_sync")
    ap.add_argument("--symbols", help="Comma-separated; default = research universe")
    ap.add_argument("--quarters", type=int, default=4)
    args = ap.parse_args()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        from bharat_terminal.research.universe import RESEARCH_UNIVERSE
        symbols = sorted(RESEARCH_UNIVERSE)
    stats = asyncio.run(run(symbols, quarters=args.quarters))
    logger.info(f"[SHP] DONE: {stats}")


if __name__ == "__main__":
    main()
