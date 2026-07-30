"""Market-state feed sync — ASM/GSM/ESM stages + price bands → market_state.

WHY (ANALYST_CLONE_ARCHITECTURE_2026-07-29.md §3): surveillance stages gate
everything downstream. GSM-III trades once a week and GSM-IV prohibits upward
moves — a filing on such a name CANNOT show a normal reaction by exchange
design, so reaction labels on those symbols are corrupt and paper fills are
fiction. This table (migration 033) is the label-validity and execution gate.

Feeds (all verified HTTP-200 from this VM 2026-07-29; payload shapes below
are from live inspection, parsed defensively because NSE renames fields):
  reportASM  : {longterm:{data:[...]}, shortterm:{data:[...]}} —
               survCode 'LTASM - I (13)' / 'STASM - II (12)',
               asmSurvIndicator 'Stage I'.
  reportGSM  : top-level LIST. GOTCHA: the 'gsmStage' field is NOT the GSM
               stage — it is the surveillance-series number in roman
               ('LVIII' = 58, matching survCode '(58)'). The real stage
               lives in survCode/survDesc, often in combined strings
               ('LTASM I & GSM 0 (50)', 'ASM IBC Stage I and GSM Stage 0');
               live payload also carries stage VI, beyond the FAQ's 0–IV.
  reportESM  : top-level LIST. survCode 'ESM - II (35)',
               esmSurvIndicator 'Stage II'.
  sec_list.csv (nsearchives): Symbol,Series,Security Name,Band —
               Band in 2/5/10/20/'No Band'; 'No Band' = derivatives-linked.
               EQ/BE series only.

Behavior: fetch all four FAIL-SOFT — one feed down must not kill the run;
that feed's column stays NULL (and, on re-run upsert, a failed feed never
clobbers a value written earlier the same day). One row per symbol seen in
ANY feed for as_of = today IST; absence from every list = clean = no row.
The homepage cookie warmup itself may 403 (observed 2026-07-29) yet still
set cookies that unlock the API — warmup status is deliberately ignored.

Dry-run never touches the DB: all sqlalchemy/DB imports are lazy inside the
write path, so `--dry-run` runs anywhere httpx exists.

CLI:
  python -m bharat_terminal.kb.sync.market_state_sync --dry-run   (default)
  python -m bharat_terminal.kb.sync.market_state_sync --execute
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import re
from collections import Counter
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

NSE_HOME = "https://www.nseindia.com/"
ASM_URL = "https://www.nseindia.com/api/reportASM"
GSM_URL = "https://www.nseindia.com/api/reportGSM"
ESM_URL = "https://www.nseindia.com/api/reportESM"
SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"

# Same browser fingerprint as ingestion/adapters/nse_filings.py — NSE 403s
# bare clients.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

FEED_TIMEOUT = 30.0

# Stages NSE actually emits (VI observed live in GSM despite the FAQ's 0–IV).
_VALID_STAGES = {"0", "I", "II", "III", "IV", "V", "VI"}

# (?<![A-Za-z]) everywhere: 'GSM' must not match inside 'LTASM'/'STASM',
# and 'Stage I' inside 'LTASM Stage I and GSM Stage 0' must not be taken
# for the GSM stage.
_RE_ASM_CODE = re.compile(r"(?:LT|ST)ASM\s*-?\s*([IVX]+)")
_RE_STAGE_IND = re.compile(r"Stage\s+([IVX]+)", re.IGNORECASE)
_RE_GSM = re.compile(r"(?<![A-Za-z])GSM\s*(?:-|stage)?\s*(0|[IVX]+)", re.IGNORECASE)
_RE_ESM = re.compile(r"(?<![A-Za-z])ESM\s*-?\s*([IVX]+)")

_BAND_MAP = {"2": "2", "5": "5", "10": "10", "20": "20", "NO BAND": "NoBand"}
_SEC_LIST_SERIES = {"EQ", "BE"}


def _stage(m: Optional[re.Match]) -> Optional[str]:
    if not m:
        return None
    s = m.group(1).upper()
    return s if s in _VALID_STAGES else None


def _sym(row: Dict[str, Any]) -> Optional[str]:
    s = str(row.get("symbol") or row.get("Symbol") or "").strip().upper()
    return s or None


# ---------------------------------------------------------------- parsers
# All parsers are pure (payload -> {symbol: fragment}) so dry-run and tests
# exercise the exact production path. Fragments carry the raw source rows;
# build_rows() merges them into market_state rows.

def parse_asm(payload: Any) -> Dict[str, Dict[str, Any]]:
    """{symbol: {'asm_stage': 'LT-I'..'LT-IV'|'ST-I'|'ST-II', 'raw': [...]}}.

    A symbol in both LT and ST lists (never observed, but nothing forbids
    it) gets both, '+'-joined, LT first — VARCHAR(16) holds the worst case.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(payload, dict):
        logger.warning("[MKTSTATE] ASM payload not a dict; skipping")
        return out
    for key, prefix in (("longterm", "LT"), ("shortterm", "ST")):
        block = payload.get(key) or {}
        rows = block.get("data") if isinstance(block, dict) else None
        if rows is None:
            logger.warning(f"[MKTSTATE] ASM '{key}' block missing/odd shape")
            continue
        for row in rows:
            symbol = _sym(row)
            if not symbol:
                continue
            stage = _stage(_RE_ASM_CODE.search(str(row.get("survCode") or "")))
            if stage is None:
                stage = _stage(_RE_STAGE_IND.search(str(row.get("asmSurvIndicator") or "")))
            if stage is None:
                logger.warning(f"[MKTSTATE] ASM {symbol}: no stage in {row.get('survCode')!r}")
                continue
            frag = out.setdefault(symbol, {"asm_stage": None, "raw": []})
            piece = f"{prefix}-{stage}"
            frag["asm_stage"] = f"{frag['asm_stage']}+{piece}" if frag["asm_stage"] else piece
            frag["raw"].append({"list": key, **row})
    return out


