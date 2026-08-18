"""Concall transcript sync — fetch COMPLETE transcript PDFs into the KB (A1).

Source queue: `news_items` rows whose headline mentions "transcript" from the
exchange-filings adapters (the ingestion pipeline already sees every such
announcement; the URL column carries the nsearchives/bseindia PDF directly).
Each pending announcement is downloaded, text-extracted WITHOUT ANY CLIPPING,
and upserted into `concall_transcripts` keyed by the announcement id.

No-clipping rule (founder, 2026-07-19): unlike ingestion/pdf_fetch.py (which
caps pages/chars for the hot analysis path), this module has NO page cap and
NO char cap — the stored transcript is the entire document. Only a byte-size
guard against pathological files remains, and hitting it is recorded as a
failure, never as a silent truncation.

Failures are stored with parse_status so they don't refetch forever;
re-attempt explicitly with --retry-failed.

Run:
  python -m bharat_terminal.kb.sync.transcript_sync            # sync pending
  python -m bharat_terminal.kb.sync.transcript_sync --limit 20 --dry-run
  python -m bharat_terminal.kb.sync.transcript_sync --retry-failed
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
from typing import Optional

import httpx
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Headers + text cleaning mirror ingestion/pdf_fetch.py, inlined because the
# kb image deliberately does not depend on the ingestion package (aiohttp).
_PDF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
}
_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


_SURROGATES_RE = re.compile("[\ud800-\udfff]")


def _clean(text_: str) -> str:
    """Whitespace normalisation only — never content removal (see
    ingestion/pdf_fetch._clean for the ligature rationale). Exception:
    lone UTF-16 surrogates are stripped — pypdf decodes some PDF glyphs
    (icon fonts, private-use codepoints) into unpaired surrogates, which
    are not valid UTF-8 and make Postgres reject the ENTIRE row (the
    2026-08-18 TBOTEK wedge: one such doc froze the whole backfill queue)."""
    text_ = text_.replace("\x00", "").replace("�", "")
    text_ = _SURROGATES_RE.sub("", text_)
    text_ = re.sub(r"-\n(?=[a-z])", "", text_)  # "compa-\nny" -> "company"
    text_ = _WS_RE.sub(" ", text_)
    text_ = _MULTI_NL_RE.sub("\n\n", text_)
    return text_.strip()

# Byte guard only — NOT a content cap. A concall transcript PDF is ~0.2-2 MB;
# anything past this is not a transcript (or is corrupt) and is recorded as a
# failure rather than silently truncated.
TRANSCRIPT_PDF_MAX_BYTES = int(
    os.getenv("TRANSCRIPT_PDF_MAX_BYTES", str(50 * 1024 * 1024))
)
FETCH_TIMEOUT_S = float(os.getenv("TRANSCRIPT_FETCH_TIMEOUT_S", "60"))
POLITENESS_DELAY_S = float(os.getenv("TRANSCRIPT_FETCH_DELAY_S", "0.6"))
# Scanned-PDF detector: pypdf extracts near-nothing from image pages. The
# 2026-07-19 audit found a 30-page scanned call at ~293 chars/page stored as
# "ok"; real transcripts run 2-3K chars/page. Rows below the floor get
# parse_status='needs_ocr' and never reach customers or the LLM.
MIN_CHARS_PER_PAGE = int(os.getenv("TRANSCRIPT_MIN_CHARS_PER_PAGE", "500"))

_PENDING_SQL = """
    SELECT ni.id, ni.source, ni.headline, ni.url, ni.timestamp_utc,
           ni.symbols_mentioned
    FROM news_items ni
    WHERE ni.headline ILIKE '%transcript%'
      AND ni.source IN ('NSE_FILINGS', 'BSE_FILINGS')
      AND ni.url ILIKE '%.pdf%'
      AND NOT EXISTS (
          SELECT 1 FROM concall_transcripts ct
          WHERE ct.news_id = ni.id {retry_clause}
      )
    ORDER BY ni.timestamp_utc ASC
    {limit_clause}
