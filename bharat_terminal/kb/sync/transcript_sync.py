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


def _clean(text_: str) -> str:
    """Whitespace normalisation only — never content removal (see
    ingestion/pdf_fetch._clean for the ligature rationale)."""
    text_ = text_.replace("\x00", "").replace("�", "")
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
                chars = len(full_text) if full_text else None

            async with SessionFactory() as db:
                await db.execute(text("""
                    INSERT INTO concall_transcripts
                        (symbol, news_id, source, headline, announced_at,
                         pdf_url, pdf_bytes, page_count, char_count,
                         full_text, parse_status)
                    VALUES (:sym, :nid, :src, :head, :ann, :url, :bytes,
                            :pages, :chars, :text, :status)
                    ON CONFLICT (news_id) DO UPDATE SET
                        full_text = EXCLUDED.full_text,
                        pdf_bytes = EXCLUDED.pdf_bytes,
                        page_count = EXCLUDED.page_count,
                        char_count = EXCLUDED.char_count,
                        parse_status = EXCLUDED.parse_status,
                        updated_at = now()
                """), {
                    "sym": symbol, "nid": r.id, "src": r.source,
                    "head": r.headline, "ann": r.timestamp_utc, "url": r.url,
                    "bytes": len(data) if data else None, "pages": pages,
                    "chars": chars, "text": full_text,
                    "status": status,
                })
                await db.commit()

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
    async with SessionFactory() as db:
        await db.execute(text("""
            UPDATE concall_transcripts
            SET structure = :struct, structure_status = :st,
                structure_model = :model, structured_at = now(),
                updated_at = now()
            WHERE news_id = :nid
        """), {"struct": json.dumps(struct) if struct else None,
               "st": "ok" if struct else "failed",
               "model": STRUCTURE_MODEL, "nid": news_id})
        await db.commit()
    return struct is not None


async def restructure(limit: Optional[int] = None) -> dict:
    """Re-fetch + re-extract + structure rows that have no good structure yet
    (backfill for pre-phase-2 rows and --retry for failed structuring).
    Re-extraction also (re)computes page offsets, updating full_text to the
    offset-consistent per-page extraction — content identical, still unclipped."""
    from bharat_terminal.kb.sync.utils import create_async_session

    stats = {"candidates": 0, "structured": 0, "structure_failed": 0,
             "fetch_failed": 0}
    SessionFactory = create_async_session()
    async with SessionFactory() as db:
        rows = (await db.execute(text(f"""
            SELECT news_id, symbol, pdf_url FROM concall_transcripts
            WHERE parse_status = 'ok' AND structure_status != 'ok'
            ORDER BY announced_at DESC
            {f"LIMIT {int(limit)}" if limit else ""}
        """))).all()
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


async def watch(interval_s: int) -> None:
    """Real-time mode: poll for new transcript announcements forever.
    Ingestion lands the announcement (with the PDF URL) within minutes of the
    exchange filing; this loop makes the transcript + structure available
    end-to-end without manual runs."""
    logger.info(f"[transcripts] watch mode, every {interval_s}s")
    while True:
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
    ap.add_argument("--watch", action="store_true",
                    help="Run forever, syncing new transcripts as they arrive.")
    ap.add_argument("--interval", type=int,
                    default=int(os.getenv("TRANSCRIPT_WATCH_INTERVAL_S", "180")))
    args = ap.parse_args()

    if args.watch:
        asyncio.run(watch(args.interval))
        return
    if args.restructure:
        stats = asyncio.run(restructure(limit=args.limit))
        sys.exit(0 if stats.get("fetch_failed", 0) == 0 else 1)
    stats = asyncio.run(sync_transcripts(
        limit=args.limit, dry_run=args.dry_run, retry_failed=args.retry_failed,
        structure=not args.no_structure))
    sys.exit(0 if stats.get("fetch_failed", 0) == 0 else 1)


if __name__ == "__main__":
    main()
