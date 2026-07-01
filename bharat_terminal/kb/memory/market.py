"""Market tier — relevance-ranked event memory across the whole market."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from bharat_terminal.kb.memory.retrieve import retrieve_memory

WINDOW_DAYS = int(os.getenv("MARKET_MEMORY_WINDOW_DAYS", "30"))
TOP_K = int(os.getenv("MARKET_MEMORY_TOP_K", "6"))
HALF_LIFE = float(os.getenv("MARKET_MEMORY_HALFLIFE_DAYS", "21"))


async def build_market_memory(
    session: AsyncSession,
    *, query_embedding: Optional[Sequence[float]] = None,
    query_theme: Optional[str] = None,
    query_sectors: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    data = await retrieve_memory(
        session, "market", "MARKET",
        window_days=WINDOW_DAYS, top_k=TOP_K, half_life=HALF_LIFE,
        query_embedding=query_embedding, query_theme=query_theme,
        query_sectors=query_sectors,
    )
    data["scope"] = "MARKET"
    return data


def format_market_block(mem: Optional[Dict[str, Any]]) -> str:
    if not mem or not mem.get("events"):
        return ""
    themes = ", ".join(
        f"{t['theme']}({t['count']},{t['tilt']})" for t in mem.get("by_theme", [])[:4]
    )
    lines = [
        f"MARKET EVENT MEMORY (last {mem['window_days']}d; net tilt: {mem['net_tilt']}"
        + (f"; dominant themes: {themes}" if themes else "") + "):"
    ]
    for e in mem["events"]:
        lines.append(
            f"  [{e['days_ago']:.0f}d ago] {e['sentiment']} {e['magnitude']}/5: {e['summary']}"
        )
    return "\n".join(lines)
