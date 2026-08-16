"""Historical concall-transcript backfill from the BSE announcement archive.

WHY (E-series Stage 1, Option B — founder decision 2026-08-15): the live
transcript corpus starts 2026-05-10, so guidance outcomes can't be labeled
against FY25/FY26 results. SEBI has mandated transcript disclosure since
2021; the archive holds them under Company Update → "Analyst / Investor
Meet" (there is NO dedicated Transcript subcategory — headline filtering
selects them; verified 2026-08-15, see STAGE1_GUIDANCE_OUTCOMES_2026-08-15.md).

DESIGN: this module ONLY enumerates the archive and (on --execute) inserts
`news_items` rows shaped exactly like the live BSE adapter's — source
'BSE_FILINGS', headline containing "transcript", AttachHis PDF url,
symbols_mentioned=[symbol]. The EXISTING chain (transcript_sync →
transcript_structure → guidance extraction → guidance_ledger_sync) then
processes them unchanged; one ingestion path, one set of rules. Direct
table inserts do not touch Kafka, so the analysis pipeline stays asleep.

Idempotency: id = BSE_ARCH_<attachment-uuid> (deterministic), ON CONFLICT
DO NOTHING, plus a URL-existence check against live-ingested rows.

Politeness: the archive endpoint is flaky on long windows (empty bodies +
read timeouts — same behavior the results-index endpoint shows); every
page fetch retries with backoff, and requests are spaced.

Run (dry-run is the default and writes NOTHING):
  python -m bharat_terminal.kb.sync.bse_transcript_backfill --dry-run \
      --manifest /out/bse_transcripts.jsonl
  python -m bharat_terminal.kb.sync.bse_transcript_backfill --execute  # gated
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import text

logger = logging.getLogger(__name__)

API = ("https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
       "?pageno={page}&strCat={cat}&strPrevDate={frm}"
       "&strScrip=&strSearch=P&strToDate={to}&strType=C"
       "&subcategory={subcat}")

# (category, subcategory, dedicated): "Earnings Call Transcript" is the
# dedicated filing option (discovered 2026-08-15 via an Atam Valves apology
# headline; 719 rows in May-2025 alone) — every row there is transcript
# INTENT, so rows whose headline lacks the literal word get a "[Transcript]"
# prefix to satisfy transcript_sync's queue contract, while audio/video
# recording announcements are excluded. "Analyst / Investor Meet" catches
# the companies that misfile (~1.2k over the window, ~15% of the total).
PAIRS = [
    ("Company Update", "Earnings Call Transcript", True),
    ("Company Update", "Analyst / Investor Meet", False),
]
PDF_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}

# Live transcript ingestion begins 2026-05-10; the backfill stops just
# before it so the two corpora meet without overlap.
BACKFILL_FROM = date.fromisoformat(os.getenv("BSE_BACKFILL_FROM", "2024-04-01"))
BACKFILL_TO = date.fromisoformat(os.getenv("BSE_BACKFILL_TO", "2026-05-09"))

REQUEST_DELAY_S = float(os.getenv("BSE_BACKFILL_DELAY_S", "0.7"))
FETCH_TIMEOUT_S = float(os.getenv("BSE_BACKFILL_TIMEOUT_S", "25"))
RETRIES = 4

# Selection: a transcript announcement, with an attached PDF. NEWSSUB often
# repeats the headline; matching either side is deliberate.
_TRANSCRIPT_RE = re.compile(r"transcript", re.I)
_AV_ONLY_RE = re.compile(r"audio|video|recording|web[- ]?link|webcast", re.I)
IST_OFFSET = timedelta(hours=5, minutes=30)

INSERT_SQL = text("""
    INSERT INTO news_items
        (id, source, timestamp_utc, headline, body, url,
         ingest_latency_ms, category, symbols_mentioned, created_at)
    VALUES (:id, 'BSE_FILINGS', :ts, :headline, :body, :url,
            0.0, 'COMPANY_UPDATE', :symbols, now())
    ON CONFLICT (id) DO NOTHING
