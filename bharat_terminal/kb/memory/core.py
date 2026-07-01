"""Shared memory primitives + the write-side extraction/consolidation that turns
`impact_reports` into consolidated events for the `memory_items` index.

Design carried over (and refined) from the original per-company memory:
  - Event-time anchor: `event_time = LEAST(news.timestamp_utc, ir.created_at)`.
    Postgres LEAST ignores NULLs and picks the earlier value, so live news anchors
    on publish time, backfilled news stays old (not artificially fresh), and a
    future-dated calendar filing (timestamp_utc in the future) correctly anchors on
    analysis time instead.
  - Cluster-level dedup: events are consolidated per news report, then near-
    duplicate coverage across outlets is merged on a deterministic event_key
    (normalised headline + top symbols + day) — so one macro event counts once,
    not once per outlet or once per impacted company.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text

# Retention of the derived index (generous; per-tier retrieval windows are tighter).
RETENTION_DAYS = int(os.getenv("MEMORY_INDEX_RETENTION_DAYS", "120"))
# Only direct, material impacts become memory (drops routine magnitude-1 noise).
INDEX_MIN_MAGNITUDE = int(os.getenv("MEMORY_INDEX_MIN_MAGNITUDE", "2"))
# Default recency half-life for salience/tilt (per-tier overrides live in retrieve).
DEFAULT_HALFLIFE_DAYS = float(os.getenv("MEMORY_HALFLIFE_DAYS", "14"))

_SIGN = {"positive": 1, "negative": -1, "neutral": 0}


# ── Shared scoring / formatting helpers ──────────────────────────────────────
def recency_decay(age_days: float, half_life: float = DEFAULT_HALFLIFE_DAYS) -> float:
    return math.exp(-max(0.0, age_days) / max(1e-6, half_life))


def salience(magnitude: float, age_days: float,
             half_life: float = DEFAULT_HALFLIFE_DAYS) -> float:
    return (magnitude or 0) * recency_decay(age_days, half_life)


def tilt_from_ratio(ratio: float) -> str:
    """Directional label from a scale-invariant lean ratio in [-1, 1] — works
    equally for a company's few events and a 300-event market scan (raw summed
    nets would saturate the label on the market/sector tiers)."""
    if ratio >= 0.5:
        return "strongly positive"
    if ratio >= 0.15:
        return "slightly positive"
    if ratio <= -0.5:
        return "strongly negative"
    if ratio <= -0.15:
        return "slightly negative"
    return "neutral / mixed"


def first_clause(text_: str, limit: int = 160) -> str:
    """First sentence/clause of a string, for a compact memory line."""
    s = re.sub(r"\s+", " ", (text_ or "")).strip()
    m = re.search(r"[.;]", s)
    if m and m.start() <= limit:
        return s[: m.start() + 1]
    return s[:limit] + ("…" if len(s) > limit else "")


def embed_text_for(headline: Optional[str], macro_theme: Optional[str],
                   explanation: Optional[str]) -> str:
    """Text embedded for semantic retrieval — headline carries event identity,
    theme + strongest explanation add the causal signal tags miss."""
    parts = [p for p in (headline, macro_theme, explanation) if p]
    return re.sub(r"\s+", " ", " — ".join(parts)).strip()[:2000]


def event_key_for(headline: Optional[str], symbols: Iterable[str],
                  event_time: Optional[datetime]) -> str:
    """Deterministic identity so the same story (across outlets) collapses:
    normalised headline + up-to-3 sorted symbols + calendar day (IST-agnostic UTC
    date is fine — coverage of one event lands on the same day)."""
    norm = re.sub(r"\s+", " ", (headline or "").lower()).strip()[:120]
    sym = ",".join(sorted({s.upper() for s in symbols if s})[:3])
    day = event_time.date().isoformat() if event_time else ""
    return hashlib.sha256(f"{norm}|{sym}|{day}".encode()).hexdigest()[:32]


# ── Write-side extraction from impact_reports ────────────────────────────────
# One row per DIRECT, material company-impact, joined to its news headline and the
# anchored event time. Consolidated in Python (clearer + easy to unit-test than a
# grouping SQL with per-group "strongest impact" picks).
EXTRACT_SQL = text("""
    SELECT ir.id            AS report_id,
           ir.news_id       AS news_id,
           ir.macro_theme   AS macro_theme,
           ir.affected_sectors AS affected_sectors,
           ir.created_at    AS created_at,
           n.headline       AS headline,
           LEAST(n.timestamp_utc, ir.created_at) AS event_time,
           upper(imp->>'symbol')                 AS symbol,
           COALESCE(NULLIF(imp->>'magnitude','')::int, 0) AS magnitude,
           imp->>'sentiment'                     AS sentiment,
           imp->>'time_horizon'                  AS time_horizon,
           imp->>'explanation'                   AS explanation
    FROM impact_reports ir
    JOIN news_items n ON n.id = ir.news_id
    CROSS JOIN LATERAL jsonb_array_elements(ir.company_impacts) AS imp
    WHERE ir.relevant = true
      AND ir.created_at >= :since
      AND COALESCE(NULLIF(imp->>'hop_distance','')::int, 0) = 0
      AND COALESCE(NULLIF(imp->>'magnitude','')::int, 0) >= :min_mag
    ORDER BY event_time
