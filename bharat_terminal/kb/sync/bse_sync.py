"""
BSE scrip master sync — onboards BSE-only listed companies into the KB.

BSE identifies securities by a 6-digit numeric scrip code; `companies.symbol`
for onboarded BSE-only rows is the alphanumeric BSE `scrip_id` (e.g. "NSDL"),
with the numeric code kept in `companies.bse_scrip_code` for API calls.

Scope (user decision 2026-07-03): "clean group" BSE-only companies —
groups A/B/X/P/R/Y/IP. Trade-to-trade (T/XT/TS), defaulter (Z/ZP) and SME
(M/MT/MS) groups are excluded. "BSE-only" is determined by ISIN absence
from the existing companies table.

Dual-listed rows (ISIN already present) only get `bse_scrip_code` and
`bse_group` backfilled — their symbol/exchange are never touched, and their
filings keep flowing exclusively through the NSE adapter (no double LLM
analysis; see BSEFilingsAdapter's exchange='BSE' scrip-map filter).

Universe refresh is a manual CLI operation (`sync-bse-companies --execute`);
it is deliberately NOT scheduled in the monitor — auto-inserting companies
into production weekly needs its own approval.
"""
import logging
import re
from typing import Dict, List, Optional, Set, Tuple

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bharat_terminal.kb.sync.utils import (
    complete_sync_log,
    create_async_session,
    start_sync_log,
)

logger = logging.getLogger(__name__)

# Single place all BSE modules import their headers from: api.bseindia.com
# returns errors/empty bodies without a browser UA + bseindia referer.
BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}

SCRIP_MASTER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)

# Explicit allowlist (not an exclusion list): a new/unknown BSE group is
# skipped and logged rather than silently onboarded.
ALLOWED_GROUPS = {"A", "B", "X", "P", "R", "Y", "IP"}

_SYMBOL_RE = re.compile(r"^[A-Z0-9&-]{1,20}$")


async def fetch_bse_scrip_list() -> List[Dict]:
    """Fetch the BSE active-equity scrip master. Returns normalized dicts:
    scrip_code (str), scrip_id, group, isin, issuer_name, mktcap_cr, face_value.
    """
    async with httpx.AsyncClient(timeout=60, headers=BSE_HEADERS) as client:
        resp = await client.get(SCRIP_MASTER_URL)
        resp.raise_for_status()
        rows = resp.json()
    if not isinstance(rows, list):
        raise ValueError(f"BSE scrip master: expected list, got {type(rows).__name__}")

    out: List[Dict] = []
    for r in rows:
        scrip_code = str(r.get("SCRIP_CD") or "").strip()
        if not scrip_code:
            continue
        try:
            mktcap = float(r.get("Mktcap")) if r.get("Mktcap") else None
        except (TypeError, ValueError):
            mktcap = None
        out.append({
            "scrip_code": scrip_code,
            "scrip_id": (r.get("scrip_id") or "").strip(),
            "group": (r.get("GROUP") or "").strip(),
            "isin": (r.get("ISIN_NUMBER") or "").strip(),
            "issuer_name": (r.get("Issuer_Name") or r.get("Scrip_Name") or "").strip(),
            "mktcap_cr": mktcap,
            "face_value": (r.get("FACE_VALUE") or "").strip(),
        })
    logger.info(f"[bse_sync] scrip master fetched: {len(out)} active equity scrips")
    return out


def resolve_symbol(scrip_id: str, scrip_code: str, taken: Set[str]) -> Tuple[str, bool]:
    """Deterministic symbol for a BSE-only company.

    Prefer the BSE scrip_id (matches Angel One's BSE symbol field, so live
    LTP resolves by name). Fall back to "BSE{scrip_code}" — always unique
    because SCRIP_CD is unique — when the scrip_id is missing, malformed, or
    collides with an existing symbol. Returns (symbol, used_fallback).
    """
    candidate = (scrip_id or "").strip().upper()
    if candidate and _SYMBOL_RE.match(candidate) and candidate not in taken:
        return candidate, False
    fallback = f"BSE{scrip_code}"
    if fallback in taken:  # cannot happen while SCRIP_CD is unique; belt-and-braces
        raise ValueError(f"symbol fallback collision for scrip {scrip_code}")
    return fallback, True