"""


def extract_full_text(data: bytes) -> tuple[Optional[str], int, list[dict]]:
    """Extract the COMPLETE text of a PDF — every page, no char cap.
    Returns (text | None, page_count, page_offsets) where page_offsets maps
    each page to its (start, end) character span in the joined text. Pages
    are cleaned individually so the offsets stay valid.
    Runs CPU-bound; call via to_thread."""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")  # exchange PDFs are often empty-password encrypted
        except Exception:
            return None, 0, []
    parts: list[str] = []
    for page in reader.pages:  # deliberately uncapped
        try:
            parts.append(_clean(page.extract_text() or ""))
        except Exception:
            parts.append("")  # keep page alignment; never drop content silently
    offsets: list[dict] = []
    pos = 0
    joined: list[str] = []
    for n, part in enumerate(parts, start=1):
        if joined:
            joined.append("\n\n")
            pos += 2
        offsets.append({"n": n, "start": pos, "end": pos + len(part)})
        joined.append(part)
        pos += len(part)
    # No trailing .strip(): it would shift every stored page offset. Pages are
    # individually stripped by _clean already.
    text_ = "".join(joined)
    return (text_ or None), len(reader.pages), offsets


# ── Discussed-quarter derivation ───────────────────────────────────────────
# The quarter a call DISCUSSES ≠ the filing date's quarter (Q4 calls straggle
# into July). ~90% of transcripts name it on the title page — extract
# deterministically, fall back to the earnings-season date rule.

_PERIOD_SCAN_CHARS = 3000
_Q_FYRANGE_RE = re.compile(  # "Q4 – FY 2025-26" / "Q4 FY 25-26" → trailing year wins
    r"\bQ\s*([1-4])\s*[–\-&,]?\s*(?:and\s+)?FY\s*[-'’ ]?\s*(?:20)?(\d{2})\s*[-–]\s*(?:20)?(\d{2})", re.I)
_Q_FY_RE = re.compile(
    r"\b(?:Q|Quarter)\s*([1-4])\s*[–\-&,]?\s*(?:and\s+)?FY\s*[-'’ ]?\s*(?:20)?(\d{2})\b", re.I)
_ENDED_RE = re.compile(
    r"(?:quarter|period|3 months?)[^.\n]{0,60}?ended[^.\n]{0,20}?"
    r"(March|June|September|December)[^.\n]{0,15}?(\d{4})", re.I)
_ENDED_Q = {"march": (4, 0), "june": (1, 1), "september": (2, 1), "december": (3, 1)}


def derive_period(full_text: Optional[str], announced_at) -> Optional[str]:
    """Label like 'Q1FY27' for the quarter the call discusses. Pure function."""
    head = (full_text or "")[:_PERIOD_SCAN_CHARS]
    # (pos, priority, quarter, fy2digit): the FY-range pattern outranks the
    # simple one at the same position — "Q4 – FY 2025-26" must yield 26, but
    # the simple pattern also matches there capturing "25".
    candidates: list[tuple[int, int, int, int]] = []
    for m in _Q_FYRANGE_RE.finditer(head):
        candidates.append((m.start(), 0, int(m.group(1)), int(m.group(3))))
    for m in _Q_FY_RE.finditer(head):
        candidates.append((m.start(), 1, int(m.group(1)), int(m.group(2))))
    for m in _ENDED_RE.finditer(head):
        q, fy_bump = _ENDED_Q[m.group(1).lower()]
        candidates.append((m.start(), 1, q, (int(m.group(2)) % 100) + fy_bump))
    for _, _, q, fy in sorted(candidates):
        if 20 <= fy <= 35:  # sanity window rejects garbled years
            return f"Q{q}FY{fy}"
    if announced_at is None:
        return None
    mth, yr = announced_at.month, announced_at.year % 100
    if 4 <= mth <= 6:
        return f"Q4FY{yr}"
    if 7 <= mth <= 9:
        return f"Q1FY{yr + 1}"
    if 10 <= mth <= 12:
        return f"Q2FY{yr + 1}"
    return f"Q3FY{yr}"


def _first_symbol(symbols_mentioned) -> Optional[str]:
    v = symbols_mentioned
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except json.JSONDecodeError:
            return None
    if isinstance(v, (list, tuple)) and v:
        s = str(v[0]).strip().upper()
        return s or None
    return None


def _referer_for(url: str) -> str:
    return (
        "https://www.bseindia.com/"
        if "bseindia" in (url or "")
        else "https://www.nseindia.com/"
    )


async def _fetch_pdf(client: httpx.AsyncClient, url: str) -> tuple[Optional[bytes], str]:
    """Download the PDF. Returns (bytes | None, failure_status)."""
    try:
        headers = {**_PDF_HEADERS, "Referer": _referer_for(url)}
        resp = await client.get(url, headers=headers,
                                timeout=FETCH_TIMEOUT_S, follow_redirects=True)
        if resp.status_code != 200:
            logger.warning(f"[transcripts] HTTP {resp.status_code} for {url}")
            return None, "fetch_failed"
        data = resp.content
        if len(data) > TRANSCRIPT_PDF_MAX_BYTES:
            logger.warning(f"[transcripts] {len(data)} bytes exceeds guard, {url}")
            return None, "fetch_failed"
        if not data[:5].startswith(b"%PDF"):
            return None, "not_pdf"
        return data, ""
    except Exception as e:
        logger.warning(f"[transcripts] fetch error for {url}: {e}")
        return None, "fetch_failed"


async def sync_transcripts(
    limit: Optional[int] = None,
    dry_run: bool = False,
    retry_failed: bool = False,
    structure: bool = True,
) -> dict:
    from bharat_terminal.kb.sync.utils import create_async_session

    stats = {"pending": 0, "ok": 0, "fetch_failed": 0, "extract_failed": 0,
             "not_pdf": 0, "no_symbol": 0, "total_chars": 0}
    sql = _PENDING_SQL.format(
        retry_clause="AND ct.parse_status = 'ok'" if retry_failed else "",
        limit_clause=f"LIMIT {int(limit)}" if limit else "",
    )

    SessionFactory = create_async_session()
    async with SessionFactory() as db:
        rows = (await db.execute(text(sql))).all()
    stats["pending"] = len(rows)
    logger.info(f"[transcripts] {len(rows)} pending announcement(s)")
    if dry_run or not rows:
        return stats

    async with httpx.AsyncClient() as http:
        for i, r in enumerate(rows):
            symbol = _first_symbol(r.symbols_mentioned)
            if not symbol:
                stats["no_symbol"] += 1
                logger.warning(f"[transcripts] no symbol for {r.id}: {r.headline[:70]}")
                continue

            data, status = await _fetch_pdf(http, r.url)
            full_text = None
            pages = chars = None
            page_offsets: list[dict] = []
            if data is not None:
                try:
                    full_text, pages, page_offsets = await asyncio.to_thread(
                        extract_full_text, data)
                except Exception as e:
                    logger.warning(f"[transcripts] extract error {symbol}: {e}")
                    full_text = None
                status = "ok" if full_text else (status or "extract_failed")
                if (status == "ok" and pages
                        and len(full_text) / pages < MIN_CHARS_PER_PAGE):
                    status = "needs_ocr"  # scanned/image PDF — text is a stub
                chars = len(full_text) if full_text else None

            _UPSERT = text("""
                    INSERT INTO concall_transcripts
                        (symbol, news_id, source, headline, announced_at,
                         pdf_url, pdf_bytes, page_count, char_count,
                         full_text, parse_status, period_label)
                    VALUES (:sym, :nid, :src, :head, :ann, :url, :bytes,
                            :pages, :chars, :text, :status, :period)
                    ON CONFLICT (news_id) DO UPDATE SET
                        period_label = EXCLUDED.period_label,
                        full_text = EXCLUDED.full_text,
                        pdf_bytes = EXCLUDED.pdf_bytes,
                        page_count = EXCLUDED.page_count,
                        char_count = EXCLUDED.char_count,
                        parse_status = EXCLUDED.parse_status,
                        updated_at = now()
            """)
            params = {
                "sym": symbol, "nid": r.id, "src": r.source,
                "head": r.headline, "ann": r.timestamp_utc, "url": r.url,
                "bytes": len(data) if data else None, "pages": pages,
                "chars": chars, "text": full_text,
                "status": status,
                "period": derive_period(full_text, r.timestamp_utc),
            }
            try:
                async with SessionFactory() as db:
                    await db.execute(_UPSERT, params)
                    await db.commit()
            except Exception as e:
                # 2026-08-18 wedge postmortem: an unstorable document (lone
                # UTF-16 surrogates in extracted text) raised here, killed
                # the whole cycle, and — because the pending queue re-picks
                # it first every cycle — froze the backfill for hours. One
                # document must never block the queue: record a minimal
                # store_failed row (no full_text) so the item leaves the
                # pending set, and keep moving. Retryable later via
                # --retry-failed once the cause is fixed.
                logger.error(f"[transcripts] store FAILED for {symbol} "
                             f"{r.id}: {type(e).__name__}: {str(e)[:200]} — "
                             f"recording store_failed, queue continues")
                try:
                    async with SessionFactory() as db:
                        await db.execute(_UPSERT, {
                            **params, "text": None, "chars": None,
                            "period": None, "status": "store_failed"})
                        await db.commit()
                    stats["store_failed"] = stats.get("store_failed", 0) + 1
                except Exception as e2:
                    logger.error(f"[transcripts] fallback store also failed "
                                 f"for {r.id}: {e2}")
                await asyncio.sleep(POLITENESS_DELAY_S)
                continue

            stats[status] = stats.get(status, 0) + 1
            if status == "ok":
                stats["total_chars"] += chars or 0
                logger.info(
                    f"[transcripts] {symbol}: {pages}p / {chars:,} chars "
                    f"({i + 1}/{len(rows)})"
                )
                if structure:
                    ok = await _structure_row(
                        SessionFactory, r.id, symbol, full_text, page_offsets)
                    stats["structured" if ok else "structure_failed"] = (
                        stats.get("structured" if ok else "structure_failed", 0) + 1)
            await asyncio.sleep(POLITENESS_DELAY_S)

    logger.info(f"[transcripts] DONE: {stats}")
    return stats


async def _structure_row(SessionFactory, news_id: str, symbol: str,
                         full_text: str, page_offsets: list[dict]) -> bool:
    """Run LLM structuring for one stored transcript; fail-soft. The
    transcript row stays available (parse_status ok) regardless — structure
    is an annotation layer, never a gate on availability."""
    from bharat_terminal.kb.sync.transcript_structure import (
        STRUCTURE_MODEL, structure_transcript,
    )

    try:
        struct = await structure_transcript(full_text, page_offsets, symbol)
    except Exception as e:
        logger.warning(f"[transcripts] structuring error {symbol}: {e}")
        struct = None

    # Cover-letter gate: a document with ZERO speaking turns is an intimation
    # notice ("transcript available on our website"), not a transcript. The
    # 2026-07-19 audit found 21 of these listed as transcripts (LT, IOC,
    # HYUNDAI…) rendering empty readers. parse_status='not_transcript' drops
    # them from every customer list (which filter parse_status='ok').
    not_transcript = bool(
        struct and struct["stats"].get("turns_total", 0) == 0)
    if not_transcript:
        logger.info(f"[transcripts] {symbol}: zero speaking turns — "
                    f"classified not_transcript (intimation notice)")

    async with SessionFactory() as db:
        await db.execute(text("""
            UPDATE concall_transcripts
            SET structure = :struct, structure_status = :st,
                structure_model = :model, structured_at = now(),
                parse_status = CASE WHEN :nt THEN 'not_transcript'
                                    ELSE parse_status END,
                updated_at = now()
            WHERE news_id = :nid
        """), {"struct": json.dumps(struct) if struct else None,
               "st": "ok" if struct else "failed",
               "model": STRUCTURE_MODEL, "nid": news_id,
               "nt": not_transcript})
        await db.commit()

    if struct is not None and not not_transcript:
        try:
            await _guidance_row(SessionFactory, news_id, symbol, full_text)
        except Exception as e:  # guidance must never block structure/availability
            logger.warning(f"[transcripts] guidance extraction failed {symbol}: {e}")
        try:
            await _embed_row(SessionFactory, news_id, symbol, full_text, struct)
        except Exception as e:  # embedding must never block availability either
            logger.warning(f"[transcripts] embedding failed {symbol}: {e}")
    return struct is not None


async def _embed_row(SessionFactory, news_id: str, symbol: str,
                     full_text: str, struct: dict) -> None:
    from bharat_terminal.kb.sync.transcript_embed import embed_transcript

    async with SessionFactory() as db:
        me = (await db.execute(text(
            "SELECT id, announced_at FROM concall_transcripts "
            "WHERE news_id = :nid"), {"nid": news_id})).first()
    if not me:
        return
    n = await embed_transcript(SessionFactory, me.id, symbol,
                               me.announced_at, full_text, struct)
    logger.info(f"[transcripts] {symbol}: embedded {n} chunk(s)")


async def embed_backfill(limit: Optional[int] = None) -> dict:
    """Chunk + embed transcripts lacking the current embed version."""
    from bharat_terminal.kb.sync.utils import create_async_session
    from bharat_terminal.kb.sync.transcript_embed import (
        EMBED_VERSION, embed_transcript,
    )

    stats = {"candidates": 0, "embedded": 0, "failed": 0, "chunks": 0}
    SessionFactory = create_async_session()
    async with SessionFactory() as db:
        rows = (await db.execute(text(f"""
            SELECT id, symbol, announced_at, full_text, structure
            FROM concall_transcripts
            WHERE parse_status = 'ok' AND structure_status = 'ok'
              AND coalesce(embed_version, 0) < :ver
            ORDER BY announced_at ASC
            {f"LIMIT {int(limit)}" if limit else ""}
        """), {"ver": EMBED_VERSION})).all()
    stats["candidates"] = len(rows)
    logger.info(f"[transcripts] embed backfill: {len(rows)} candidate(s)")
    for r in rows:
        try:
            n = await embed_transcript(SessionFactory, r.id, r.symbol,
                                       r.announced_at, r.full_text, r.structure)
            stats["embedded"] += 1
            stats["chunks"] += n
        except Exception as e:
            stats["failed"] += 1
            logger.warning(f"[transcripts] embed backfill {r.symbol}: {e}")
    logger.info(f"[transcripts] embed backfill DONE: {stats}")
    return stats


async def _guidance_row(SessionFactory, news_id: str, symbol: str,
                        full_text: str) -> bool:
    """Extract structured guidance + QoQ verdicts for one transcript.
    Pairs against the company's PREVIOUS ok call (needs that call's guidance
    already extracted — process chronologically). Idempotent: replaces the
    transcript's guidance rows. Fail-soft."""
    from bharat_terminal.kb.sync.transcript_guidance import (
        GUIDANCE_VERSION, extract_guidance,
    )

    async with SessionFactory() as db:
        me = (await db.execute(text(
            "SELECT id, announced_at FROM concall_transcripts "
            "WHERE news_id = :nid"), {"nid": news_id})).first()
        if not me:
            return False
        prior_rows = (await db.execute(text("""
            SELECT g.id, g.metric, g.period, g.value_low, g.value_high, g.unit
            FROM transcript_guidance g
            JOIN concall_transcripts prior ON prior.id = g.transcript_id
            WHERE prior.symbol = :sym AND prior.parse_status = 'ok'
              AND prior.announced_at < :ann
              AND prior.announced_at = (
                  SELECT max(p2.announced_at) FROM concall_transcripts p2
                  WHERE p2.symbol = :sym AND p2.parse_status = 'ok'
                    AND p2.announced_at < :ann)
        """), {"sym": symbol, "ann": me.announced_at})).mappings().all()

    out = await extract_guidance(full_text, symbol, [dict(p) for p in prior_rows])
    if out is None:
        return False
    async with SessionFactory() as db:
        await db.execute(text(
            "DELETE FROM transcript_guidance WHERE transcript_id = :tid"),
            {"tid": me.id})
        await db.execute(text(
            "UPDATE concall_transcripts SET guidance_version = :ver, "
            "updated_at = now() WHERE id = :tid"),
            {"ver": GUIDANCE_VERSION, "tid": me.id})
        for it in out["items"]:
            await db.execute(text("""
                INSERT INTO transcript_guidance
                    (transcript_id, symbol, announced_at, metric, period,
                     value_low, value_high, unit, quote_start, quote_end,
                     prior_guidance_id, change_verdict, extraction_version)
                VALUES (:tid, :sym, :ann, :metric, :period, :lo, :hi, :unit,
                        :qs, :qe, :prior, :verdict, :ver)
            """), {"tid": me.id, "sym": symbol, "ann": me.announced_at,
                   "metric": it["metric"], "period": it["period"],
                   "lo": it["value_low"], "hi": it["value_high"],
                   "unit": it["unit"], "qs": it["quote_start"],
                   "qe": it["quote_end"], "prior": it["prior_guidance_id"],
                   "verdict": it["change_verdict"], "ver": GUIDANCE_VERSION})
        await db.commit()
    return True


