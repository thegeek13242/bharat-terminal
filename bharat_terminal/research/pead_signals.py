"""PEAD earnings-surprise signals — the frozen pead_v2_2026_07 recipe, live.

Validated 2026-07-04 on 21,188 events / 51 monthly cohorts (2020-2026):
composite-SUE D10−D1 +2.11% over 20 trading days (t=4.7), positive 6/7 years,
long leg (D10 vs cohort median) +2.62%. Every parameter here is FROZEN to
that study; changes require a new study_version and a re-run.

Design invariants:
- Deterministic and zero-LLM. Pass/fail comes from arithmetic on our own
  financial_results + price_points, never from model judgment.
- No look-ahead: a result's decile is ranked against the TRAILING 365-day
  distribution of stored composites, never against its own (incomplete)
  season cross-section.
- Fail-open at ingest: compute_surprise must never block results ingestion.
- Backfills never emit signals (5-day announcement recency guard).
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

STUDY_VERSION = "pead_v2.1_2026_07"

# v2.1 input hygiene (re-validated 2026-07-05: composite D10-D1 +2.18%/20d t=5.1,
# up from v2.0's +2.11%/t=4.7). Two cleanings, neither mutates stored data:
#  - winsorize SUE at ±10σ: near-dormant shells (e.g. INDBNK, ~0 revenue for
#    years then a jump) produce absurd 40-68σ scores from near-zero historical
#    variance. The percentile RANK is unaffected (monotone), but capping keeps
#    displayed values sane and stops outliers dominating the composite mean.
#  - drop non-positive revenue quarters from the revenue-SUE history: many
#    filers report only audited ANNUAL figures at year-end, so the derived
#    standalone Q4 (= FY - 9M) can be legitimately negative on a reversal/
#    restatement (e.g. HINDOILEXP -205.89 Cr). The parser correctly stores that
#    per the source-of-truth doctrine; it is simply not a valid "revenue growth"
#    input. Such an event then has no revenue-SUE → no composite → unranked
#    (matches the study, which required both legs).
SUE_WINSOR = 10.0

MIN_PRIOR_YOY_OBS = 4
ENTRY_MIN_PRICE = 5.0
MIN_TRADED_VALUE_CR = 1.0
LIQUIDITY_WINDOW_SESSIONS = 20
REFERENCE_TRAILING_DAYS = 365
REFERENCE_MIN_N = 200
SIGNAL_RECENCY_DAYS = 5

# Study leg means @20 trading days, used as implied_move_pct on tracking rows.
D10_HISTORICAL_EXCESS_20D = 2.6
D1_HISTORICAL_EXCESS_20D = 0.5

# Static customer-facing context (factual/statistical language only — RA
# posture; see plan 2026-07-04). Served verbatim by the API route.
HISTORICAL_CONTEXT = {
    "study_version": STUDY_VERSION,
    "summary": (
        "In our backtest of ~21,000 Indian quarterly results (2020–2026), "
        "companies whose combined earnings-and-revenue surprise ranked in the "
        "top decile of the trailing year went on to outperform the median "
        "result-reporting company by ~2.6% over the following 20 trading "
        "sessions on average; bottom-decile surprises underperformed."
    ),
    "disclaimer": (
        "Historical statistics from Taperead's own backtest. Past patterns "
        "may not repeat. This is factual research data, not investment "
        "advice or a recommendation to buy or sell any security."
    ),
}


def shift_quarter(d0: date, n: int) -> date:
    m = (d0.month - 1) + 3 * n
    y = d0.year + m // 12
    m = m % 12 + 1
    last = {3: 31, 6: 30, 9: 30, 12: 31}.get(m)
    return date(y, m, last) if last else d0


def compute_sue(history: Dict[date, float], period_end: date,
                min_obs: int = MIN_PRIOR_YOY_OBS) -> Optional[float]:
    """Standardized unexpected earnings: this quarter's YoY change vs the
    mean/σ of the company's own prior YoY changes (up to 12 look-backs).
    Exact port of the validated study function."""
    prev = shift_quarter(period_end, -4)
    if period_end not in history or prev not in history:
        return None
    surprise = history[period_end] - history[prev]
    prior = []
    for back in range(1, 13):
        q = shift_quarter(period_end, -back)
        q4 = shift_quarter(q, -4)
        if q in history and q4 in history:
            prior.append(history[q] - history[q4])
    if len(prior) < min_obs:
        return None
    mu = sum(prior) / len(prior)
    var = sum((x - mu) ** 2 for x in prior) / (len(prior) - 1)
    sd = math.sqrt(var)
    if sd <= 1e-9:
        return None
    z = (surprise - mu) / sd
    return max(-SUE_WINSOR, min(SUE_WINSOR, z))  # v2.1 winsorize


def pead_signal_id(symbol: str, period_end: date) -> str:
    return hashlib.sha256(f"PEAD|{symbol.upper()}|{period_end}".encode()).hexdigest()[:32]


async def _load_history(session: AsyncSession, symbol: str):
    """Quarterly eps/revenue history, consolidated-preferred (study dedupe)."""
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (period_end) period_end, eps_basic, revenue_cr
        FROM financial_results
        WHERE symbol = :sym AND period_type = 'quarterly' AND eps_basic IS NOT NULL
        ORDER BY period_end, consolidated DESC
    """), {"sym": symbol})).all()
    eps = {r.period_end: float(r.eps_basic) for r in rows}
    # v2.1: only strictly-positive revenue quarters feed the revenue-SUE — a
    # derived/reversal negative Q4 is real in the filing but not a growth input.
    rev = {r.period_end: float(r.revenue_cr) for r in rows
           if r.revenue_cr is not None and float(r.revenue_cr) > 0}
    return eps, rev