def parse_gsm(payload: Any) -> Dict[str, Dict[str, Any]]:
    """{symbol: {'gsm_stage': '0'|'I'..'VI'|None, 'raw': {...}}}.

    Stage comes from survCode, falling back to survDesc — NEVER from the
    'gsmStage' field (see module docstring). Rows with no parseable stage
    are kept with gsm_stage=None: the symbol IS under surveillance and the
    raw row lands in source_payload, which beats silently dropping it.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(payload, dict):  # tolerate a future {data:[...]} wrapping
        payload = payload.get("data", [])
    if not isinstance(payload, list):
        logger.warning("[MKTSTATE] GSM payload not a list; skipping")
        return out
    for row in payload:
        symbol = _sym(row)
        if not symbol:
            continue
        stage = _stage(_RE_GSM.search(str(row.get("survCode") or "")))
        if stage is None:
            stage = _stage(_RE_GSM.search(str(row.get("survDesc") or "")))
        if stage is None:
            logger.warning(
                f"[MKTSTATE] GSM {symbol}: no stage in "
                f"{row.get('survCode')!r} / {row.get('survDesc')!r}"
            )
        out[symbol] = {"gsm_stage": stage, "raw": row}
    return out


def parse_esm(payload: Any) -> Dict[str, Dict[str, Any]]:
    """{symbol: {'esm_stage': 'I'|'II', 'raw': {...}}}."""
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    if not isinstance(payload, list):
        logger.warning("[MKTSTATE] ESM payload not a list; skipping")
        return out
    for row in payload:
        symbol = _sym(row)
        if not symbol:
            continue
        stage = _stage(_RE_STAGE_IND.search(str(row.get("esmSurvIndicator") or "")))
        if stage is None:
            stage = _stage(_RE_ESM.search(str(row.get("survCode") or "")))
        if stage is None:
            logger.warning(f"[MKTSTATE] ESM {symbol}: no stage in {row.get('survCode')!r}")
        out[symbol] = {"esm_stage": stage, "raw": row}
    return out


def parse_sec_list(text: str) -> Dict[str, Dict[str, Any]]:
    """{symbol: {'price_band': '2'|'5'|'10'|'20'|'NoBand', 'raw': {...}}}.

    EQ/BE series only — SM/ST (SME), IV (InvIT) etc. are outside the
    universe. 'NoBand' means derivatives-linked, the authoritative F&O flag.
    """
    out: Dict[str, Dict[str, Any]] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        symbol = (row.get("Symbol") or "").upper()
        series = row.get("Series") or ""
        if not symbol or series not in _SEC_LIST_SERIES:
            continue
        raw_band = row.get("Band") or ""
        band = _BAND_MAP.get(raw_band.upper())
        if band is None:
            logger.warning(f"[MKTSTATE] sec_list {symbol}: unknown band {raw_band!r}")
            band = raw_band[:8] or None  # store what NSE said; VARCHAR(8)
        out[symbol] = {"price_band": band, "raw": row}
    return out


# ---------------------------------------------------------------- fetch

async def fetch_feeds() -> Tuple[Dict[str, Any], Dict[str, bool]]:
    """Fetch all four feeds fail-soft.

    Returns (parsed-fragments-per-feed, ok-flags-per-feed). A failed feed
    yields {} and ok=False — its column stays NULL for symbols this run
    and is excluded from the upsert SET so a re-run cannot clobber values
    a healthier earlier run wrote today.
    """
    parsed: Dict[str, Any] = {"asm": {}, "gsm": {}, "esm": {}, "band": {}}
    ok: Dict[str, bool] = {"asm": False, "gsm": False, "esm": False, "band": False}

    async with httpx.AsyncClient(timeout=FEED_TIMEOUT, headers=HEADERS,
                                 follow_redirects=True) as client:
        try:
            warm = await client.get(NSE_HOME)
            logger.info(f"[MKTSTATE] cookie warmup: HTTP {warm.status_code}")
        except Exception as e:  # noqa: BLE001 — warmup failure is not fatal
            logger.warning(f"[MKTSTATE] cookie warmup failed: {e}")

        for name, url, parser, is_json in (
            ("asm", ASM_URL, parse_asm, True),
            ("gsm", GSM_URL, parse_gsm, True),
            ("esm", ESM_URL, parse_esm, True),
            ("band", SEC_LIST_URL, parse_sec_list, False),
        ):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                if is_json:
                    body = resp.text
                    if not body.strip().startswith(("{", "[")):
                        raise ValueError(f"non-JSON response: {body[:80]!r}")
                    parsed[name] = parser(json.loads(body))
                else:
                    parsed[name] = parser(resp.text)
                ok[name] = True
                logger.info(f"[MKTSTATE] {name}: {len(parsed[name])} symbols")
            except Exception as e:  # noqa: BLE001 — fail-soft per feed
                logger.warning(f"[MKTSTATE] {name} feed FAILED ({url}): {e}")
    return parsed, ok


# ---------------------------------------------------------------- rows

def build_rows(parsed: Dict[str, Any], as_of: date) -> List[Dict[str, Any]]:
    """One row per symbol seen in ANY feed; absence from all = clean = no row."""
    symbols = (set(parsed["asm"]) | set(parsed["gsm"])
               | set(parsed["esm"]) | set(parsed["band"]))
    rows = []
    for symbol in sorted(symbols):
        asm = parsed["asm"].get(symbol)
        gsm = parsed["gsm"].get(symbol)
        esm = parsed["esm"].get(symbol)
        band = parsed["band"].get(symbol)
        payload = {}
        if asm:
            payload["asm"] = asm["raw"]
        if gsm:
            payload["gsm"] = gsm["raw"]
        if esm:
            payload["esm"] = esm["raw"]
        if band:
            payload["sec_list"] = band["raw"]
        rows.append({
            "symbol": symbol,
            "as_of": as_of,
            "asm_stage": asm["asm_stage"] if asm else None,
            "gsm_stage": gsm["gsm_stage"] if gsm else None,
            "esm_stage": esm["esm_stage"] if esm else None,
            "price_band": band["price_band"] if band else None,
            "source_payload": payload,
        })
    return rows


def print_summary(rows: List[Dict[str, Any]], ok: Dict[str, bool]) -> None:
    print(f"feeds ok: {ok}")
    print(f"total rows (union of symbols): {len(rows)}")
    for col in ("asm_stage", "gsm_stage", "esm_stage", "price_band"):
        counts = Counter(r[col] for r in rows if r[col] is not None)
        nulls = sum(1 for r in rows if r[col] is None)
        print(f"  {col}: {dict(sorted(counts.items()))}  (NULL: {nulls})")
    # Samples: surface multi-feed symbols first — they exercise the merge.
    sample = sorted(
        rows,
        key=lambda r: (-sum(r[c] is not None for c in
                            ("asm_stage", "gsm_stage", "esm_stage", "price_band")),
                       r["symbol"]),
    )[:10]
    print("sample rows (multi-feed first):")
    for r in sample:
        print(f"  {r['symbol']:<14} asm={r['asm_stage']!s:<12} gsm={r['gsm_stage']!s:<5} "
              f"esm={r['esm_stage']!s:<5} band={r['price_band']!s:<7} "
              f"payload_keys={sorted(r['source_payload'])}")


# ---------------------------------------------------------------- write
# DB code lives behind lazy imports: --dry-run must run in any environment
# with httpx alone, and must never open a connection.

_COL_FOR_FEED = {"asm": "asm_stage", "gsm": "gsm_stage",
                 "esm": "esm_stage", "band": "price_band"}


async def upsert_rows(rows: List[Dict[str, Any]], ok: Dict[str, bool]) -> int:
    from sqlalchemy import text  # lazy — see module docstring

    from bharat_terminal.kb.sync.utils import (
        complete_sync_log, create_async_session, start_sync_log,
    )

    # Only columns whose feed succeeded THIS run may be overwritten;
    # source_payload merges so a partial re-run keeps earlier fragments.
    set_cols = [_COL_FOR_FEED[f] for f in ("asm", "gsm", "esm", "band") if ok[f]]
    set_clause = ", ".join(
        [f"{c} = EXCLUDED.{c}" for c in set_cols]
        + ["source_payload = COALESCE(market_state.source_payload, '{}'::jsonb) "
           "|| EXCLUDED.source_payload"]
    )
    stmt = text(f"""
        INSERT INTO market_state
            (symbol, as_of, asm_stage, gsm_stage, esm_stage, price_band,
             source_payload)
        VALUES (:symbol, :as_of, :asm_stage, :gsm_stage, :esm_stage,
                :price_band, CAST(:source_payload AS JSONB))
        ON CONFLICT (symbol, as_of) DO UPDATE SET {set_clause}
    """)
    params = [{**r, "source_payload": json.dumps(r["source_payload"])} for r in rows]

    SessionFactory = create_async_session()
    # Log row is committed BEFORE the upsert (sibling pattern): if it shared
    # the upsert's transaction, a failure would roll the log row away and the
    # 'failed' status below would target a nonexistent id.
    async with SessionFactory() as s:
        log_id = await start_sync_log(s, "market_state_sync", "NSE_SURVEILLANCE")
        await s.commit()
    try:
        async with SessionFactory() as s:
            await s.execute(stmt, params)
            await s.commit()
    except Exception as e:
        async with SessionFactory() as s:
            await complete_sync_log(s, log_id, "failed", error_message=str(e)[:500])
            await s.commit()
        raise
    async with SessionFactory() as s:
        await complete_sync_log(s, log_id, "completed", records_processed=len(rows),
                                metadata={"feeds_ok": ok})
        await s.commit()
    return len(rows)


# ---------------------------------------------------------------- entry

async def sync(dry_run: bool = True) -> Dict[str, Any]:
    parsed, ok = await fetch_feeds()
    if not any(ok.values()):
        logger.error("[MKTSTATE] ALL four feeds failed — nothing to write")
        return {"feeds_ok": ok, "rows": 0, "written": 0}

    as_of = datetime.now(IST).date()
    rows = build_rows(parsed, as_of)
    print_summary(rows, ok)

    written = 0
    if dry_run:
        logger.info(f"[MKTSTATE] DRY-RUN: {len(rows)} rows for {as_of}, no DB writes")
    else:
        written = await upsert_rows(rows, ok)
        logger.info(f"[MKTSTATE] EXECUTE: upserted {written} rows for {as_of}")
    return {"feeds_ok": ok, "rows": len(rows), "written": written}


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    ap = argparse.ArgumentParser(
        prog="market_state_sync",
        description="ASM/GSM/ESM surveillance stages + price bands -> market_state",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="fetch + parse + print summary; no DB (default)")
    mode.add_argument("--execute", action="store_true",
                      help="fetch + parse + UPSERT into market_state")
    args = ap.parse_args()
    asyncio.run(sync(dry_run=not args.execute))


if __name__ == "__main__":
    main()
