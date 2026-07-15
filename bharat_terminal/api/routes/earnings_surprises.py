"""Earnings-surprise (PEAD) events — customer-facing, factual-statistical.

RA posture (founder decision 2026-07-04, flagged for counsel review): the
surprise decile is a computed statistic against the company's own history,
presented with backtest context and a persistent disclaimer. NO buy/sell,
target, or recommendation language anywhere in this payload — additions must
preserve that. LLM trade signals remain internal-only; this route serves only
deterministic earnings_surprises rows.
"""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from bharat_terminal.api.auth import get_current_user
from bharat_terminal.api.db import get_session_factory
from bharat_terminal.research.pead_signals import HISTORICAL_CONTEXT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/earnings-surprises", tags=["earnings-surprises"])


def _row(r) -> dict:
    return {
        "symbol": r.symbol,
        "company_name": r.company_name,
        "period_end": r.period_end.isoformat(),
        "announced_at": r.announced_at.isoformat() if r.announced_at else None,
        "eps_basic": r.eps_basic,
        "revenue_cr": r.revenue_cr,
        "sue_eps": r.sue_eps,
        "sue_revenue": r.sue_revenue,
        "sue_composite": r.sue_composite,
        "percentile": r.percentile,
        "decile": r.decile,
        "liquidity_pass": r.liquidity_pass,
        "study_version": r.study_version,
    }


@router.get("/recent")
async def recent_surprises(
    days: int = Query(7, ge=1, le=90),
    decile: str = Query("tails", pattern="^(tails|all)$"),
    include_illiquid: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    """Recent surprise events. decile=tails (default) returns only the
    top/bottom decile — the classes the backtest found informative. Illiquid
    names (below the ₹1 Cr/day screen that the study REQUIRED) are excluded by
    default; the validated edge does not apply to them."""
    session_factory = get_session_factory()
    decile_clause = "AND es.decile IN (1, 10)" if decile == "tails" else ""
    liq_clause = "" if include_illiquid else "AND es.liquidity_pass"
    async with session_factory() as session:
        rows = (await session.execute(text(f"""
            SELECT es.symbol, c.company_name, es.period_end, es.announced_at,
                   es.eps_basic, es.revenue_cr, es.sue_eps, es.sue_revenue,
                   es.sue_composite, es.percentile, es.decile,
                   es.liquidity_pass, es.study_version
            FROM earnings_surprises es
            JOIN companies c ON c.symbol = es.symbol
            WHERE es.announced_at >= now() - make_interval(days => :days)
              AND es.sue_composite IS NOT NULL
              {decile_clause}
              {liq_clause}
            ORDER BY es.announced_at DESC
            LIMIT 200
        """), {"days": days})).all()
    return {
        "items": [_row(r) for r in rows],
        "count": len(rows),
        "historical_context": HISTORICAL_CONTEXT,
    }


@router.get("/{symbol}")
async def surprise_history(symbol: str, user: dict = Depends(get_current_user)):
    """Per-company surprise history (newest first)."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (await session.execute(text("""
            SELECT es.symbol, c.company_name, es.period_end, es.announced_at,
                   es.eps_basic, es.revenue_cr, es.sue_eps, es.sue_revenue,
                   es.sue_composite, es.percentile, es.decile,
                   es.liquidity_pass, es.study_version
            FROM earnings_surprises es
            JOIN companies c ON c.symbol = es.symbol
            WHERE es.symbol = :sym
            ORDER BY es.period_end DESC
            LIMIT 40
        """), {"sym": symbol.upper()})).all()
    return {
        "items": [_row(r) for r in rows],
        "count": len(rows),
        "historical_context": HISTORICAL_CONTEXT,
    }
