"""Daily signal-report endpoints.

Signals generated after the 15:30 IST close are actionable on the next trading
day. These endpoints serve one comprehensive report per trading day.

  GET /signals/daily-report            -> current actionable trading day
  GET /signals/daily-report/{date}     -> report for the trading day on/after {date}
  GET /signals/report-dates            -> trading days that already have a stored report

Past days are served from the persisted signal_reports table (generated on first
request). The current actionable day is always recomputed live, since more
signals land through the session.
"""
import logging
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from bharat_terminal.api.db import get_session_factory
from bharat_terminal.research import signal_report as sr

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/signals", tags=["signals"])


async def _report_for(requested: date, refresh: bool) -> dict:
    factory = get_session_factory()
    async with factory() as s:
        trading_day = await sr.resolve_trading_day(s, requested)
        current = await sr.current_actionable_trading_day(s)
        # The live day changes through the session — always recompute it. Force
        # recompute when explicitly asked. Otherwise serve the cached snapshot.
        if refresh or trading_day >= current:
            return await sr.generate(s, trading_day, persist=True)
        return await sr.get_or_generate(s, trading_day)


@router.get("/daily-report")
async def daily_report_latest(refresh: bool = Query(default=False)):
    """Report for the current actionable trading day (live)."""
    factory = get_session_factory()
    async with factory() as s:
        day = await sr.current_actionable_trading_day(s)
    return await _report_for(day, refresh=True)


@router.get("/daily-report/{day}")
async def daily_report_for_date(day: str, refresh: bool = Query(default=False)):
    """Report for the trading day on/after `day` (YYYY-MM-DD)."""
    try:
        requested = date.fromisoformat(day)
    except ValueError:
        raise HTTPException(400, "day must be ISO date YYYY-MM-DD")
    return await _report_for(requested, refresh=refresh)


@router.get("/report-dates")
async def report_dates(limit: int = Query(default=120, ge=1, le=1000)):
    """Trading days that already have a stored report, newest first."""
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(text(
            "SELECT trading_day, n_signals, n_long, n_short, n_high_conviction, generated_at "
            "FROM signal_reports ORDER BY trading_day DESC LIMIT :lim"
        ), {"lim": limit})).mappings().all()
    return {"dates": [dict(r) for r in rows]}