async def extract_guidance_backfill(limit: Optional[int] = None,
                                    symbols: Optional[list[str]] = None) -> dict:
    """Chronological guidance extraction over transcripts that lack it —
    oldest first, so each call's extraction can pair against its
    predecessor's already-extracted items."""
    from bharat_terminal.kb.sync.utils import create_async_session
    from bharat_terminal.kb.sync.transcript_guidance import GUIDANCE_VERSION

    stats = {"candidates": 0, "extracted": 0, "failed": 0}
    SessionFactory = create_async_session()
    async with SessionFactory() as db:
        rows = (await db.execute(text(f"""
            SELECT news_id, symbol, full_text FROM concall_transcripts ct
            WHERE parse_status = 'ok' AND structure_status = 'ok'
              AND coalesce(guidance_version, 0) < :ver
              {"AND symbol = ANY(:syms)" if symbols else ""}
            ORDER BY announced_at ASC
            {f"LIMIT {int(limit)}" if limit else ""}
        """), {"ver": GUIDANCE_VERSION,
               **({"syms": symbols} if symbols else {})})).all()
    stats["candidates"] = len(rows)
    logger.info(f"[transcripts] guidance backfill: {len(rows)} candidate(s)")
    for r in rows:
        try:
            ok = await _guidance_row(SessionFactory, r.news_id, r.symbol,
                                     r.full_text)
        except Exception as e:
            logger.warning(f"[transcripts] guidance backfill {r.symbol}: {e}")
            ok = False
        stats["extracted" if ok else "failed"] += 1
    logger.info(f"[transcripts] guidance backfill DONE: {stats}")
    return stats


