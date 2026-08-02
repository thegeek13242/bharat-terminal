"""Earnings-surprise (PEAD) events — customer-facing, factual-statistical.

RA posture (founder decision 2026-07-04, flagged for counsel review): the
surprise decile is a computed statistic against the company's own history,
presented with backtest context and a persistent disclaimer. NO buy/sell,
target, or recommendation language anywhere in this payload — additions must
preserve that. LLM trade signals remain internal-only; this route serves only
deterministic earnings_surprises rows.
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from bharat_terminal.api.auth import INTERNAL_RESEARCH_EMAILS, get_current_user
from bharat_terminal.api.db import get_session_factory
from bharat_terminal.research.pead_signals import HISTORICAL_CONTEXT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/earnings-surprises", tags=["earnings-surprises"])

# A tail surprise whose first-session price move runs ≥ this far against the
# surprise direction gets price_diverged=true. The market's immediate verdict
# is the strongest drift conditioner in the PEAD literature; suppressing it
# turned BAJAJCON Q1FY27 (top decile, then −10.6% on day one) into a green
# badge on a falling stock.
DIVERGENCE_MIN_MOVE_PCT = 2.0

# First-session close-to-close reaction, computed at read time from
# price_points (never stored — the frozen pead study recipe is untouched).
# The "public day" is the announcement's IST date, rolled to the next day for
# post-close (≥15:30 IST) filings. announced_at carries the XBRL timestamp,
# which can lag the press release by a session — the baseline close may
# therefore understate the reaction, never overstate it.
_REACTION_SQL = """
    CROSS JOIN LATERAL (
        SELECT CASE
            WHEN (es.announced_at AT TIME ZONE 'Asia/Kolkata')::time >= time '15:30'
            THEN (es.announced_at AT TIME ZONE 'Asia/Kolkata')::date + 1
            ELSE (es.announced_at AT TIME ZONE 'Asia/Kolkata')::date
        END AS public_day
    ) pd
    LEFT JOIN LATERAL (
        SELECT close FROM price_points
        WHERE symbol = es.symbol AND date < pd.public_day
        ORDER BY date DESC LIMIT 1
    ) base ON true
    LEFT JOIN LATERAL (
        SELECT close, date FROM price_points
        WHERE symbol = es.symbol AND date >= pd.public_day
        ORDER BY date ASC LIMIT 1
    ) react ON true
