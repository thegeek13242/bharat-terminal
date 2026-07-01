"""Company tier — bounded, relevance-ranked event memory for one symbol.

Backward-compatible with the original company_memory: the response keeps
`symbol / window_days / event_count / net_tilt / events` (each event has
`days_ago / sentiment / magnitude / time_horizon / summary`); `by_theme` and
per-event `macro_theme / source_count` are additive.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from bharat_terminal.kb.memory.retrieve import retrieve_memory

WINDOW_DAYS = int(os.getenv("COMPANY_MEMORY_WINDOW_DAYS", "15"))
TOP_K = int(os.getenv("COMPANY_MEMORY_TOP_K", "8"))
HALF_LIFE = float(os.getenv("COMPANY_MEMORY_HALFLIFE_DAYS", "14"))


def _empty(symbol: str) -> Dict[str, Any]:
    return {"symbol": symbol, "window_days": WINDOW_DAYS, "event_count": 0,
            "net_tilt": "neutral / mixed", "by_theme": [], "events": []}


async def build_company_memory(
    session: AsyncSession, symbol: str,
    *, query_embedding: Optional[Sequence[float]] = None,
    query_theme: Optional[str] = None,
    query_sectors: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return _empty(symbol)
    data = await retrieve_memory(
        session, "company", symbol,
        window_days=WINDOW_DAYS, top_k=TOP_K, half_life=HALF_LIFE,
        query_embedding=query_embedding, query_theme=query_theme,
        query_sectors=query_sectors,
    )
    data["symbol"] = symbol
    return data


def format_memory_block(mem: Optional[Dict[str, Any]]) -> str:
    """Compact prompt block. Empty string when there's no history."""
    if not mem or not mem.get("events"):
        return ""
    lines = [
        f"RECENT EVENT MEMORY — {mem.get('symbol', '')} "
        f"(last {mem['window_days']}d, material only; net tilt: {mem['net_tilt']}):"
    ]
    for e in mem["events"]:
        lines.append(
            f"  [{e['days_ago']:.0f}d ago] {e['sentiment']} {e['magnitude']}/5 "
            f"({e.get('time_horizon') or 'n/a'}): {e['summary']}"
        )
    return "\n".join(lines)