async def bse_universe(session: AsyncSession) -> List[Tuple[str, str]]:
    """(scrip_code, symbol) pairs for the onboarded BSE-only universe,
    in deterministic scrip_code order. Shared by all BSE sync jobs."""
    from bharat_terminal.kb.models import Company
    result = await session.execute(
        select(Company.bse_scrip_code, Company.symbol)
        .where(
            Company.exchange == "BSE",
            Company.bse_scrip_code.isnot(None),
            Company.is_active.is_(True),
        )
        .order_by(Company.bse_scrip_code)
    )
    return [(code, sym) for code, sym in result.all()]


async def sync_bse_companies(dry_run: bool = True) -> Dict:
    """Onboard BSE-only companies + backfill scrip codes for dual-listed rows.

    dry_run=True (default) computes and prints everything, then rolls back.
    Writing to the companies table requires the explicit --execute flag.
    """
    from bharat_terminal.kb.models import Company

    scrips = await fetch_bse_scrip_list()

    session_maker = create_async_session()
    async with session_maker() as session:
        log_id = await start_sync_log(session, "company_master", "bse")

        result = await session.execute(
            select(Company.symbol, Company.isin, Company.bse_scrip_code, Company.bse_group)
        )
        existing_rows = result.all()
        taken: Set[str] = {r[0] for r in existing_rows}
        by_isin: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {
            r[1]: (r[0], r[2], r[3]) for r in existing_rows if r[1]
        }

        stats = {
            "inserted": 0, "backfilled": 0, "skipped_no_isin": 0,
            "skipped_group": 0, "symbol_fallbacks": 0, "unknown_groups": {},
        }
        group_histogram: Dict[str, int] = {}
        preview: List[str] = []

        # scrip_code order → deterministic symbol assignment across re-runs
        for s in sorted(scrips, key=lambda r: r["scrip_code"]):
            group = s["group"]
            if group not in ALLOWED_GROUPS:
                stats["skipped_group"] += 1
                if group not in ALLOWED_GROUPS | {"T", "XT", "TS", "Z", "ZP", "M", "MT", "MS"}:
                    stats["unknown_groups"][group] = stats["unknown_groups"].get(group, 0) + 1
                continue
            if not s["isin"]:
                stats["skipped_no_isin"] += 1
                continue

            if s["isin"] in by_isin:
                # Dual-listed (or already onboarded): enrich only, never mutate identity.
                symbol, cur_code, cur_group = by_isin[s["isin"]]
                if cur_code is None or cur_group is None:
                    company = (
                        await session.execute(select(Company).where(Company.symbol == symbol))
                    ).scalar_one()
                    if company.bse_scrip_code is None:
                        company.bse_scrip_code = s["scrip_code"]
                    if company.bse_group is None:
                        company.bse_group = group
                    stats["backfilled"] += 1
                continue

            symbol, used_fallback = resolve_symbol(s["scrip_id"], s["scrip_code"], taken)
            if used_fallback:
                stats["symbol_fallbacks"] += 1
                logger.warning(
                    f"[bse_sync] symbol fallback {symbol}: scrip_id={s['scrip_id']!r} "
                    f"scrip_code={s['scrip_code']}"
                )
            taken.add(symbol)
            session.add(Company(
                symbol=symbol,
                isin=s["isin"],
                exchange="BSE",
                company_name=s["issuer_name"] or symbol,
                aliases=[a for a in {s["scrip_id"], s["issuer_name"]} if a],
                bse_group=group,
                bse_scrip_code=s["scrip_code"],
                mcap_cr=s["mktcap_cr"],
                is_active=True,
                data_quality_score=0.1,
            ))
            group_histogram[group] = group_histogram.get(group, 0) + 1
            stats["inserted"] += 1
            if len(preview) < 20:
                preview.append(
                    f"  {symbol:<12} {s['scrip_code']} {group:<3} "
                    f"mcap={s['mktcap_cr']} {s['issuer_name'][:40]}"
                )

        stats["group_histogram"] = group_histogram
        logger.info(f"[bse_sync] {'DRY RUN — ' if dry_run else ''}summary: {stats}")
        for line in preview:
            logger.info(line)

        if dry_run:
            await session.rollback()
        else:
            await complete_sync_log(
                session, log_id, "success", records_processed=stats["inserted"] + stats["backfilled"],
                metadata={k: v for k, v in stats.items() if k != "group_histogram"},
            )
            await session.commit()
        return stats