async def restructure(limit: Optional[int] = None) -> dict:
    """Re-fetch + re-extract + structure rows that have no good structure yet
    (backfill for pre-phase-2 rows and --retry for failed structuring).
    Re-extraction also (re)computes page offsets, updating full_text to the
    offset-consistent per-page extraction — content identical, still unclipped."""
    from bharat_terminal.kb.sync.utils import create_async_session
    from bharat_terminal.kb.sync.transcript_structure import STRUCTURE_VERSION

    stats = {"candidates": 0, "structured": 0, "structure_failed": 0,
             "fetch_failed": 0}
    SessionFactory = create_async_session()
    async with SessionFactory() as db:
        # Candidates: never structured, failed, or built by an older prompt
        # vintage — bumping STRUCTURE_VERSION re-annotates the corpus.
        rows = (await db.execute(text(f"""
            SELECT news_id, symbol, pdf_url FROM concall_transcripts
            WHERE parse_status = 'ok'
              AND (structure_status != 'ok'
                   OR coalesce((structure->>'version')::int, 1) < :ver)
            ORDER BY announced_at DESC
            {f"LIMIT {int(limit)}" if limit else ""}
        """), {"ver": STRUCTURE_VERSION})).all()
    stats["candidates"] = len(rows)
    logger.info(f"[transcripts] restructure: {len(rows)} candidate(s)")

    async with httpx.AsyncClient() as http:
        for r in rows:
            data, _ = await _fetch_pdf(http, r.pdf_url)
            if data is None:
                stats["fetch_failed"] += 1
                continue
            full_text, pages, page_offsets = await asyncio.to_thread(
                extract_full_text, data)
            if not full_text:
                stats["fetch_failed"] += 1
                continue
            async with SessionFactory() as db:
                await db.execute(text("""
                    UPDATE concall_transcripts
                    SET full_text = :t, page_count = :p, char_count = :c,
                        updated_at = now()
                    WHERE news_id = :nid
                """), {"t": full_text, "p": pages, "c": len(full_text),
                       "nid": r.news_id})
                await db.commit()
            ok = await _structure_row(SessionFactory, r.news_id, r.symbol,
                                      full_text, page_offsets)
            stats["structured" if ok else "structure_failed"] += 1
            await asyncio.sleep(POLITENESS_DELAY_S)

    logger.info(f"[transcripts] restructure DONE: {stats}")
    return stats


