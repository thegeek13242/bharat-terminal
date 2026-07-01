"""Analysis-side client for the multi-tier event memory served by kb-service.

One POST to /memory/context returns market + sector + company memory for a news
item, relevance-ranked to it (the KB embeds the news once, server-side). Fail-soft
by design: any error → None, so memory is purely additive context and never blocks
an analysis. Formatting lives here (self-contained — no kb/sqlalchemy import, since
this runs in the analysis image).

Gated by ANALYSIS_MARKET_MEMORY_ENABLED / ANALYSIS_SECTOR_MEMORY_ENABLED so the
tiers can be rolled out (or cut) without a redeploy.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

KNOWLEDGE_BASE_URL = os.getenv("KNOWLEDGE_BASE_URL", "http://kb-service:8001")
MARKET_ENABLED = os.getenv("ANALYSIS_MARKET_MEMORY_ENABLED", "1") == "1"
SECTOR_ENABLED = os.getenv("ANALYSIS_SECTOR_MEMORY_ENABLED", "1") == "1"
CONTEXT_TIMEOUT_S = float(os.getenv("MEMORY_CONTEXT_TIMEOUT_S", "4.0"))


def any_tier_enabled(include_company: bool = True) -> bool:
    return MARKET_ENABLED or SECTOR_ENABLED or include_company


async def fetch_memory_context(
    headline: Optional[str],
    macro_theme: Optional[str],
    sectors: Optional[List[str]],
    symbols: Optional[List[str]],
    include_company: bool = True,
) -> Optional[Dict[str, Any]]:
    """POST the news context to KB → {market, sectors, companies}. None on error."""
    if not any_tier_enabled(include_company):
        return None
    payload = {
        "headline": headline,
        "macro_theme": macro_theme,
        "sectors": [s for s in (sectors or []) if s] if SECTOR_ENABLED else [],
        "symbols": [s for s in (symbols or []) if s],
        "include_company": include_company,
    }
    try:
        async with httpx.AsyncClient(timeout=CONTEXT_TIMEOUT_S) as client:
            resp = await client.post(f"{KNOWLEDGE_BASE_URL}/memory/context", json=payload)
            if resp.status_code == 200:
                return resp.json()
            logger.debug(f"memory context HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"memory context fetch failed: {e}")
    return None


def _event_lines(events: Optional[List[dict]], n: int) -> List[str]:
    out = []
    for e in (events or [])[:n]:
        out.append(
            f"    [{e.get('days_ago', 0):.0f}d ago] {e.get('sentiment')} "
            f"{e.get('magnitude')}/5: {e.get('summary', '')}"
        )
    return out


def format_market_block(market: Optional[dict]) -> str:
    """Market regime block: narrative digest (if any) + dominant themes + top events."""
    if not MARKET_ENABLED or not market:
        return ""
    digest = (market.get("digest") or {}).get("text")
    if not market.get("events") and not digest:
        return ""
    themes = ", ".join(
        f"{t['theme']}({t['tilt']})" for t in (market.get("by_theme") or [])[:4]
    )
    head = (f"MARKET REGIME (last {market.get('window_days')}d; net tilt: "
            f"{market.get('net_tilt')}" + (f"; themes: {themes}" if themes else "") + "):")
    lines = [head]
    if digest:
        lines.append(f"  {digest}")
    lines += _event_lines(market.get("events"), 4)
    return "\n".join(lines)


def format_sector_block(sec: Optional[dict]) -> str:
    if not SECTOR_ENABLED or not sec:
        return ""
    digest = (sec.get("digest") or {}).get("text")
    if not sec.get("events") and not digest:
        return ""
    head = (f"SECTOR {sec.get('sector', '')} (last {sec.get('window_days')}d; "
            f"net tilt: {sec.get('net_tilt')}):")
    lines = [head]
    if digest:
        lines.append(f"  {digest}")
    lines += _event_lines(sec.get("events"), 3)
    return "\n".join(lines)


def format_sectors_block(context: Optional[dict]) -> str:
    """All affected-sector blocks concatenated."""
    if not context:
        return ""
    blocks = [format_sector_block(s) for s in (context.get("sectors") or {}).values()]
    return "\n".join(b for b in blocks if b)


def market_and_sector_backdrop(context: Optional[dict]) -> str:
    """Combined MARKET + SECTOR backdrop for a prompt. '' when nothing to show."""
    if not context:
        return ""
    parts = [format_market_block(context.get("market")), format_sectors_block(context)]
    return "\n".join(p for p in parts if p)
