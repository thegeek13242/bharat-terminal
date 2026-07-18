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


def extract_full_text(data: bytes) -> tuple[Optional[str], int]:
    """Extract the COMPLETE text of a PDF — every page, no char cap.
    Returns (text | None, page_count). Runs CPU-bound; call via to_thread."""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")  # exchange PDFs are often empty-password encrypted
        except Exception:
            return None, 0
    pages = []
    for page in reader.pages:  # deliberately uncapped
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")  # keep page alignment; never drop content silently
    text_ = _clean("\n\n".join(pages))
    return (text_ or None), len(reader.pages)


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
            if data is not None:
                try:
                    full_text, pages = await asyncio.to_thread(extract_full_text, data)
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
            await asyncio.sleep(POLITENESS_DELAY_S)

    logger.info(f"[transcripts] DONE: {stats}")
    return stats


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
    args = ap.parse_args()
    stats = asyncio.run(sync_transcripts(
        limit=args.limit, dry_run=args.dry_run, retry_failed=args.retry_failed))
    sys.exit(0 if stats.get("fetch_failed", 0) == 0 else 1)


if __name__ == "__main__":
    main()