""")
URL_EXISTS_SQL = text("SELECT 1 FROM news_items WHERE url = :url LIMIT 1")
SCRIP_MAP_SQL = text("""
    SELECT symbol, bse_scrip_code FROM companies
    WHERE bse_scrip_code IS NOT NULL
""")


def _month_windows(frm: date, to: date):
    """(start, end) date pairs, one per calendar month, clipped to [frm, to]."""
    cur = frm.replace(day=1)
    while cur <= to:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield max(cur, frm), min(nxt - timedelta(days=1), to)
        cur = nxt


async def _fetch_page(client: httpx.AsyncClient, cat: str, subcat: str,
                      frm: date, to: date, page: int) -> list[dict]:
    """One archive page, with retries — the endpoint intermittently returns
    empty bodies/timeouts on long windows and is fine on retry."""
    from urllib.parse import quote
    url = API.format(page=page, cat=quote(cat), subcat=quote(subcat),
                     frm=frm.strftime("%Y%m%d"), to=to.strftime("%Y%m%d"))
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return data.get("Table") or []
        except Exception as e:                    # flaky endpoint: retry all
            last_err = e
            await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"page fetch failed after {RETRIES} tries "
                       f"({frm}..{to} p{page}): {last_err}")


def _to_utc(news_dt: str) -> datetime | None:
    """NEWS_DT is naive-IST ISO ('2025-05-14T18:30:45' / with millis)."""
    try:
        return (datetime.fromisoformat(news_dt.split(".")[0])
                - IST_OFFSET).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _candidate(row: dict, dedicated: bool) -> dict | None:
    """Archive row → backfill candidate, or None if not a transcript PDF."""
    attach = (row.get("ATTACHMENTNAME") or "").strip()
    headline = (row.get("HEADLINE") or "").strip()
    newssub = (row.get("NEWSSUB") or "").strip()
    if not attach.lower().endswith(".pdf"):
        return None
    both = f"{headline} {newssub}"
    has_word = bool(_TRANSCRIPT_RE.search(both))
    if not has_word:
        # In the DEDICATED subcategory the filing intent is transcript even
        # when the headline doesn't say so — but audio/video/webcast-link
        # announcements also land there and must not enter the PDF queue.
        if not dedicated or _AV_ONLY_RE.search(both):
            return None
    ts = _to_utc(str(row.get("NEWS_DT") or ""))
    if ts is None:
        return None
    # transcript_sync's queue requires "transcript" in the HEADLINE
    # specifically — promote from NEWSSUB, or tag dedicated-subcategory rows.
    if not _TRANSCRIPT_RE.search(headline):
        headline = (newssub if _TRANSCRIPT_RE.search(newssub)
                    else f"[Transcript] {headline or newssub}")
    return {
        "id": f"BSE_ARCH_{attach[:-4]}",
        "scrip_cd": str(row.get("SCRIP_CD") or "").split(".")[0],
        "company": (row.get("SLONGNAME") or "").strip(),
        "headline": headline[:500],
        "body": newssub[:1000] or None,
        "url": PDF_BASE + attach,
        "ts": ts,
    }


async def enumerate_archive(only_subcat: str | None = None) -> list[dict]:
    """Walk every (pair × month window), collect candidates, dedup by id."""
    seen: dict[str, dict] = {}
    pairs = [p for p in PAIRS
             if only_subcat is None or p[1] == only_subcat]
    async with httpx.AsyncClient(headers=HEADERS,
                                 timeout=FETCH_TIMEOUT_S) as client:
        for cat, subcat, dedicated in pairs:
            for frm, to in _month_windows(BACKFILL_FROM, BACKFILL_TO):
                month_hits = 0
                page = 1
                while True:
                    rows = await _fetch_page(client, cat, subcat, frm, to,
                                             page)
                    if not rows:
                        break
                    for row in rows:
                        cand = _candidate(row, dedicated)
                        if cand and cand["id"] not in seen:
                            seen[cand["id"]] = cand
                            month_hits += 1
                    page += 1
                    await asyncio.sleep(REQUEST_DELAY_S)
                logger.info(f"[WALK] {subcat} {frm:%Y-%m}: {month_hits} "
                            f"transcripts ({page - 1} pages)")
    return list(seen.values())


def _load_manifests(paths: str) -> list[dict]:
    """Rehydrate candidates from prior dry-run manifests (skips the ~35-min
    archive re-walk on --execute). Dedup by id across files."""
    seen: dict[str, dict] = {}
    for path in paths.split(","):
        with open(path.strip()) as f:
            for line in f:
                c = json.loads(line)
                c["ts"] = datetime.fromisoformat(c["ts"])
                seen.setdefault(c["id"], c)
    return list(seen.values())


async def run(execute: bool, manifest_path: str | None,
              only_subcat: str | None = None,
              from_manifest: str | None = None) -> int:
    from bharat_terminal.kb.sync.utils import create_async_session
    session_factory = create_async_session()

    async with session_factory() as session:
        scrip_map = {
            str(r["bse_scrip_code"]).split(".")[0]: r["symbol"]
            for r in (await session.execute(SCRIP_MAP_SQL)).mappings()
        }
    logger.info(f"scrip map: {len(scrip_map)} companies with bse_scrip_code")

    cands = (_load_manifests(from_manifest) if from_manifest
             else await enumerate_archive(only_subcat))
    for c in cands:
        c["symbol"] = scrip_map.get(c["scrip_cd"])
    mapped = [c for c in cands if c["symbol"]]
    unmapped = [c for c in cands if not c["symbol"]]
    by_month: dict[str, int] = {}
    for c in cands:
        by_month[c["ts"].strftime("%Y-%m")] = (
            by_month.get(c["ts"].strftime("%Y-%m"), 0) + 1)

    logger.info(f"[ENUM] candidates: {len(cands)} | mapped to symbols: "
                f"{len(mapped)} ({len({c['symbol'] for c in mapped})} "
                f"companies) | unmapped scrips: {len(unmapped)}")
    logger.info(f"[ENUM] by month: {json.dumps(by_month, sort_keys=True)}")
    # Extraction-cost estimate for the approval decision: structure +
    # guidance passes together average ~$0.06-0.12/transcript on
    # Haiku/Flash-class at observed transcript sizes.
    logger.info(f"[EST] LLM extraction spend for {len(mapped)} transcripts: "
                f"${0.06 * len(mapped):.0f}-{0.12 * len(mapped):.0f}")

    if manifest_path:
        with open(manifest_path, "w") as f:
            for c in sorted(cands, key=lambda x: x["ts"]):
                f.write(json.dumps({**c, "ts": c["ts"].isoformat()}) + "\n")
        logger.info(f"[ENUM] manifest written: {manifest_path}")

    if not execute:
        logger.info("DRY-RUN: nothing written to the database")
        return 0

    inserted = skipped_url = 0
    async with session_factory() as session:
        for c in mapped:
            if (await session.execute(
                    URL_EXISTS_SQL, {"url": c["url"]})).first():
                skipped_url += 1
                continue
            res = await session.execute(INSERT_SQL, {
                "id": c["id"], "ts": c["ts"], "headline": c["headline"],
                "body": c["body"], "url": c["url"],
                "symbols": json.dumps([c["symbol"]]),
            })
            inserted += res.rowcount or 0
        await session.commit()
    logger.info(f"EXECUTE: inserted {inserted} news_items rows "
                f"(url-dup skipped: {skipped_url}, "
                f"unmapped excluded: {len(unmapped)}). transcript_sync will "
                f"pick them up as its normal pending queue.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="enumerate + report + manifest, write nothing "
                           "(default)")
    mode.add_argument("--execute", action="store_true",
                      help="insert news_items rows for mapped candidates")
    parser.add_argument("--manifest", default=None,
                        help="write the candidate list as JSONL here")
    parser.add_argument("--subcategory", default=None,
                        help="walk only this subcategory (default: all PAIRS)")
    parser.add_argument("--from-manifest", default=None,
                        help="comma-separated dry-run manifest JSONLs — "
                             "insert from these instead of re-walking")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(run(execute=args.execute, manifest_path=args.manifest,
                           only_subcat=args.subcategory,
                           from_manifest=args.from_manifest))


if __name__ == "__main__":
    sys.exit(main())
