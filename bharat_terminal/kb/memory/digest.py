"""Sector / market narrative digests — the "bigger picture" prose layer.

For each sector with enough recent events (plus the whole market), build the
mechanical aggregate (net tilt + dominant themes + top events), hash it, and —
only when the aggregate changed since last time — synthesise a short strategist
note via an LLM and OVERWRITE the row in `memory_digests`. The table is keyed by
(scope_type, scope_key) so it is bounded by construction (~1 market + N sectors);
there is no accumulation and therefore no eviction job.

Run as:  python -m bharat_terminal.kb.memory.digest refresh
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal import llm
from bharat_terminal.research.db import session_scope
from bharat_terminal.kb.models import MemoryDigest
from bharat_terminal.kb.memory.market import build_market_memory
from bharat_terminal.kb.memory.sector import build_sector_memory, WINDOW_DAYS as SECTOR_WINDOW

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("memory_digest")

MEMORY_DIGEST_MODEL = os.getenv("MEMORY_DIGEST_MODEL", "gemini-3.5-flash")
MEMORY_DIGEST_MIN_EVENTS = int(os.getenv("MEMORY_DIGEST_MIN_EVENTS", "5"))

_SYSTEM = (
    "You are a buy-side market strategist writing a terse internal desk note for "
    "Indian equities. Summarise the recent event backdrop for the given scope in "
    "3-5 sentences (<=180 words): the net directional lean, the dominant themes "
    "driving it, and the most material events. Be specific and neutral; no "
    "preamble, no bullet lists, no disclaimers."
)


def _aggregate_hash(agg: Dict[str, Any]) -> str:
    payload = json.dumps({
        "net_tilt": agg.get("net_tilt"),
        "by_theme": agg.get("by_theme"),
        "events": [(e.get("summary"), e.get("magnitude"), e.get("sentiment"))
                   for e in agg.get("events", [])],
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _prompt(scope_label: str, agg: Dict[str, Any]) -> str:
    themes = ", ".join(f"{t['theme']} ({t['count']}, {t['tilt']})"
                       for t in agg.get("by_theme", []))
    events = "\n".join(
        f"  - [{e['days_ago']:.0f}d] {e['sentiment']} {e['magnitude']}/5: {e['summary']}"
        for e in agg.get("events", [])
    )
    return (
        f"SCOPE: {scope_label}\n"
        f"Window: last {agg.get('window_days')}d · events: {agg.get('event_count')} · "
        f"net tilt: {agg.get('net_tilt')}\n"
        f"Dominant themes: {themes or 'n/a'}\n"
        f"Most salient events:\n{events or '  (none)'}\n\n"
        f"Write the desk note now."
    )


async def _synthesize(scope_label: str, agg: Dict[str, Any]) -> Optional[str]:
    resp = await llm.acomplete(
        model=MEMORY_DIGEST_MODEL, max_tokens=400, system=_SYSTEM,
        messages=[{"role": "user", "content": _prompt(scope_label, agg)}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text" and b.text]
    return " ".join(parts).strip() or None


async def _existing_hashes(session) -> Dict[tuple, str]:
    rows = (await session.execute(text(
        "SELECT scope_type, scope_key, input_hash FROM memory_digests"
    ))).all()
    return {(r.scope_type, r.scope_key): r.input_hash for r in rows}


async def _upsert(session, scope_type: str, scope_key: str, agg: Dict[str, Any],
                  digest_text: str, input_hash: str) -> None:
    stmt = pg_insert(MemoryDigest.__table__).values(
        scope_type=scope_type, scope_key=scope_key, digest_text=digest_text,
        net_tilt=agg.get("net_tilt"), event_count=agg.get("event_count", 0),
        source_window_days=agg.get("window_days"), input_hash=input_hash,
        model_used=MEMORY_DIGEST_MODEL, generated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["scope_type", "scope_key"],
        set_={
            "digest_text": stmt.excluded.digest_text,
            "net_tilt": stmt.excluded.net_tilt,
            "event_count": stmt.excluded.event_count,
            "source_window_days": stmt.excluded.source_window_days,
            "input_hash": stmt.excluded.input_hash,
            "model_used": stmt.excluded.model_used,
            "generated_at": stmt.excluded.generated_at,
        },
    )
    await session.execute(stmt)
    await session.commit()


async def _sectors_with_data(session) -> List[str]:
    rows = (await session.execute(text("""
        SELECT s AS sector, count(*) AS c
        FROM memory_items, unnest(sectors) s
        WHERE event_time >= now() - make_interval(days => :d)
        GROUP BY s HAVING count(*) >= :min
        ORDER BY c DESC
    """), {"d": SECTOR_WINDOW, "min": MEMORY_DIGEST_MIN_EVENTS})).all()
    return [r.sector for r in rows]


async def refresh_all(dry_run: bool = False) -> None:
    async with session_scope() as s:
        existing = await _existing_hashes(s)
        scopes: List[tuple] = [("market", "MARKET", "the whole Indian market")]
        for sec in await _sectors_with_data(s):
            scopes.append(("sector", sec, f"the {sec} sector"))

        built = skipped = 0
        for scope_type, scope_key, label in scopes:
            if scope_type == "market":
                agg = await build_market_memory(s)
            else:
                agg = await build_sector_memory(s, scope_key)
            if not agg.get("events"):
                continue
            h = _aggregate_hash(agg)
            if existing.get((scope_type, scope_key)) == h:
                skipped += 1
                continue
            if dry_run:
                built += 1
                continue
            digest_text = await _synthesize(label, agg)
            if not digest_text:
                continue
            await _upsert(s, scope_type, scope_key, agg, digest_text, h)
            built += 1
        logger.info(f"digests: built/updated={built} skipped(unchanged)={skipped} "
                    f"scopes={len(scopes)}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="memory_digest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("refresh")
    rp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cmd == "refresh":
        asyncio.run(refresh_all(args.dry_run))


if __name__ == "__main__":
    main()