async def _liquidity(session: AsyncSession, symbol: str, as_of: date):
    row = (await session.execute(text("""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY close * coalesce(volume,0) / 1e7)
                 AS med_tv,
               max(close) FILTER (WHERE date = sub.max_date) AS last_close
        FROM (
            SELECT close, volume, date, max(date) OVER () AS max_date
            FROM price_points
            WHERE symbol = :sym AND date < :asof
            ORDER BY date DESC LIMIT :n
        ) sub
    """), {"sym": symbol, "asof": as_of, "n": LIQUIDITY_WINDOW_SESSIONS})).first()
    med_tv = float(row.med_tv) if row and row.med_tv is not None else None
    last_close = float(row.last_close) if row and row.last_close is not None else None
    ok = (med_tv is not None and med_tv >= MIN_TRADED_VALUE_CR
          and last_close is not None and last_close >= ENTRY_MIN_PRICE)
    return ok, med_tv


async def _percentile(session: AsyncSession, composite: float,
                      as_of: Optional[datetime]):
    """Rank vs the trailing reference distribution of stored composites.
    Strictly historical (announced_at < as_of) → no intra-season look-ahead."""
    anchor = as_of or datetime.now(timezone.utc)
    row = (await session.execute(text("""
        SELECT count(*) AS n,
               count(*) FILTER (WHERE sue_composite < :v) AS below
        FROM earnings_surprises
        WHERE sue_composite IS NOT NULL
          AND announced_at IS NOT NULL
          AND announced_at < :anchor
          AND announced_at >= :anchor - interval '365 days'
    """), {"v": composite, "anchor": anchor})).first()
    n, below = int(row.n), int(row.below)
    if n < REFERENCE_MIN_N:
        row = (await session.execute(text("""
            SELECT count(*) AS n, count(*) FILTER (WHERE sue_composite < :v) AS below
            FROM earnings_surprises
            WHERE sue_composite IS NOT NULL AND announced_at < :anchor
        """), {"v": composite, "anchor": anchor})).first()
        n, below = int(row.n), int(row.below)
    if n < REFERENCE_MIN_N:
        return None, None, n
    pct = below / n
    decile = min(9, int(pct * 10)) + 1
    return pct, decile, n


async def compute_surprise(session: AsyncSession, symbol: str, period_end: date,
                           announced_at: Optional[datetime],
                           emit_signals: bool = True) -> Optional[dict]:
    """Compute + upsert the surprise row for one result; emit the tracking
    signal when the frozen criteria hold. FAIL-OPEN: catches everything.
    Caller owns the session/commit."""
    try:
        return await _compute_surprise_inner(session, symbol, period_end,
                                             announced_at, emit_signals)
    except Exception as e:
        logger.warning(f"[pead] {symbol} {period_end}: surprise computation failed "
                       f"(fail-open): {e}")
        return None


