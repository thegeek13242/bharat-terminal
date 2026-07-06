"""
BSE financial-results sync — structured results (XBRL) for the BSE-only
universe.

Chain (verified live 2026-07-03 against UHM Vacation 544779 / NSDL 544467):
  1. GET /BseIndiaAPI/api/Corp_FinanceResult_ng/w
     ?SCRIP_CD=X&FlagDur=<window>&HFQ=&ISUBGROUP_CODE=
     FlagDur: 1=today, 2=1wk, 3=15d, 4=1mo, 5=3mo, 6=1yr, 7=older.
     HFQ empty = all period types (3=Q, 1=H, 2=Y). Rows carry `XMLName` and
     `Consol_XMLName` (standalone / consolidated variants).
  2. https://www.bseindia.com/XBRLFILES/{XMLName with .html → .xml} is a plain
     SEBI integrated-filing XBRL instance (in-capmkt taxonomy — the same one
     NSE serves), so parsing is delegated to the proven
     nse_financial_results.parse_xbrl. Verified: full P&L + EPS extracted with
     coherent arithmetic (income − expenses = PBT; PBT − tax = PAT).
  3. _upsert_financial_result(source="bse_xbrl") — the period, audited and
     consolidated flags come from the XBRL's own metadata.

The endpoint intermittently returns an EMPTY body on long windows (observed
once, succeeded on retry) — each index call retries once.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx

from bharat_terminal.kb.sync.rate_limiter import RateLimiter
from bharat_terminal.kb.sync.utils import (
    complete_sync_log, create_async_session, start_sync_log,
)
from bharat_terminal.kb.sync.bse_sync import BSE_HEADERS
from bharat_terminal.kb.sync.nse_financial_results import parse_xbrl

logger = logging.getLogger(__name__)

RESULTS_INDEX_API = "https://api.bseindia.com/BseIndiaAPI/api/Corp_FinanceResult_ng/w"
XBRL_FILE_URL = "https://www.bseindia.com/XBRLFILES/{name}"

# BSE integrated-filing filenames end "..._IF<Type>.html" — NSE's
# classify_taxonomy regex doesn't match this shape, so map the suffix here.
# Only IFIndAs is verified live; the other tokens follow the template names —
# an unknown suffix falls back to 'indas' and is logged loudly.
_SUFFIX_RE = re.compile(r"_IF([A-Za-z]+)\.(?:html|xml)$", re.I)
_SUFFIX_TO_TAXONOMY = {
    "INDAS": "indas",
    "BANKING": "banking",
    "BANK": "banking",
    "NBFC": "nbfc",
    "NBFCINDAS": "nbfc",
    "NONINDAS": "nonindas",
    "GI": "general_insurance",
    "LI": "life_insurance",
}


def classify_bse_taxonomy(xml_name: str) -> str:
    m = _SUFFIX_RE.search(xml_name or "")
    if m:
        tax = _SUFFIX_TO_TAXONOMY.get(m.group(1).upper())
        if tax:
            return tax
        logger.warning(f"[BSE-FIN] unknown XBRL template suffix in {xml_name!r} — using indas")
    return "indas"


def flagdurs_for_days(since_days: int) -> List[str]:
    """FlagDur windows covering `since_days`. Values beyond a year need BOTH
    "6" (last year) and "7" (beyond last year) — they don't overlap."""
    for flag, limit in (("1", 1), ("2", 7), ("3", 15), ("4", 31), ("5", 93), ("6", 366)):
        if since_days <= limit:
            return [flag]
    return ["6", "7"]


def _xml_url(xml_name: str) -> str:
    name = re.sub(r"\.html$", ".xml", xml_name.strip(), flags=re.I)
    return XBRL_FILE_URL.format(name=name)


# Reporting-period span limits (days). Quarter ≈ 90; half-year ≈ 182 (many BSE
# smallcaps file half-yearly); anything longer is an annual/cumulative document
# whose period_end collides with Q4 on the upsert key — skip those outright.
_QUARTER_MAX_SPAN = 120
_HALFYEAR_MAX_SPAN = 200


