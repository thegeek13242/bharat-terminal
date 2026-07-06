"""
Fetch + extract text from NSE filing PDF attachments.

NSE corporate announcements carry the real content in a PDF (`attchmntFile`);
the JSON only exposes a truncated `attchmntText`. This module downloads that
PDF and extracts its text so the analysis pipeline reads the full filing
instead of a clipped headline.

Design notes:
- pypdf is pure-Python (no system/poppler deps) — keeps the slim image lean.
- Extraction is CPU-bound and runs in a worker thread (`asyncio.to_thread`) so
  it never blocks the adapter's event loop.
- Hard guardrails (size cap, page cap, timeout) bound cost on the tier-1 hot
  path: a stray 50 MB annual report must not stall filing ingestion.
- Fail-soft: any failure returns None, and the caller falls back to the
  truncated `attchmntText`. We never block a filing on a bad attachment.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# Guardrails (env-tunable without a rebuild). Accuracy over latency: read the
# COMPLETE attachment — generous caps so long/detailed filings aren't truncated
# (and large filings aren't skipped) before analysis. A higher timeout lets big
# PDFs finish rather than failing soft to the truncated attchmntText.
PDF_MAX_BYTES = int(os.getenv("FILING_PDF_MAX_BYTES", str(50 * 1024 * 1024)))  # 50 MB
PDF_MAX_PAGES = int(os.getenv("FILING_PDF_MAX_PAGES", "200"))
PDF_FETCH_TIMEOUT_S = float(os.getenv("FILING_PDF_TIMEOUT_S", "30"))
PDF_TEXT_MAX_CHARS = int(os.getenv("FILING_PDF_TEXT_MAX_CHARS", "120000"))

# nsearchives serves the PDF; a browser-like UA + NSE referer avoids 403.
_PDF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
    "Referer": "https://www.nseindia.com/",
}

_WS_RE = re.compile(r"[ \t]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    """Normalise whitespace; drop NULs/replacement chars left by font quirks.

    We deliberately do NOT try to reverse-guess ligatures (ti/ff/fi): a wrong
    guess would corrupt content. Numbers extract reliably; only some letter
    pairs garble, which the LLM tolerates."""
    text = text.replace("\x00", "").replace("�", "")
    # Join hyphenated line breaks: "compa-\nny" -> "company".
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def _extract_text(data: bytes) -> Optional[str]:
    """Extract text from PDF bytes (runs in a thread). None on failure/empty."""
    try:
        import pypdf
    except ImportError:  # pragma: no cover
        logger.error("[pdf] pypdf not installed; cannot extract filing attachments")
        return None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")  # many NSE PDFs are empty-password encrypted
            except Exception:
                return None
        parts = []
        for page in reader.pages[:PDF_MAX_PAGES]:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = _clean("\n".join(parts))
        return text or None
    except Exception as e:
        logger.warning(f"[pdf] extraction failed: {e}")
        return None


async def fetch_pdf_text(
    url: str, session: aiohttp.ClientSession,
    referer: str = "https://www.nseindia.com/",
) -> Optional[str]:
    """Download `url` and return extracted text, or None on any failure.

    Bounded by PDF_FETCH_TIMEOUT_S / PDF_MAX_BYTES / PDF_MAX_PAGES so it is safe
    to call inline on the filing path. `referer` must match the exchange host
    (NSE default; BSE attachments need a bseindia referer)."""
    if not url or ".pdf" not in url.lower():
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=PDF_FETCH_TIMEOUT_S)
        headers = {**_PDF_HEADERS, "Referer": referer}
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning(f"[pdf] {resp.status} fetching {url}")
                return None
            ctype = (resp.headers.get("content-type") or "").lower()
            clen = resp.headers.get("content-length")
            if clen and clen.isdigit() and int(clen) > PDF_MAX_BYTES:
                logger.warning(f"[pdf] too large ({clen} bytes), skipping {url}")
                return None
            data = await resp.read()
        if len(data) > PDF_MAX_BYTES:
            logger.warning(f"[pdf] too large ({len(data)} bytes), skipping {url}")
            return None
        if "pdf" not in ctype and not data[:5].startswith(b"%PDF"):
            logger.warning(f"[pdf] not a PDF (ctype={ctype!r}) at {url}")
            return None
        text = await asyncio.to_thread(_extract_text, data)
        if text:
            return text[:PDF_TEXT_MAX_CHARS]
        return None
    except asyncio.TimeoutError:
        logger.warning(f"[pdf] timeout after {PDF_FETCH_TIMEOUT_S}s fetching {url}")
        return None
    except Exception as e:
        logger.warning(f"[pdf] error fetching {url}: {e}")
        return None
