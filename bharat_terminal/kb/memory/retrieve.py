"""Read-side retrieval over the `memory_items` index.

Scored assistant-style: for a given scope (company / sector / market) and the
current news context, rank events by

    score = w_rel · relevance + w_rec · recency + w_imp · importance

where `relevance` blends semantic similarity (pgvector cosine, HNSW ANN) with a
structured boost for shared macro_theme / sector, `recency` is exp-decay on the
anchored event_time, and `importance` is the magnitude. Falls back to
recency+importance (+structured) when no query embedding is supplied
(MEMORY_SEMANTIC_ENABLED=0 or embedding failure) — so retrieval never hard-depends
on the embedding service.

Returns the top-k events plus a window-level `net_tilt` and `by_theme` breakdown
(computed over a broader recent scan than the top-k, so the tilt reflects breadth).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from bharat_terminal.kb.memory import core

SCAN_LIMIT = int(os.getenv("MEMORY_SCAN_LIMIT", "300"))
ANN_CANDIDATES = int(os.getenv("MEMORY_ANN_CANDIDATES", "80"))


def _weights() -> tuple[float, float, float]:
    try:
        w = [float(x) for x in os.getenv("MEMORY_RELEVANCE_WEIGHTS", "0.5,0.3,0.2").split(",")]
        return w[0], w[1], w[2]
    except Exception:
        return 0.5, 0.3, 0.2


def _scope_clause(scope_type: str, scope_key: str) -> tuple[str, Dict[str, Any]]:
    """Index-friendly scope filter (GIN @> for arrays)."""
    if scope_type == "company":
        return "symbols @> ARRAY[:key]::text[]", {"key": scope_key.upper()}
    if scope_type == "sector":
        return "sectors @> ARRAY[:key]::text[]", {"key": scope_key}
    return "TRUE", {}  # market


async def retrieve_memory(
    session: AsyncSession,
    scope_type: str,
    scope_key: str,
    *,
    window_days: int,
    top_k: int,
    half_life: float,
    query_embedding: Optional[Sequence[float]] = None,
    query_theme: Optional[str] = None,
    query_sectors: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=window_days)
    where, params = _scope_clause(scope_type, scope_key)
    params["since"] = since

    # ── candidate fetch (ANN when we have an embedding, else recency) ─────────
    cols = ("event_key, summary, sentiment, magnitude, time_horizon, macro_theme, "
            "symbols, sectors, event_time, importance, source_count")
    if query_embedding is not None:
        params["q"] = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
        params["lim"] = ANN_CANDIDATES
        cand_sql = text(f"""
            SELECT {cols}, (1 - (embedding <=> (:q)::vector)) AS relevance
            FROM memory_items
            WHERE {where} AND event_time >= :since AND embedding IS NOT NULL
            ORDER BY embedding <=> (:q)::vector
            LIMIT :lim
        """)
    else:
        params["lim"] = ANN_CANDIDATES
        cand_sql = text(f"""
            SELECT {cols}, NULL::float AS relevance
            FROM memory_items
            WHERE {where} AND event_time >= :since
            ORDER BY event_time DESC
            LIMIT :lim
        """)
    candidates = (await session.execute(cand_sql, params)).all()

    # ── window scan for net_tilt + by_theme (broader than the top-k) ─────────
    agg_params = {k: v for k, v in params.items() if k in ("key", "since")}
    agg_params["scan"] = SCAN_LIMIT
    agg_sql = text(f"""
        SELECT sentiment, magnitude, macro_theme, event_time
        FROM memory_items
        WHERE {where} AND event_time >= :since
        ORDER BY event_time DESC
        LIMIT :scan
    """)
    agg = (await session.execute(agg_sql, agg_params)).all()

    now = datetime.now(timezone.utc)
    w_rel, w_rec, w_imp = _weights()
    qsectors = {s for s in (query_sectors or [])}

    # ── score + rank candidates ──────────────────────────────────────────────
    scored: List[tuple] = []
    for r in candidates:
        age = max(0.0, (now - r.event_time).total_seconds() / 86400.0)
        rec = core.recency_decay(age, half_life)
        imp = min(1.0, (r.importance or 0) / 5.0)
        struct = 0.0
        if query_theme and r.macro_theme == query_theme:
            struct += 0.5
        if qsectors and (set(r.sectors or []) & qsectors):
            struct += 0.5
        struct = min(1.0, struct)
        if r.relevance is not None:
            rel = min(1.0, 0.7 * float(r.relevance) + 0.3 * struct)
        else:
            rel = struct
        score = w_rel * rel + w_rec * rec + w_imp * imp
        scored.append((score, age, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    items: List[Dict[str, Any]] = []
    for _score, age, r in scored[:top_k]:
        items.append({
            "date": r.event_time.date().isoformat(),
            "days_ago": round(age, 1),
            "sentiment": r.sentiment,
            "magnitude": r.magnitude,
            "time_horizon": r.time_horizon,
            "macro_theme": r.macro_theme,
            "summary": r.summary,
            "source_count": r.source_count,
        })
    items.sort(key=lambda e: e["days_ago"])  # surface most-recent-first

    # ── net tilt + by_theme over the scan (scale-invariant lean ratio) ───────
    net_signed = 0.0
    net_weight = 0.0
    by_theme: Dict[str, Dict[str, float]] = {}
    for r in agg:
        age = max(0.0, (now - r.event_time).total_seconds() / 86400.0)
        weight = (r.magnitude or 0) * core.recency_decay(age, half_life)
        signed = core._SIGN.get(r.sentiment, 0) * weight
        net_signed += signed
        net_weight += weight
        bt = by_theme.setdefault(r.macro_theme or "OTHER",
                                 {"count": 0, "signed": 0.0, "weight": 0.0})
        bt["count"] += 1
        bt["signed"] += signed
        bt["weight"] += weight

    def _ratio(sgn: float, wt: float) -> float:
        return sgn / wt if wt > 0 else 0.0

    by_theme_out = sorted(
        [{"theme": t, "count": int(v["count"]),
          "tilt": core.tilt_from_ratio(_ratio(v["signed"], v["weight"]))}
         for t, v in by_theme.items()],
        key=lambda x: x["count"], reverse=True,
    )[:6]

    return {
        "window_days": window_days,
        "event_count": len(agg),
        "net_tilt": core.tilt_from_ratio(_ratio(net_signed, net_weight)),
        "by_theme": by_theme_out,
        "events": items,
    }