""")


def consolidate(rows: List[Any]) -> List[Dict[str, Any]]:
    """Rows → consolidated events (memory_items-shaped dicts, minus the embedding).

    Level 1: group direct-impact rows by report so one news event carries all the
    symbols it touched (max-magnitude impact is the representative one).
    Level 2: merge near-duplicate report-events across outlets on event_key.
    """
    # Level 1 — group by report_id.
    by_report: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        rp = by_report.get(r.report_id)
        if rp is None:
            rp = {
                "news_id": r.news_id, "headline": r.headline,
                "macro_theme": r.macro_theme,
                "sectors": list(r.affected_sectors or []),
                "event_time": r.event_time or r.created_at,
                "symbols": [], "impacts": [],
            }
            by_report[r.report_id] = rp
        if r.symbol:
            rp["symbols"].append(r.symbol)
        rp["impacts"].append(
            (r.magnitude or 0, r.sentiment, r.time_horizon, r.explanation)
        )

    # Level 2 — merge report-events by deterministic event_key.
    events: Dict[str, Dict[str, Any]] = {}
    for rp in by_report.values():
        symbols = sorted({s for s in rp["symbols"] if s})
        key = event_key_for(rp["headline"], symbols, rp["event_time"])
        mag, sent, horizon, expl = max(rp["impacts"], key=lambda x: x[0] or 0)
        summary = first_clause(rp["headline"] or expl or "")
        ev = events.get(key)
        if ev is None:
            events[key] = {
                "event_key": key,
                "summary": summary,
                "sentiment": sent,
                "magnitude": mag,
                "time_horizon": horizon,
                "macro_theme": rp["macro_theme"],
                "symbols": set(symbols),
                "sectors": set(rp["sectors"]),
                "event_time": rp["event_time"],
                "source_news_ids": [rp["news_id"]],
                "embed_text": embed_text_for(rp["headline"], rp["macro_theme"], expl),
            }
        else:
            ev["symbols"].update(symbols)
            ev["sectors"].update(rp["sectors"])
            ev["source_news_ids"].append(rp["news_id"])
            if (mag or 0) > (ev["magnitude"] or 0):
                ev["magnitude"], ev["sentiment"], ev["time_horizon"] = mag, sent, horizon
                ev["summary"] = summary
                ev["embed_text"] = embed_text_for(rp["headline"], rp["macro_theme"], expl)
            if rp["event_time"] and (
                ev["event_time"] is None or rp["event_time"] < ev["event_time"]
            ):
                ev["event_time"] = rp["event_time"]

    out: List[Dict[str, Any]] = []
    for ev in events.values():
        ev["symbols"] = sorted(ev["symbols"])
        ev["sectors"] = sorted(ev["sectors"])
        ev["source_count"] = len(ev["source_news_ids"])
        ev["importance"] = float(ev["magnitude"] or 0)
        out.append(ev)
    return out