async def _validated_upsert(SessionFactory, symbol: str, parsed: dict, name: str,
                            announced_at=None) -> str:
    """Upsert one parsed results document behind three accuracy gates.
    Returns one of: upserted | skipped_annual | skipped_halfyear_covered |
    scale_suspect | no_period.
    """
    meta = parsed.get("metadata") or {}
    p_start, p_end = meta.get("period_start"), meta.get("period_end")
    if not p_start or not p_end:
        return "no_period"

    # Gate 1: period span. Annual/cumulative docs share period_end with Q4 and
    # would silently overwrite quarterly rows (period_type is fixed 'quarterly').
    span = (p_end - p_start).days
    if span > _HALFYEAR_MAX_SPAN:
        return "skipped_annual"

    # Gate 2 (warn-only): implied-EPS consistency. All monetary facts scale by
    # the SAME divisor, so this cannot catch a uniform scale error (that is
    # what force_absolute_inr fixes at the root) — what it does catch is
    # garbage capital/face-value fields, which per the parser's doctrine must
    # never drop an otherwise-good P&L. Flag + store; audit later.
    q = parsed.get("quarter") or {}
    pat, eps = q.get("pat_cr"), q.get("eps_basic")
    capital, fv = q.get("paid_up_capital_cr"), q.get("face_value")
    flagged = False
    if (pat is not None and eps and capital and fv
            and abs(pat) > 0.5 and abs(eps) > 0.05 and capital > 0 and 0 < fv <= 1000):
        implied_eps = pat / (capital / fv)
        ratio = abs(implied_eps / eps)
        if not (1 / 3) <= ratio <= 3:
            flagged = True
            logger.warning(
                f"[BSE-FIN] {symbol}: EPS/capital inconsistency in {name} — reported "
                f"EPS {eps}, implied {implied_eps:.2f} (PAT {pat} Cr / capital "
                f"{capital} Cr @ FV {fv}) — stored, flagged for audit"
            )

    from bharat_terminal.kb.sync.pipeline import _upsert_financial_result
    is_consolidated = "consolidated" in (meta.get("consolidated") or "").lower()

    async with SessionFactory() as session:
        # Gate 3: a half-year document must not overwrite an existing finer
        # (quarterly) row that ends on the same date.
        if span > _QUARTER_MAX_SPAN:
            from sqlalchemy import select
            from bharat_terminal.kb.models import FinancialResult
            existing = await session.execute(
                select(FinancialResult.period_start).where(
                    FinancialResult.symbol == symbol,
                    FinancialResult.period_end == p_end,
                    FinancialResult.period_type == "quarterly",
                    FinancialResult.consolidated == is_consolidated,
                )
            )
            row = existing.first()
            if row and row[0] and row[0] > p_start:
                return "skipped_halfyear_covered"

        ok = await _upsert_financial_result(
            session, symbol, filing_meta={}, parsed=parsed,
            nse_filing_id=None, source="bse_xbrl", announced_at=announced_at,
        )
        if ok:
            # PEAD surprise for this quarter (deterministic, fail-open); same
            # transaction so the surprise/signal lands with the result row.
            from bharat_terminal.research.pead_signals import compute_surprise
            await compute_surprise(session, symbol, p_end, announced_at)
            await session.commit()
            return "scale_suspect" if flagged else "upserted"
    return "no_period"


# BSE results-index announcement timestamp: Fld_CreateDate is a naive-IST ISO
# string ("2026-07-03T18:41:12.65"); DT_TM is a display string. Parse the ISO
# form → UTC; None when absent (older index rows).
_IST = timezone(timedelta(hours=5, minutes=30))


def _index_announced_at(rec: dict):
    raw = (rec.get("Fld_CreateDate") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=_IST).astimezone(timezone.utc)
    except ValueError:
        return None


