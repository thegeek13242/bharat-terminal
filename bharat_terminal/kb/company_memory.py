"""
Per-company event memory — bounded, recency-decayed context for the analysis
agents.

Memory is DERIVED ON READ from impact_reports (the source of truth), never a
separate store. That makes it impossible for the memory to drift from reality or
grow without bound. It is bounded three ways so old/irrelevant events cannot
skew a fresh analysis:

  1. Recency window   — only impacts in the last COMPANY_MEMORY_WINDOW_DAYS.
  2. Materiality gate — only DIRECT impacts (hop_distance=0) with magnitude
                        >= COMPANY_MEMORY_MIN_MAGNITUDE (drops routine, e.g.
                        "trading window closed" magnitude-1 noise).
  3. Salience top-K   — rank by magnitude * exp(-age/half_life) and keep the top
                        COMPANY_MEMORY_TOP_K, so a stale minor event can never
                        crowd out a recent material one.

Repeat coverage of the same event (same sentiment/magnitude, near-identical
explanation) is collapsed so a story covered by five outlets counts once.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

WINDOW_DAYS = int(os.getenv("COMPANY_MEMORY_WINDOW_DAYS", "15"))
MIN_MAGNITUDE = int(os.getenv("COMPANY_MEMORY_MIN_MAGNITUDE", "2"))
TOP_K = int(os.getenv("COMPANY_MEMORY_TOP_K", "8"))
HALF_LIFE_DAYS = float(os.getenv("COMPANY_MEMORY_HALFLIFE_DAYS", "14"))
# Cap rows pulled before ranking — bounds the per-call cost on a hot path.
SCAN_LIMIT = int(os.getenv("COMPANY_MEMORY_SCAN_LIMIT", "120"))

_SIGN = {"positive": 1, "negative": -1, "neutral": 0}

# Direct, material impacts for one symbol inside the recency window. The
# created_at index makes the window scan cheap; LATERAL unnests company_impacts.
_SQL = text("""
    SELECT ir.created_at, imp
    FROM impact_reports ir
    CROSS JOIN LATERAL jsonb_array_elements(ir.company_impacts) AS imp
    WHERE ir.created_at >= now() - make_interval(days => :days)
      AND imp->>'symbol' = :symbol
      AND COALESCE(NULLIF(imp->>'hop_distance', '')::int, 0) = 0
      AND COALESCE(NULLIF(imp->>'magnitude', '')::int, 0) >= :min_mag
    ORDER BY ir.created_at DESC
    LIMIT :scan_limit
""")


def _first_clause(text_: str, limit: int = 160) -> str:
    """First sentence/clause of an explanation, for a compact memory line."""
    s = re.sub(r"\s+", " ", (text_ or "")).strip()
    m = re.search(r"[.;]", s)
    if m and m.start() <= limit:
        return s[: m.start() + 1]
    return s[:limit] + ("…" if len(s) > limit else "")


def _tilt_label(net: float) -> str:
    if net >= 4:
        return "strongly positive"
    if net >= 1.0:
        return "slightly positive"
    if net <= -4:
        return "strongly negative"
    if net <= -1.0:
        return "slightly negative"
    return "neutral / mixed"


async def build_company_memory(
    session: AsyncSession, symbol: str, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Assemble the bounded event memory for one symbol. Always returns a dict
    (empty events list when the company has no material recent history)."""
    symbol = (symbol or "").upper().strip()
    now = now or datetime.now(timezone.utc)
    if not symbol:
        return {"symbol": symbol, "window_days": WINDOW_DAYS,
                "event_count": 0, "net_tilt": "neutral / mixed", "events": []}

    rows = (await session.execute(_SQL, {
        "days": WINDOW_DAYS, "symbol": symbol,
        "min_mag": MIN_MAGNITUDE, "scan_limit": SCAN_LIMIT,
    })).all()

    seen: set = set()
    events = []
    for created_at, imp in rows:
        sentiment = imp.get("sentiment", "neutral")
        magnitude = int(imp.get("magnitude") or 0)
        explanation = (imp.get("explanation") or "").strip()
        # Collapse repeat coverage of the same event.
        dedup_key = (sentiment, magnitude, explanation[:80].lower())
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
        salience = magnitude * math.exp(-age_days / HALF_LIFE_DAYS)
        events.append({
            "date": created_at.date().isoformat(),
            "days_ago": round(age_days, 1),
            "sentiment": sentiment,
            "magnitude": magnitude,
            "time_horizon": imp.get("time_horizon"),
            "summary": _first_clause(explanation),
            "_salience": salience,
        })

    # Recency-weighted net tilt over ALL material events in the window …
    net = sum(
        _SIGN.get(e["sentiment"], 0) * e["magnitude"]
        * math.exp(-e["days_ago"] / HALF_LIFE_DAYS)
        for e in events
    )
    # … but only surface the top-K most salient, shown most-recent-first.
    events.sort(key=lambda e: e["_salience"], reverse=True)
    top = sorted(events[:TOP_K], key=lambda e: e["days_ago"])
    for e in top:
        e.pop("_salience", None)

    return {
        "symbol": symbol,
        "window_days": WINDOW_DAYS,
        "event_count": len(events),
        "net_tilt": _tilt_label(net),
        "events": top,
    }


def format_memory_block(mem: Optional[Dict[str, Any]]) -> str:
    """Render memory as a compact prompt block. Empty string when no history."""
    if not mem or not mem.get("events"):
        return ""
    lines = [
        f"RECENT EVENT MEMORY — {mem['symbol']} "
        f"(last {mem['window_days']}d, material only; net tilt: {mem['net_tilt']}):"
    ]
    for e in mem["events"]:
        lines.append(
            f"  [{e['days_ago']:.0f}d ago] {e['sentiment']} {e['magnitude']}/5 "
            f"({e.get('time_horizon') or 'n/a'}): {e['summary']}"
        )
    return "\n".join(lines)