_NSE_ANN_API = "https://www.nseindia.com/api/corporate-announcements"
_NSE_API_HEADERS = {
    **_PDF_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}
NSE_API_DELAY_S = float(os.getenv("TRANSCRIPT_NSE_API_DELAY_S", "1.5"))


async def _nse_announcements(client: httpx.AsyncClient, symbol: str,
                             d_from: str, d_to: str) -> Optional[list]:
    """Historical per-symbol announcements from the NSE API (dates DD-MM-YYYY).
    None on failure — the caller skips and the row is retried on a later run."""
    try:
        resp = await client.get(
            _NSE_ANN_API,
            params={"index": "equities", "symbol": symbol,
                    "from_date": d_from, "to_date": d_to},
            headers=_NSE_API_HEADERS, timeout=25.0, follow_redirects=True)
        if resp.status_code != 200:
            logger.warning(f"[transcripts] NSE API {resp.status_code} for {symbol}")
            return None
        body = resp.text.strip()
        if not body.startswith(("[", "{")):
            logger.warning(f"[transcripts] NSE API non-JSON for {symbol}: {body[:60]!r}")
            return None
        data = resp.json()
        return data if isinstance(data, list) else data.get("data") or []
    except Exception as e:
        logger.warning(f"[transcripts] NSE API error for {symbol}: {e}")
        return None


def _pick_announcement(anns: list, headline: str) -> Optional[str]:
    """Choose the transcript announcement's PDF URL. Exact attchmntText match
    first (our stored headline IS NSE's attchmntText), then any transcript-
    subject row with a PDF attachment."""
    def pdf(a) -> Optional[str]:
        f = (a.get("attchmntFile") or "").strip()
        return f if ".pdf" in f.lower() else None

    h = " ".join((headline or "").split()).lower()
    for a in anns:
        if " ".join((a.get("attchmntText") or "").split()).lower() == h and pdf(a):
            return pdf(a)
    for a in anns:
        blob = f"{a.get('attchmntText') or ''} {a.get('desc') or ''}".lower()
        if "transcript" in blob and pdf(a):
            return pdf(a)
    return None


