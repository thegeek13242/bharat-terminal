"""Build / refresh / prune the derived `memory_items` index.

The index is a rebuildable materialisation of `impact_reports`:
  - `rebuild(full=True)`  → recompute the whole retention window, embed every event.
    Idempotent and deterministic (modulo embedding noise) — the drift-free
    reconciliation path and the one-time backfill.
  - `refresh(days=N)`     → recompute only the recent window and upsert; reuses the
    stored embedding for unchanged events so each cycle embeds only what's new.
  - `prune()`             → delete events past the retention window.

Consolidation happens in `core.consolidate`; embeddings via `llm.embeddings`.
Run as:  python -m bharat_terminal.kb.memory.index {rebuild|refresh|prune} [--days N]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bharat_terminal.research.db import session_scope
from bharat_terminal.kb.models import MemoryItem
from bharat_terminal.kb.memory import core
from bharat_terminal.llm.embeddings import embed_texts, EMBEDDING_COLUMN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("memory_index")

# Small incremental window (with overlap) so late-arriving duplicate coverage of a
# recent event still merges, while old events are left untouched.
REFRESH_DAYS = int(os.getenv("MEMORY_INDEX_REFRESH_DAYS", "5"))


async def _fetch_events(since: datetime) -> List[Dict[str, Any]]:
    async with session_scope() as s:
        rows = (await s.execute(core.EXTRACT_SQL, {
            "since": since, "min_mag": core.INDEX_MIN_MAGNITUDE,
        })).all()
    events = core.consolidate(rows)
    logger.info(f"extracted {len(rows)} direct-impact rows → {len(events)} "
                f"consolidated events (since {since.date()})")
    return events


async def _existing_signatures(event_keys: List[str]) -> Dict[str, Any]:
    """Map event_key → (summary, magnitude, has_embedding) for embedding reuse."""
    if not event_keys:
        return {}
    async with session_scope() as s:
        rows = (await s.execute(text(f"""
            SELECT event_key, summary, magnitude, ({EMBEDDING_COLUMN} IS NOT NULL) AS has_emb
            FROM memory_items WHERE event_key = ANY(:keys)
        """), {"keys": event_keys})).all()
    return {r.event_key: (r.summary, r.magnitude, r.has_emb) for r in rows}


async def _upsert(events: List[Dict[str, Any]], embeddings: Dict[str, List[float]]) -> None:
    if not events:
        return
    values = []
    for ev in events:
        values.append({
            "event_key": ev["event_key"],
            "summary": ev["summary"],
            "sentiment": ev["sentiment"],
            "magnitude": ev["magnitude"],
            "time_horizon": ev["time_horizon"],
            "macro_theme": ev["macro_theme"],
            "symbols": ev["symbols"],
            "sectors": ev["sectors"],
            "event_time": ev["event_time"],
            "importance": ev["importance"],
            "source_news_ids": ev["source_news_ids"],
            "source_count": ev["source_count"],
            # None → keep existing on conflict. Written to the ACTIVE column
            # (embedding vs embedding_1024) for the configured provider.
            EMBEDDING_COLUMN: embeddings.get(ev["event_key"]),
        })
    async with session_scope() as s:
        # Chunk to keep statements a sane size.
        for i in range(0, len(values), 500):
            chunk = values[i:i + 500]
            stmt = pg_insert(MemoryItem.__table__).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["event_key"],
                set_={
                    "summary": stmt.excluded.summary,
                    "sentiment": stmt.excluded.sentiment,
                    "magnitude": stmt.excluded.magnitude,
                    "time_horizon": stmt.excluded.time_horizon,
                    "macro_theme": stmt.excluded.macro_theme,
                    "symbols": stmt.excluded.symbols,
                    "sectors": stmt.excluded.sectors,
                    "event_time": stmt.excluded.event_time,
                    "importance": stmt.excluded.importance,
                    "source_news_ids": stmt.excluded.source_news_ids,
                    "source_count": stmt.excluded.source_count,
                    # Only overwrite the embedding when we computed a fresh one.
                    EMBEDDING_COLUMN: func.coalesce(
                        getattr(stmt.excluded, EMBEDDING_COLUMN),
                        MemoryItem.__table__.c[EMBEDDING_COLUMN],
                    ),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
            await s.execute(stmt)
        await s.commit()
    logger.info(f"upserted {len(values)} memory_items")


async def _run(since: datetime, embed_all: bool) -> None:
    events = await _fetch_events(since)
    if not events:
        return
    sigs = {} if embed_all else await _existing_signatures([e["event_key"] for e in events])

    to_embed = []
    for ev in events:
        sig = sigs.get(ev["event_key"])
        unchanged = (
            sig is not None and sig[0] == ev["summary"]
            and sig[1] == ev["magnitude"] and sig[2]
        )
        if not unchanged:
            to_embed.append(ev)

    embeddings: Dict[str, List[float]] = {}
    if to_embed:
        logger.info(f"embedding {len(to_embed)}/{len(events)} events "
                    f"(reusing {len(events) - len(to_embed)} stored)")
        vecs = await embed_texts([e["embed_text"] for e in to_embed])
        embeddings = {e["event_key"]: v for e, v in zip(to_embed, vecs)}

    await _upsert(events, embeddings)


async def rebuild() -> None:
    since = datetime.now(timezone.utc) - timedelta(days=core.RETENTION_DAYS)
    await _run(since, embed_all=True)
    await prune()


async def refresh(days: int = REFRESH_DAYS) -> None:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    await _run(since, embed_all=False)


async def prune() -> None:
    cutoff_days = core.RETENTION_DAYS
    async with session_scope() as s:
        res = await s.execute(text(
            "DELETE FROM memory_items WHERE event_time < now() - make_interval(days => :d)"
        ), {"d": cutoff_days})
        await s.commit()
    logger.info(f"pruned {res.rowcount} events older than {cutoff_days}d")


def main() -> None:
    ap = argparse.ArgumentParser(prog="memory_index")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild")
    rp = sub.add_parser("refresh")
    rp.add_argument("--days", type=int, default=REFRESH_DAYS)
    sub.add_parser("prune")
    args = ap.parse_args()
    if args.cmd == "rebuild":
        asyncio.run(rebuild())
    elif args.cmd == "refresh":
        asyncio.run(refresh(args.days))
    elif args.cmd == "prune":
        asyncio.run(prune())


if __name__ == "__main__":
    main()
