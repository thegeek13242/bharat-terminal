"""Signal-accuracy REST endpoints (Feature 2 — measure + flag).

Read-only views over `signal_outcomes` (populated by research/score_signals.py):
  GET /signal-accuracy/summary?horizon=5d&since=  -> hit-rate sliced by dimension
  GET /signal-accuracy/failures?category=&limit=  -> categorized LLM post-mortems

Hit-rate excludes inconclusive (dead-band) outcomes: denominator = signals with a
non-NULL correct_<h> flag; numerator = those that were TRUE.
"""
import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/signal-accuracy", tags=["signal-accuracy"])

_HORIZONS = ("1d", "5d", "10d", "20d")
_DIMENSIONS = ("conviction", "macro_theme", "sector", "source", "trigger_id")


@router.get("/summary")
async def summary(
    horizon: str = Query(default="5d"),
    since: str | None = Query(default=None, description="ISO date; only signals on/after"),
):
    """Hit-rate + mean magnitude error per slice, for a given horizon."""
    if horizon not in _HORIZONS:
        raise HTTPException(400, f"horizon must be one of {list(_HORIZONS)}")
    cc = f"correct_{horizon}"
    rc = f"ret_{horizon}"
    where = "WHERE 1=1"
    params: dict = {}
    if since:
        where += " AND signal_created_at >= :since"
        params["since"] = since

    out: dict = {"horizon": horizon, "since": since, "slices": {}}
    factory = get_session_factory()
    async with factory() as s:
        overall = (await s.execute(text(f"""
            SELECT count(*) total,
                   count(*) FILTER (WHERE {cc} IS NOT NULL) evaluated,
                   round(100.0*count(*) FILTER (WHERE {cc} IS TRUE)
                         / NULLIF(count(*) FILTER (WHERE {cc} IS NOT NULL),0),1) hit_rate_pct,
                   round(avg(abs({rc}) - implied_move_pct) FILTER (WHERE {rc} IS NOT NULL)::numeric,2) mean_magnitude_error_pct
            FROM signal_outcomes {where}
        """), params)).mappings().one()
        out["overall"] = dict(overall)

        for dim in _DIMENSIONS:
            rows = (await s.execute(text(f"""
                SELECT {dim} AS key,
                       count(*) FILTER (WHERE {cc} IS NOT NULL) evaluated,
                       round(100.0*count(*) FILTER (WHERE {cc} IS TRUE)
                             / NULLIF(count(*) FILTER (WHERE {cc} IS NOT NULL),0),1) hit_rate_pct
                FROM signal_outcomes {where}
                GROUP BY {dim} HAVING count(*) FILTER (WHERE {cc} IS NOT NULL) > 0
                ORDER BY evaluated DESC
            """), params)).mappings().all()
            out["slices"][dim] = [dict(r) for r in rows]
    return out


@router.get("/failures")
async def failures(
    category: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
):
    """Categorized post-mortems of wrong signals for human review."""
    where = "WHERE failure_category IS NOT NULL"
    params: dict = {"lim": limit}
    if category:
        where += " AND failure_category = :cat"
        params["cat"] = category
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(text(f"""
            SELECT signal_id, symbol, direction, conviction, macro_theme, sector, source,
                   signal_created_at, ret_5d, failure_category, failure_rationale
            FROM signal_outcomes {where}
            ORDER BY signal_created_at DESC LIMIT :lim
        """), params)).mappings().all()
        counts = (await s.execute(text("""
            SELECT failure_category, count(*) n FROM signal_outcomes
            WHERE failure_category IS NOT NULL GROUP BY failure_category ORDER BY n DESC
        """))).mappings().all()
    return {"counts": [dict(c) for c in counts], "failures": [dict(r) for r in rows]}