async def backfill_nse(limit: Optional[int] = None, structure: bool = False) -> dict:
    """Recover PDF URLs for pre-2026-07-04 transcript announcements (stored
    with quote-page URLs before ingestion captured attachments) via the NSE
    announcements API, then fetch + store the complete transcripts.

    Availability-first: structuring defaults OFF here (rows land as
    structure_status=pending); run --restructure afterwards for annotations.
    Rows whose URL cannot be recovered are stored as parse_status
    'url_not_found' so re-runs don't re-query NSE for them (--retry-failed
    re-attempts explicitly)."""
    from datetime import timedelta
    from bharat_terminal.kb.sync.utils import create_async_session

    stats = {"pending": 0, "ok": 0, "url_not_found": 0, "fetch_failed": 0,
             "extract_failed": 0, "not_pdf": 0, "no_symbol": 0,
             "api_failed": 0, "total_chars": 0}
    SessionFactory = create_async_session()
    async with SessionFactory() as db:
        rows = (await db.execute(text(f"""
            SELECT ni.id, ni.source, ni.headline, ni.url, ni.timestamp_utc,
                   ni.symbols_mentioned
            FROM news_items ni
            WHERE ni.headline ILIKE '%transcript%'
              AND ni.source = 'NSE_FILINGS'
              AND ni.url NOT ILIKE '%.pdf%'
              AND NOT EXISTS (SELECT 1 FROM concall_transcripts ct
                              WHERE ct.news_id = ni.id)
            ORDER BY ni.timestamp_utc ASC
            {f"LIMIT {int(limit)}" if limit else ""}
        """))).all()
    stats["pending"] = len(rows)
    logger.info(f"[transcripts] backfill: {len(rows)} announcement(s) to recover")

    ann_cache: dict = {}
    async with httpx.AsyncClient() as http:
        for i, r in enumerate(rows):
            symbol = _first_symbol(r.symbols_mentioned)
            if not symbol:
                stats["no_symbol"] += 1
                continue

            # ±2-day window around the announcement, cached per (symbol, day).
            day = r.timestamp_utc.date()
            key = (symbol, day)
            if key not in ann_cache:
                d_from = (day - timedelta(days=2)).strftime("%d-%m-%Y")
                d_to = (day + timedelta(days=2)).strftime("%d-%m-%Y")
                ann_cache[key] = await _nse_announcements(http, symbol, d_from, d_to)
                await asyncio.sleep(NSE_API_DELAY_S)
            anns = ann_cache[key]
            if anns is None:
                stats["api_failed"] += 1
                continue

            pdf_url = _pick_announcement(anns, r.headline)
            if not pdf_url:
                stats["url_not_found"] += 1
                await _store_stub(SessionFactory, r, symbol, "url_not_found")
                continue

            data, status = await _fetch_pdf(http, pdf_url)
            full_text = None
            pages = chars = None
            page_offsets: list[dict] = []
            if data is not None:
                try:
                    full_text, pages, page_offsets = await asyncio.to_thread(
                        extract_full_text, data)
                except Exception as e:
                    logger.warning(f"[transcripts] extract error {symbol}: {e}")
                status = "ok" if full_text else (status or "extract_failed")
                if (status == "ok" and pages
                        and len(full_text) / pages < MIN_CHARS_PER_PAGE):
                    status = "needs_ocr"  # scanned/image PDF — text is a stub
                chars = len(full_text) if full_text else None

            async with SessionFactory() as db:
                await db.execute(text("""
                    INSERT INTO concall_transcripts
                        (symbol, news_id, source, headline, announced_at,
                         pdf_url, pdf_bytes, page_count, char_count,
                         full_text, parse_status, period_label)
                    VALUES (:sym, :nid, :src, :head, :ann, :url, :bytes,
                            :pages, :chars, :text, :status, :period)
                    ON CONFLICT (news_id) DO NOTHING
                """), {"sym": symbol, "nid": r.id, "src": r.source,
                       "head": r.headline, "ann": r.timestamp_utc,
                       "url": pdf_url, "bytes": len(data) if data else None,
                       "pages": pages, "chars": chars, "text": full_text,
                       "status": status,
                       "period": derive_period(full_text, r.timestamp_utc)})
                await db.commit()

            stats[status] = stats.get(status, 0) + 1
            if status == "ok":
                stats["total_chars"] += chars or 0
                logger.info(f"[transcripts] backfill {symbol}: {pages}p / "
                            f"{chars:,} chars ({i + 1}/{len(rows)})")
                if structure:
                    await _structure_row(SessionFactory, r.id, symbol,
                                         full_text, page_offsets)
            await asyncio.sleep(POLITENESS_DELAY_S)

    logger.info(f"[transcripts] backfill DONE: {stats}")
    return stats


async def _store_stub(SessionFactory, r, symbol: str, status: str) -> None:
    async with SessionFactory() as db:
        await db.execute(text("""
            INSERT INTO concall_transcripts
                (symbol, news_id, source, headline, announced_at, pdf_url,
                 parse_status)
            VALUES (:sym, :nid, :src, :head, :ann, :url, :status)
            ON CONFLICT (news_id) DO NOTHING
        """), {"sym": symbol, "nid": r.id, "src": r.source, "head": r.headline,
               "ann": r.timestamp_utc, "url": r.url, "status": status})
        await db.commit()


# ── Letter-link recovery ───────────────────────────────────────────────────
# Some companies file only an intimation letter ("transcript available on our
# website") — the real transcript lives at a link inside the letter. Recovery
# re-reads the letter PDF's link annotations (immune to text-extraction URL
# mangling), falls back to a text regex, follows IR-page HTML one hop, and
# runs every candidate through the SAME gates as normal ingestion (PDF magic,
# density floor, zero-turn classification) — a wrong document cannot land.

_URL_RE = re.compile(r"https?://[^\s<>\")\]]{8,200}")
_SKIP_HOSTS = ("nseindia", "bseindia", "sebi.gov", "twitter", "facebook",
               "linkedin", "youtube", "instagram")
RECOVERY_MIN_CHARS = int(os.getenv("TRANSCRIPT_RECOVERY_MIN_CHARS", "5000"))