async def _fetch_index(
    scrip_code: str, client: httpx.AsyncClient, limiter: RateLimiter, flagdur: str,
) -> Optional[List[dict]]:
    """The index endpoint is flaky on long windows — empty bodies and read
    timeouts both observed transiently (2026-07-03), each fine on retry."""
    params = {"SCRIP_CD": scrip_code, "FlagDur": flagdur, "HFQ": "", "ISUBGROUP_CODE": ""}
    for attempt in (1, 2, 3):
        try:
            async with limiter:
                r = await client.get(RESULTS_INDEX_API, params=params, timeout=60.0)
            body = r.text.strip()
            if body:
                return (r.json() or {}).get("Table") or []
        except (httpx.HTTPError, ValueError) as e:
            logger.debug(f"[BSE-FIN] index attempt {attempt} failed "
                         f"(scrip={scrip_code or 'ALL'}, FlagDur={flagdur}): {e}")
        if attempt < 3:
            await asyncio.sleep(2.0 * attempt)
    return None


_COUNT_KEYS = ("upserted", "xml_missing", "parse_failed", "skipped_annual",
               "skipped_halfyear_covered", "scale_suspect")


async def sync_scrip(
    scrip_code: str, symbol: str, client: httpx.AsyncClient, limiter: RateLimiter,
    since_days: int,
) -> Dict[str, int]:
    counts = {k: 0 for k in _COUNT_KEYS}

    # name -> announcement timestamp (first index rec wins). Announcement
    # dates ride along so financial_results.announced_at + PEAD get anchored.
    xml_names: Dict[str, Optional[datetime]] = {}
    for flagdur in flagdurs_for_days(since_days):
        entries = await _fetch_index(scrip_code, client, limiter, flagdur)
        if entries is None:
            logger.warning(f"[BSE-FIN] {symbol} ({scrip_code}): index unreadable "
                           f"(FlagDur={flagdur}) after retries")
            counts["parse_failed"] += 1
            continue
        # Standalone + consolidated variants are separate documents; the upsert
        # conflict key includes the consolidated flag, so both are kept.
        for rec in entries:
            ann = _index_announced_at(rec)
            for field in ("XMLName", "Consol_XMLName"):
                name = (rec.get(field) or "").strip()
                if name and name not in xml_names:
                    xml_names[name] = ann

    SessionFactory = create_async_session()
    for name, announced_at in xml_names.items():
        try:
            async with limiter:
                xr = await client.get(_xml_url(name))
            if xr.status_code == 404:
                counts["xml_missing"] += 1
                continue
            xr.raise_for_status()
            parsed = parse_xbrl(xr.content, taxonomy=classify_bse_taxonomy(name),
                                force_absolute_inr=True)
        except Exception as e:
            counts["parse_failed"] += 1
            logger.warning(f"[BSE-FIN] {symbol} ({scrip_code}): XBRL fetch/parse failed "
                           f"for {name}: {e}")
            continue

        outcome = await _validated_upsert(SessionFactory, symbol, parsed, name,
                                          announced_at=announced_at)
        counts[outcome if outcome in counts else "parse_failed"] += 1
        if outcome == "no_period":
            logger.warning(f"[BSE-FIN] {symbol}: no reporting period in {name} — skipped")
    return counts


