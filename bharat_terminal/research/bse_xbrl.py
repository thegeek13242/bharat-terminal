"""
BSE corporate-filings XBRL fetcher.

Two endpoints we use:

  1. Filing index   — https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w
                       (list of filings for a security code, filterable by
                        category=Result/Annual Report and date range)
  2. Filing detail  — https://api.bseindia.com/BseIndiaAPI/api/AnnGetDetails/w
                       (returns NSURL = XBRL/PDF download path)

Auth: none (BSE's web API is open). User-Agent must be set or it 403s.

This module is a TOOL surface for the research agent — every public function
is shaped to be exposed via Anthropic tool-use. The LLM decides when to
call which one; we just return clean JSON.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

BSE_INDEX_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_DETAIL_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetDetails/w"
BSE_LOOKUP_URL = "https://api.bseindia.com/BseIndiaAPI/api/PeerSmartSearch/w"

# Tight timeouts — agent calls happen in a loop, can't block on slow filings
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

# BSE's API rejects requests without a browser-ish UA + Referer.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BharatTerminal/1.0; "
        "+https://github.com/thegeek13242/bharat-terminal)"
    ),
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}


async def resolve_security_code(symbol: str) -> Optional[str]:
    """
    Map an NSE-style symbol (e.g. "RELIANCE") to a BSE 6-digit security code
    (e.g. "500325"). Falls back to None if the search returns nothing useful.
    """
    if not symbol:
        return None
    # PeerSmartSearch returns HTML-fragment chunks; the security code is the
    # first numeric run in each `<a>` tag.
    params = {"Type": "SS", "text": symbol.upper()}
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS) as client:
            resp = await client.get(BSE_LOOKUP_URL, params=params)
            resp.raise_for_status()
            text = resp.text
    except Exception as e:
        logger.debug(f"[BSE-XBRL] lookup failed for {symbol}: {e}")
        return None
    m = re.search(r">\s*(\d{6})\s*<", text)
    return m.group(1) if m else None


async def list_filings(
    security_code: str,
    *,
    category: str = "Result",          # "Result", "Annual Report", "Corp. Action"
    months_back: int = 18,
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """
    Return the recent filings for a security_code. Each item carries the
    NSURL (download link) and a `xbrl_url` if XBRL is attached.
    """
    if not security_code:
        return []
    today = date.today()
    start = today - timedelta(days=int(months_back * 30.4))
    params = {
        "pageno": 1,
        "strCat": category,
        "strPrevDate": start.strftime("%Y%m%d"),
        "strScrip": security_code,
        "strSearch": "P",
        "strToDate": today.strftime("%Y%m%d"),
        "strType": "C",
    }
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS) as client:
            resp = await client.get(BSE_INDEX_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning(f"[BSE-XBRL] list_filings failed for {security_code}: {e}")
        return []

    rows = (data.get("Table") or [])[:limit]
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "scrip_cd":      r.get("SCRIP_CD"),
                "headline":      r.get("HEADLINE") or r.get("NEWSSUB"),
                "category":      r.get("CATEGORYNAME"),
                "subcategory":   r.get("SUBCATNAME"),
                "filing_date":   r.get("News_submission_dt") or r.get("DT_TM"),
                "broadcast_date": r.get("DissemDT"),
                "pdf_url":       _pdf_url(r),
                "xbrl_url":      r.get("XBRLATTACHMENTNAME") or _xbrl_url(r),
                "summary":       r.get("MORE") or r.get("HEADLINE"),
                "newsid":        r.get("NEWSID"),
            }
        )
    return out


async def fetch_xbrl_text(xbrl_url: str, *, max_chars: int = 30_000) -> Optional[str]:
    """
    Pull the raw XBRL document and return the first `max_chars` of textual
    content. We don't parse — the agent reads it directly. This is cheap
    and avoids us pre-deciding which tags matter for which (symbol, trigger).
    """
    if not xbrl_url:
        return None
    if not xbrl_url.lower().startswith(("http://", "https://")):
        # Some BSE responses return just a filename; prefix with the canonical host.
        xbrl_url = f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{xbrl_url}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=HEADERS) as client:
            resp = await client.get(xbrl_url)
            resp.raise_for_status()
            text = resp.text
    except Exception as e:
        logger.warning(f"[BSE-XBRL] fetch_xbrl_text failed {xbrl_url}: {e}")
        return None

    # Tag-strip lightly so the LLM doesn't waste tokens on XML scaffolding,
    # but keep numeric facts in place (they live as text content of leaf tags).
    stripped = re.sub(r"<[^>]+>", " ", text)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped[:max_chars]


def _pdf_url(row: Dict[str, Any]) -> Optional[str]:
    fn = row.get("ATTACHMENTNAME") or row.get("PDFFLAG")
    if not fn:
        return None
    if str(fn).lower().startswith("http"):
        return str(fn)
    return f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{fn}"


def _xbrl_url(row: Dict[str, Any]) -> Optional[str]:
    fn = row.get("XBRLATTACHMENTNAME")
    if not fn:
        return None
    if str(fn).lower().startswith("http"):
        return str(fn)
    return f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{fn}"