"""

_REACTION_COLS = """,
       round(((react.close / nullif(base.close, 0) - 1) * 100)::numeric, 2)
           AS reaction_return_pct,
       react.date AS reaction_date,
       base.close AS reaction_base_close,
       pd.public_day AS reaction_public_day"""

KNOWLEDGE_BASE_URL = os.getenv("KNOWLEDGE_BASE_URL", "http://kb-service:8001")
LIVE_REACTION_MAX_SYMBOLS = 16
IST = ZoneInfo("Asia/Kolkata")


async def _live_reactions(rows) -> dict:
    """Provisional reaction from live LTP for rows whose first post-announcement
    close hasn't landed yet (reaction session underway, or closed but the 19:00
    IST bhavcopy sync hasn't run). Returns {symbol: {"pct", "asof"}}.

    LICENSING (Angel One personal-use): callers must gate this on
    INTERNAL_RESEARCH_EMAILS — never serve live-derived values to customers,
    never store them. Fail-open: any problem returns {} and the UI keeps its
    pending state.
    """
    today_ist = datetime.now(IST).date()
    pending = {
        r.symbol: float(r.reaction_base_close)
        for r in rows
        if r.reaction_return_pct is None
        and r.reaction_base_close is not None
        and r.reaction_public_day is not None
        and r.reaction_public_day <= today_ist
    }
    if not pending:
        return {}
    syms = list(pending)[:LIVE_REACTION_MAX_SYMBOLS]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{KNOWLEDGE_BASE_URL}/ltp",
                                    params={"symbols": ",".join(syms)})
        if resp.status_code != 200:
            return {}
        data = resp.json()
        if data.get("source") != "angelone_live":
            return {}
        out = {}
        for sym, quote in (data.get("quotes") or {}).items():
            base = pending.get(sym)
            ltp = quote.get("ltp")
            prev_close = quote.get("prev_close")
            # The quote only reflects the reaction session once Angel One's
            # prev_close equals our baseline close. Before that session first
            # trades (e.g. Saturday after a Friday-evening filing) the LTP
            # predates the announcement — a spurious "live 0.0%".
            if not (base and ltp and prev_close):
                continue
            if abs(float(prev_close) / base - 1) > 0.001:
                continue
            out[sym] = {"pct": round((float(ltp) / base - 1) * 100, 2),
                        "asof": data.get("asof")}
        return out
    except Exception as e:
        logger.debug(f"[surprises] live reaction unavailable (fail-open): {e}")
        return {}


def _diverged(decile, reaction_pct) -> bool | None:
    """Factual price-vs-statistic relationship; defined only for tail deciles
    with at least one completed session since announcement."""
    if decile not in (1, 10) or reaction_pct is None:
        return None
    if decile == 10:
        return reaction_pct <= -DIVERGENCE_MIN_MOVE_PCT
    return reaction_pct >= DIVERGENCE_MIN_MOVE_PCT


def _row(r, live: dict | None = None) -> dict:
    reaction = float(r.reaction_return_pct) if r.reaction_return_pct is not None else None
    basis = "close" if reaction is not None else None
    reaction_asof = None
    if reaction is None and live and r.symbol in live:
        reaction = live[r.symbol]["pct"]
        basis = "live"
        reaction_asof = live[r.symbol]["asof"]
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
        "reaction_return_pct": reaction,
        "reaction_basis": basis,
        "reaction_asof": reaction_asof,
        "reaction_date": (r.reaction_date.isoformat()
                          if basis == "close" and r.reaction_date else None),
        "price_diverged": _diverged(r.decile, reaction),
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
                   {_REACTION_COLS}
            FROM earnings_surprises es
            JOIN companies c ON c.symbol = es.symbol
            {_REACTION_SQL}
            WHERE es.announced_at >= now() - make_interval(days => :days)
              AND es.sue_composite IS NOT NULL
              {decile_clause}
              {liq_clause}
            ORDER BY es.announced_at DESC
            LIMIT 200
        """), {"days": days})).all()
    # Live provisional reactions are internal-only (Angel One licensing —
    # see _live_reactions). Customers see basis=null → "pending".
    # Role is DB-backed (get_current_user); env allowlist = bootstrap fallback.
    is_internal = (
        user.get("role") in ("admin", "internal")
        or (user.get("email") or "").lower() in INTERNAL_RESEARCH_EMAILS
    )
    live = await _live_reactions(rows) if is_internal else {}
    return {
        "items": [_row(r, live) for r in rows],
        "count": len(rows),
        "historical_context": HISTORICAL_CONTEXT,
    }


@router.get("/{symbol}")
async def surprise_history(symbol: str, user: dict = Depends(get_current_user)):
    """Per-company surprise history (newest first)."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        rows = (await session.execute(text(f"""
            SELECT es.symbol, c.company_name, es.period_end, es.announced_at,
                   es.eps_basic, es.revenue_cr, es.sue_eps, es.sue_revenue,
                   es.sue_composite, es.percentile, es.decile,
                   es.liquidity_pass, es.study_version
                   {_REACTION_COLS}
            FROM earnings_surprises es
            JOIN companies c ON c.symbol = es.symbol
            {_REACTION_SQL}
            WHERE es.symbol = :sym
            ORDER BY es.period_end DESC
            LIMIT 40
        """), {"sym": symbol.upper()})).all()
    return {
        "items": [_row(r) for r in rows],
        "count": len(rows),
        "historical_context": HISTORICAL_CONTEXT,
    }
