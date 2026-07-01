"""Sector tier — relevance-ranked event memory for one BSE sector code."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from bharat_terminal.kb.memory.retrieve import retrieve_memory

WINDOW_DAYS = int(os.getenv("SECTOR_MEMORY_WINDOW_DAYS", "30"))
TOP_K = int(os.getenv("SECTOR_MEMORY_TOP_K", "6"))
HALF_LIFE = float(os.getenv("SECTOR_MEMORY_HALFLIFE_DAYS", "21"))


async def build_sector_memory(
    session: AsyncSession, sector: str,
    *, query_embedding: Optional[Sequence[float]] = None,
    query_theme: Optional[str] = None,
) -> Dict[str, Any]:
    sector = (sector or "").upper().strip()
    if not sector:
        return {"sector": sector, "window_days": WINDOW_DAYS, "event_count": 0,
                "net_tilt": "neutral / mixed", "by_theme": [], "events": []}
    data = await retrieve_memory(
        session, "sector", sector,
        window_days=WINDOW_DAYS, top_k=TOP_K, half_life=HALF_LIFE,
        query_embedding=query_embedding, query_theme=query_theme,
        query_sectors={sector},
    )
    data["sector"] = sector
    return data


def format_sector_block(mem: Optional[Dict[str, Any]]) -> str:
    if not mem or not mem.get("events"):
        return ""
    themes = ", ".join(
        f"{t['theme']}({t['count']},{t['tilt']})" for t in mem.get("by_theme", [])[:3]
    )
    lines = [
        f"SECTOR EVENT MEMORY — {mem.get('sector', '')} "
        f"(last {mem['window_days']}d; net tilt: {mem['net_tilt']}"
        + (f"; themes: {themes}" if themes else "") + "):"
    ]
    for e in mem["events"]:
        lines.append(
            f"  [{e['days_ago']:.0f}d ago] {e['sentiment']} {e['magnitude']}/5: {e['summary']}"
        )
    return "\n".join(lines)