async def run(pairs: List[Tuple[str, str]], since_days: int = 10) -> Dict[str, int]:
    """pairs: (bse_scrip_code, symbol) — from bse_sync.bse_universe().
    since_days > 366 fetches the COMPLETE history (FlagDur 6 + 7, back to the
    start of BSE results-XBRL publishing ~2018)."""
    limiter = RateLimiter(rate=2.0)
    stats = {"symbols": 0, "failed": 0, **{k: 0 for k in _COUNT_KEYS}}

    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        log_id = await start_sync_log(session, "financials", "bse_xbrl")
        await session.commit()

    async with httpx.AsyncClient(timeout=30, headers=BSE_HEADERS) as client:
        for i, (scrip_code, symbol) in enumerate(pairs, 1):
            try:
                counts = await sync_scrip(scrip_code, symbol, client, limiter, since_days)
            except Exception as e:
                stats["failed"] += 1
                logger.warning(f"[BSE-FIN] {symbol} ({scrip_code}) failed: {e}")
                continue
            stats["symbols"] += 1
            for k in _COUNT_KEYS:
                stats[k] += counts[k]
            if i % 100 == 0:
                logger.info(f"[BSE-FIN] progress {i}/{len(pairs)}: {stats}")

    async with SessionFactory() as session:
        await complete_sync_log(
            session, log_id,
            "success" if stats["failed"] < max(1, len(pairs) // 2) else "failed",
            records_processed=stats["upserted"], records_failed=stats["failed"],
            metadata=stats,
        )
        await session.commit()
    logger.info(f"[BSE-FIN] DONE: {stats}")
    return stats


async def run_market_scan(since_days: int = 1) -> Dict[str, int]:
    """Daily variant: ONE market-wide index request (SCRIP_CD empty returns
    every filer in the window), filtered to the onboarded universe — instead
    of 1,631 per-scrip queries. Keeps structured BSE results same-day fresh;
    the weekly per-scrip sweep remains the completeness safety net."""
    from bharat_terminal.kb.sync.bse_sync import bse_universe

    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        pairs = await bse_universe(session)
    if not pairs:
        logger.info("[BSE-FIN] market scan: universe empty; nothing to do")
        return {"filers": 0, "matched": 0, "upserted": 0, "xml_missing": 0, "parse_failed": 0}
    code_to_symbol = {code: sym for code, sym in pairs}

    stats = {"filers": 0, "matched": 0, **{k: 0 for k in _COUNT_KEYS}}
    limiter = RateLimiter(rate=2.0)

    async with SessionFactory() as session:
        log_id = await start_sync_log(session, "financials", "bse_xbrl_daily")
        await session.commit()

    async with httpx.AsyncClient(timeout=60, headers=BSE_HEADERS) as client:
        entries = await _fetch_index("", client, limiter, flagdurs_for_days(since_days)[0])
        if entries is None:
            entries = []
            stats["parse_failed"] += 1
        # A scrip can appear with several rows (Q/H/Y × standalone/consolidated).
        by_symbol: Dict[str, Dict[str, Optional[datetime]]] = {}
        for rec in entries:
            stats["filers"] += 1
            symbol = code_to_symbol.get(str(rec.get("Scrip_cd") or "").strip())
            if not symbol:
                continue
            ann = _index_announced_at(rec)
            for field in ("XMLName", "Consol_XMLName"):
                name = (rec.get(field) or "").strip()
                if name and name not in by_symbol.setdefault(symbol, {}):
                    by_symbol[symbol][name] = ann

        for symbol, names in by_symbol.items():
            stats["matched"] += 1
            for name, announced_at in names.items():
                try:
                    async with limiter:
                        xr = await client.get(_xml_url(name))
                    if xr.status_code == 404:
                        stats["xml_missing"] += 1
                        continue
                    xr.raise_for_status()
                    parsed = parse_xbrl(xr.content, taxonomy=classify_bse_taxonomy(name),
                                        force_absolute_inr=True)
                except Exception as e:
                    stats["parse_failed"] += 1
                    logger.warning(f"[BSE-FIN] {symbol}: daily scan fetch/parse failed "
                                   f"for {name}: {e}")
                    continue
                outcome = await _validated_upsert(SessionFactory, symbol, parsed, name,
                                                  announced_at=announced_at)
                stats[outcome if outcome in stats else "parse_failed"] += 1

    async with SessionFactory() as session:
        await complete_sync_log(
            session, log_id, "success",
            records_processed=stats["upserted"],
            records_failed=stats["parse_failed"],
            metadata=stats,
        )
        await session.commit()
    logger.info(f"[BSE-FIN] daily market scan DONE: {stats}")
    return stats


async def run_universe(since_days: int = 10) -> Dict[str, int]:
    from bharat_terminal.kb.sync.bse_sync import bse_universe
    SessionFactory = create_async_session()
    async with SessionFactory() as session:
        pairs = await bse_universe(session)
    if not pairs:
        logger.info("[BSE-FIN] no BSE-only companies onboarded; nothing to do")
        return {"symbols": 0, "upserted": 0, "xml_missing": 0, "parse_failed": 0, "failed": 0}
    return await run(pairs, since_days=since_days)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    asyncio.run(run_universe())