def _letter_urls(letter_pdf: bytes, letter_text: str) -> list[str]:
    """Candidate URLs from a letter: PDF link annotations first (reliable),
    then text-regex finds (may be truncated by extraction)."""
    import pypdf

    urls: list[str] = []
    try:
        reader = pypdf.PdfReader(io.BytesIO(letter_pdf))
        for page in reader.pages:
            annots = page.get("/Annots")
            if annots is None:
                continue
            # /Annots is often an IndirectObject — resolve before iterating,
            # or the loop silently yields nothing.
            for annot in annots.get_object() or []:
                try:
                    uri = annot.get_object().get("/A", {}).get("/URI")
                    if uri:
                        urls.append(str(uri))
                except Exception:
                    continue
    except Exception:
        pass
    # Text-regex fallback. PDF extraction wraps long URLs across lines, so a
    # raw match is often truncated ("…Transcript-of-Q4-of-FY-" ⏎ "2025-26.pdf")
    # — also emit variants with the following tokens spliced back on.
    for m in _URL_RE.finditer(letter_text or ""):
        urls.append(m.group(0))
        acc = m.group(0)
        tail = (letter_text or "")[m.end():m.end() + 120]
        for tok in tail.split()[:3]:
            if not re.fullmatch(r"[\w.%/–—-]+", tok) or ":" in tok:
                break
            acc += tok
            urls.append(acc)
    out, seen = [], set()
    for u in urls:
        u = u.strip().rstrip(".,;)'\"").replace("\xad", "")
        low = u.lower()
        if low in seen or low.startswith("mailto:"):
            continue
        if any(h in low for h in _SKIP_HOSTS):
            continue
        seen.add(low)
        out.append(u)
    # Most-promising first: explicit transcript links, then PDFs; among
    # variants of a wrapped URL, longest (fully re-joined) first.
    out.sort(key=lambda u: ("transcript" not in u.lower(),
                            ".pdf" not in u.lower(), -len(u)))
    return out


def _html_transcript_links(base_url: str, html: str) -> list[str]:
    """One-hop scrape of an IR page: anchors that look like transcript PDFs,
    recent-period first."""
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    scored: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        blob = f"{a['href']} {a.get_text(' ', strip=True)}".lower()
        if "transcript" not in blob:
            continue
        url = urljoin(base_url, a["href"])
        if any(h in url.lower() for h in _SKIP_HOSTS):
            continue
        score = 0
        score += 2 if ".pdf" in url.lower() else 0
        score += 1 if re.search(r"q4|q1|fy[\s-]?26|fy[\s-]?27|2026", blob) else 0
        scored.append((-score, url))
    scored.sort()
    seen, out = set(), []
    for _, u in scored:
        if u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out


async def _fetch_any(client: httpx.AsyncClient, url: str):
    """Fetch an arbitrary (company-site) URL. Returns ('pdf'|'html', bytes)
    or (None, b''). No exchange Referer — these are third-party hosts."""
    try:
        resp = await client.get(url, headers={
            "User-Agent": _PDF_HEADERS["User-Agent"],
            "Accept": "text/html,application/pdf,*/*",
        }, timeout=FETCH_TIMEOUT_S, follow_redirects=True)
        if resp.status_code != 200:
            return None, b""
        data = resp.content
        if len(data) > TRANSCRIPT_PDF_MAX_BYTES:
            return None, b""
        if data[:5].startswith(b"%PDF"):
            return "pdf", data
        ctype = (resp.headers.get("content-type") or "").lower()
        if "html" in ctype or data[:200].lstrip().lower().startswith(
                (b"<!doctype", b"<html")):
            return "html", data
        return None, b""
    except Exception as e:
        logger.debug(f"[transcripts] recovery fetch failed {url}: {e}")
        return None, b""


async def _try_ingest_recovered(SessionFactory, row, pdf_bytes: bytes,
                                source_url: str) -> bool:
    """Run a recovered PDF through the normal gates; update the row only if
    it survives all of them (incl. the zero-turn gate via _structure_row)."""
    full_text, pages, page_offsets = await asyncio.to_thread(
        extract_full_text, pdf_bytes)
    if not full_text or len(full_text) < RECOVERY_MIN_CHARS:
        return False
    if pages and len(full_text) / pages < MIN_CHARS_PER_PAGE:
        return False  # scanned — would only land as needs_ocr; keep looking
    async with SessionFactory() as db:
        await db.execute(text("""
            UPDATE concall_transcripts
            SET full_text = :t, pdf_url = :url, pdf_bytes = :b,
                page_count = :p, char_count = :c, parse_status = 'ok',
                structure = NULL, structure_status = 'pending',
                period_label = :period, updated_at = now()
            WHERE news_id = :nid
        """), {"t": full_text, "url": source_url, "b": len(pdf_bytes),
               "p": pages, "c": len(full_text), "nid": row.news_id,
               "period": derive_period(full_text,
                                       getattr(row, "announced_at", None))})
        await db.commit()
    ok = await _structure_row(SessionFactory, row.news_id, row.symbol,
                              full_text, page_offsets)
    if not ok:
        return True  # transcript stored; structure retryable via --restructure
    async with SessionFactory() as db:
        status = (await db.execute(text(
            "SELECT parse_status FROM concall_transcripts WHERE news_id=:n"),
            {"n": row.news_id})).scalar_one()
    return status == "ok"  # zero-turn gate may have re-flagged it