async def _compute_surprise_inner(session, symbol, period_end, announced_at,
                                  emit_signals):
    eps_h, rev_h = await _load_history(session, symbol)
    sue_eps = compute_sue(eps_h, period_end)
    if sue_eps is None:
        return None  # not enough own-history — no surprise row
    sue_rev = compute_sue(rev_h, period_end)
    composite = (sue_eps + sue_rev) / 2 if sue_rev is not None else None

    pct = decile = ref_n = None
    if composite is not None:
        pct, decile, ref_n = await _percentile(session, composite, announced_at)

    asof = (announced_at.date() if announced_at else date.today()) + timedelta(days=1)
    liq_ok, med_tv = await _liquidity(session, symbol, asof)

    await session.execute(text("""
        INSERT INTO earnings_surprises
            (symbol, period_end, announced_at, eps_basic, revenue_cr,
             sue_eps, sue_revenue, sue_composite, percentile, decile,
             liquidity_pass, median_traded_value_cr, reference_n, study_version)
        VALUES (:sym, :pend, :ann, :eps, :rev, :se, :sr, :sc, :pct, :dec,
                :liq, :mtv, :refn, :ver)
        ON CONFLICT ON CONSTRAINT uq_earnings_surprise DO UPDATE SET
            announced_at = COALESCE(earnings_surprises.announced_at, EXCLUDED.announced_at),
            eps_basic = EXCLUDED.eps_basic, revenue_cr = EXCLUDED.revenue_cr,
            sue_eps = EXCLUDED.sue_eps, sue_revenue = EXCLUDED.sue_revenue,
            sue_composite = EXCLUDED.sue_composite,
            percentile = EXCLUDED.percentile, decile = EXCLUDED.decile,
            liquidity_pass = EXCLUDED.liquidity_pass,
            median_traded_value_cr = EXCLUDED.median_traded_value_cr,
            reference_n = EXCLUDED.reference_n,
            study_version = EXCLUDED.study_version, updated_at = now()
    """), {"sym": symbol, "pend": period_end, "ann": announced_at,
           "eps": eps_h.get(period_end), "rev": rev_h.get(period_end),
           "se": sue_eps, "sr": sue_rev, "sc": composite, "pct": pct,
           "dec": decile, "liq": liq_ok, "mtv": med_tv, "refn": ref_n,
           "ver": STUDY_VERSION})

    out = {"symbol": symbol, "period_end": period_end, "sue_eps": sue_eps,
           "sue_revenue": sue_rev, "sue_composite": composite,
           "decile": decile, "liquidity_pass": liq_ok, "signal": None}

    # Signal emission — frozen criteria + recency guard (backfills/sweeps of
    # old results must never create stale "live" signals).
    recent = (announced_at is not None
              and (datetime.now(timezone.utc) - announced_at)
              <= timedelta(days=SIGNAL_RECENCY_DAYS))
    if emit_signals and recent and liq_ok and decile in (1, 10):
        direction = "long" if decile == 10 else "short"
        conviction = "high" if decile == 10 else "medium"
        implied = D10_HISTORICAL_EXCESS_20D if decile == 10 else D1_HISTORICAL_EXCESS_20D
        await session.execute(text("""
            INSERT INTO signal_outcomes
                (signal_id, impact_report_id, news_id, symbol, direction,
                 conviction, instrument_type, source, signal_created_at,
                 implied_move_pct, eval_status)
            VALUES (:sid, NULL, NULL, :sym, :dir, :conv, 'equity', 'PEAD_SUE',
                    :ts, :imp, 'pending')
            ON CONFLICT (signal_id) DO NOTHING
        """), {"sid": pead_signal_id(symbol, period_end), "sym": symbol,
               "dir": direction, "conv": conviction,
               "ts": announced_at, "imp": implied})
        await session.execute(text(
            "UPDATE earnings_surprises SET signal_emitted = true "
            "WHERE symbol = :sym AND period_end = :pend"
        ), {"sym": symbol, "pend": period_end})
        out["signal"] = direction
        logger.info(f"[pead] SIGNAL {symbol} {period_end}: decile={decile} "
                    f"{direction}/{conviction} composite={composite:.2f}")
    return out


async def backfill(dry_run: bool = True) -> dict:
    """One-time historical population. Step 1: announced_at from nse_filings.
    Step 2: walk all results chronologically computing surprises (no signal
    emission — recency guard). Chronological order preserves the integrity of
    the trailing reference distribution."""
    from bharat_terminal.kb.sync.utils import create_async_session

    SessionFactory = create_async_session()
    stats = {"announced_at_backfilled": 0, "candidates": 0, "surprises": 0,
             "d10": 0, "d1": 0}
    async with SessionFactory() as session:
        res = await session.execute(text("""
            UPDATE financial_results fr
            SET announced_at = COALESCE(nf.broadcast_date, nf.filing_date)
            FROM nse_filings nf
            WHERE nf.id = fr.nse_filing_id AND fr.announced_at IS NULL
              AND COALESCE(nf.broadcast_date, nf.filing_date) IS NOT NULL
        """))
        stats["announced_at_backfilled"] = res.rowcount

        rows = (await session.execute(text("""
            SELECT DISTINCT ON (symbol, period_end) symbol, period_end, announced_at
            FROM financial_results
            WHERE period_type = 'quarterly' AND eps_basic IS NOT NULL
              AND announced_at IS NOT NULL
            ORDER BY symbol, period_end, consolidated DESC
        """))).all()
        rows = sorted(rows, key=lambda r: r.announced_at)
        stats["candidates"] = len(rows)

        for i, r in enumerate(rows):
            out = await compute_surprise(session, r.symbol, r.period_end,
                                         r.announced_at, emit_signals=False)
            if out and out["sue_composite"] is not None:
                stats["surprises"] += 1
                if out["decile"] == 10:
                    stats["d10"] += 1
                elif out["decile"] == 1:
                    stats["d1"] += 1
            if not dry_run and i % 500 == 499:
                await session.commit()
                logger.info(f"[pead backfill] {i + 1}/{len(rows)}: {stats}")

        if dry_run:
            await session.rollback()
            logger.info(f"[pead backfill] DRY RUN — rolled back: {stats}")
        else:
            await session.commit()
            logger.info(f"[pead backfill] DONE: {stats}")
    return stats