async def recover_links(limit: Optional[int] = None) -> dict:
    """Recover real transcripts for letter-only filings (parse_status =
    'not_transcript') from the links inside the letters."""
    from bharat_terminal.kb.sync.utils import create_async_session

    stats = {"candidates": 0, "recovered": 0, "no_links": 0, "unrecovered": 0}
    SessionFactory = create_async_session()
    async with SessionFactory() as db:
        rows = (await db.execute(text(f"""
            SELECT news_id, symbol, pdf_url, full_text, announced_at
            FROM concall_transcripts WHERE parse_status = 'not_transcript'
            ORDER BY symbol
            {f"LIMIT {int(limit)}" if limit else ""}
        """))).all()
    stats["candidates"] = len(rows)
    logger.info(f"[transcripts] recovery: {len(rows)} letter-only row(s)")

    async with httpx.AsyncClient() as http:
        for r in rows:
            letter, _ = await _fetch_pdf(http, r.pdf_url)
            urls = _letter_urls(letter or b"", r.full_text or "")
            if not urls:
                stats["no_links"] += 1
                logger.info(f"[transcripts] recovery {r.symbol}: no usable links")
                continue
            recovered = False
            tried = 0
            queue = list(urls)
            while queue and tried < 8 and not recovered:
                url = queue.pop(0)
                tried += 1
                kind, data = await _fetch_any(http, url)
                if kind == "pdf":
                    recovered = await _try_ingest_recovered(
                        SessionFactory, r, data, url)
                elif kind == "html":
                    # one-hop expansion; scraped links go to the front
                    queue = _html_transcript_links(url, data.decode(
                        "utf-8", "ignore"))[:4] + queue
                await asyncio.sleep(POLITENESS_DELAY_S)
            stats["recovered" if recovered else "unrecovered"] += 1
            logger.info(f"[transcripts] recovery {r.symbol}: "
                        f"{'RECOVERED' if recovered else 'unrecovered'} "
                        f"({tried} url(s) tried)")

    logger.info(f"[transcripts] recovery DONE: {stats}")
    return stats


async def watch(interval_s: int) -> None:
    """Real-time mode: poll for new transcript announcements forever.
    Ingestion lands the announcement (with the PDF URL) within minutes of the
    exchange filing; this loop makes the transcript + structure available
    end-to-end without manual runs."""
    logger.info(f"[transcripts] watch mode, every {interval_s}s")
    from bharat_terminal.heartbeat import abeat
    while True:
        # Liveness beat for /admin/health; TTL scaled to the poll interval so
        # slow-poll configs don't read as stalled between healthy cycles.
        await abeat("transcript-watcher", ttl=max(180, interval_s * 2 + 60))
        try:
            stats = await sync_transcripts()
            if stats.get("pending"):
                logger.info(f"[transcripts] watch cycle: {stats}")
        except Exception as e:
            logger.error(f"[transcripts] watch cycle failed (continuing): {e}")
        await asyncio.sleep(interval_s)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    ap = argparse.ArgumentParser(prog="bharat_terminal.kb.sync.transcript_sync")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--retry-failed", action="store_true",
                    help="Re-attempt announcements whose last parse failed.")
    ap.add_argument("--no-structure", action="store_true",
                    help="Fetch/store only; skip the LLM annotation pass.")
    ap.add_argument("--restructure", action="store_true",
                    help="(Re)run structuring for stored transcripts lacking it.")
    ap.add_argument("--backfill-nse", action="store_true",
                    help="Recover pre-Jul-04 attachment URLs via the NSE "
                         "announcements API and ingest those transcripts "
                         "(no structuring; run --restructure after).")
    ap.add_argument("--recover-links", action="store_true",
                    help="For letter-only filings (not_transcript), fetch the "
                         "real transcript from the links inside the letter.")
    ap.add_argument("--extract-guidance", action="store_true",
                    help="Chronological guidance extraction + QoQ diffs for "
                         "transcripts lacking the current extraction version.")
    ap.add_argument("--symbols", type=str, default=None,
                    help="Comma-separated symbol filter (with --extract-guidance).")
    ap.add_argument("--embed", action="store_true",
                    help="Chunk + embed transcripts lacking the current "
                         "embedding version (retrieval substrate).")
    ap.add_argument("--watch", action="store_true",
                    help="Run forever, syncing new transcripts as they arrive.")
    ap.add_argument("--interval", type=int,
                    default=int(os.getenv("TRANSCRIPT_WATCH_INTERVAL_S", "180")))
    args = ap.parse_args()

    if args.watch:
        asyncio.run(watch(args.interval))
        return
    if args.backfill_nse:
        # Availability-first: never structure inline here; --restructure after.
        stats = asyncio.run(backfill_nse(limit=args.limit, structure=False))
        sys.exit(0 if stats.get("api_failed", 0) == 0 else 1)
    if args.recover_links:
        stats = asyncio.run(recover_links(limit=args.limit))
        sys.exit(0)
    if args.embed:
        stats = asyncio.run(embed_backfill(limit=args.limit))
        sys.exit(0 if stats.get("failed", 0) == 0 else 1)
    if args.extract_guidance:
        syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                if args.symbols else None)
        stats = asyncio.run(extract_guidance_backfill(limit=args.limit,
                                                      symbols=syms))
        sys.exit(0 if stats.get("failed", 0) == 0 else 1)
    if args.restructure:
        stats = asyncio.run(restructure(limit=args.limit))
        sys.exit(0 if stats.get("fetch_failed", 0) == 0 else 1)
    stats = asyncio.run(sync_transcripts(
        limit=args.limit, dry_run=args.dry_run, retry_failed=args.retry_failed,
        structure=not args.no_structure))
    sys.exit(0 if stats.get("fetch_failed", 0) == 0 else 1)


if __name__ == "__main__":
    main()
