"""
Per-trading-day actionable signal report.

Signals generated after the 15:30 IST close are actionable on the NEXT trading
day. The report for trading day D therefore gathers every trade signal generated
in the window (close(P), close(D)] — P = previous trading day, close(x) = x at
15:30 IST — joined to its source news, impact analysis and company context.

The trading calendar is derived from price_points (real NSE bhavcopy dates, so
weekends AND holidays are excluded). For dates beyond the latest bhavcopy we fall
back to a weekday heuristic (holiday-unaware) — only relevant for the current /
next session, which has no bhavcopy yet.

Both the API route and the CLI use the same builder (all functions take an
AsyncSession). Reports upsert idempotently into signal_reports.

CLI:
  python -m bharat_terminal.research.signal_report --latest
  python -m bharat_terminal.research.signal_report --date 2026-06-25
  python -m bharat_terminal.research.signal_report --backfill-days 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("signal_report")

IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = time(15, 30)  # NSE equity close, matches analysis/market_hours.py
HIGH_CONVICTION = "high"


# ── trading calendar (grounded in price_points) ────────────────────────────

async def _max_price_date(session: AsyncSession) -> Optional[date]:
    return (await session.execute(text("SELECT max(date) FROM price_points"))).scalar_one()


async def is_trading_day(session: AsyncSession, day: date) -> bool:
    """True if `day` is an NSE trading day.

    Authoritative for days covered by price_points (actual bhavcopy → excludes
    holidays). For future days (no bhavcopy yet) falls back to Mon–Fri.
    """
    max_d = await _max_price_date(session)
    if max_d is not None and day <= max_d:
        found = (await session.execute(
            text("SELECT 1 FROM price_points WHERE date = :d LIMIT 1"), {"d": day}
        )).first()
        return found is not None
    return day.weekday() < 5


async def prev_trading_day(session: AsyncSession, day: date) -> Optional[date]:
    """Most recent trading day strictly before `day` (always in the past →
    reliably resolved from price_points)."""
    d = (await session.execute(
        text("SELECT max(date) FROM price_points WHERE date < :d"), {"d": day}
    )).scalar_one()
    if d is not None:
        return d
    # Fallback (no price history below day): step back to previous weekday.
    c = day - timedelta(days=1)
    while c.weekday() >= 5:
        c -= timedelta(days=1)
    return c


async def next_trading_day(session: AsyncSession, day: date) -> date:
    """Smallest trading day strictly after `day`."""
    d = (await session.execute(
        text("SELECT min(date) FROM price_points WHERE date > :d"), {"d": day}
    )).scalar_one()
    if d is not None:
        return d
    c = day + timedelta(days=1)
    while c.weekday() >= 5:
        c += timedelta(days=1)
    return c


async def resolve_trading_day(session: AsyncSession, requested: date) -> date:
    """Map an arbitrary requested date to a trading day: itself if it is one,
    else the next trading day."""
    if await is_trading_day(session, requested):
        return requested
    return await next_trading_day(session, requested)


async def current_actionable_trading_day(
    session: AsyncSession, now: Optional[datetime] = None
) -> date:
    """The trading day that a signal generated *right now* would be actionable
    on: today if today is a trading session still at/before 15:30 IST, otherwise
    the next trading day."""
    now_ist = (now or datetime.now(timezone.utc)).astimezone(IST)
    today = now_ist.date()
    if await is_trading_day(session, today) and now_ist.time() <= MARKET_CLOSE:
        return today
    return await next_trading_day(session, today)


def report_window(prev_day: date, trading_day: date) -> tuple[datetime, datetime]:
    """UTC (start, end] window for signals actionable on `trading_day`:
    from the previous trading day's 15:30 IST close to this day's 15:30 close."""
    start = datetime.combine(prev_day, MARKET_CLOSE, tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(trading_day, MARKET_CLOSE, tzinfo=IST).astimezone(timezone.utc)
    return start, end


# ── comprehensive per-signal query ─────────────────────────────────────────

_SIGNALS_SQL = text("""
SELECT * FROM (
    SELECT DISTINCT ON (signal_id) *
    FROM (
        SELECT
            substr(encode(sha256((ir.id || '|' || upper(sig->>'symbol') || '|' ||
                   (sig->>'direction'))::bytea), 'hex'), 1, 32)        AS signal_id,
            ir.id                                                       AS impact_report_id,
            ir.news_id                                                  AS news_id,
            ir.created_at                                               AS generated_at,
            upper(sig->>'symbol')                                       AS symbol,
            COALESCE(c.company_name, sig->>'symbol')                    AS company_name,
            c.sector_nse                                                AS sector,
            sig->>'direction'                                           AS direction,
            sig->>'instrument_type'                                     AS instrument_type,
            sig->>'conviction'                                          AS conviction,
            (sig->>'position_size_pct_of_portfolio')::float             AS position_size_pct,
            sig->>'reasoning'                                           AS reasoning,
            sig->>'stop_loss_rationale'                                 AS stop_loss_rationale,
            sig->>'suggested_strike_rationale'                          AS suggested_strike_rationale,
            ir.macro_theme                                              AS macro_theme,
            ir.confidence                                               AS relevance_confidence,
            ir.affected_sectors                                         AS affected_sectors,
            n.source                                                    AS news_source,
            n.headline                                                  AS news_headline,
            n.url                                                       AS news_url,
            n.timestamp_utc                                             AS news_published_at,
            ci.sentiment                                                AS impact_sentiment,
            ci.magnitude                                                AS impact_magnitude,
            ci.time_horizon                                            AS time_horizon,
            ci.explanation                                              AS impact_explanation,
            ci.affected_line_items                                     AS affected_line_items,
            CASE ci.magnitude WHEN 1 THEN 0.5 WHEN 2 THEN 1.0 WHEN 3 THEN 2.0
                              WHEN 4 THEN 4.0 WHEN 5 THEN 8.0 ELSE NULL END AS implied_move_pct
        FROM impact_reports ir
        CROSS JOIN LATERAL jsonb_array_elements(ir.trade_signals) sig
        LEFT JOIN news_items n ON n.id = ir.news_id
        LEFT JOIN companies c ON c.symbol = upper(sig->>'symbol')
        LEFT JOIN LATERAL (
            SELECT impact->>'sentiment'              AS sentiment,
                   (impact->>'magnitude')::int       AS magnitude,
                   impact->>'time_horizon'           AS time_horizon,
                   impact->>'explanation'            AS explanation,
                   impact->'affected_line_items'     AS affected_line_items
            FROM jsonb_array_elements(ir.company_impacts) impact
            WHERE upper(impact->>'symbol') = upper(sig->>'symbol')
            LIMIT 1
        ) ci ON true
        WHERE ir.relevant = true
          AND ir.trade_signals IS NOT NULL
          AND jsonb_array_length(ir.trade_signals) > 0
          AND ir.created_at > :start AND ir.created_at <= :end
          AND (sig->>'direction') IN ('long', 'short')
    ) exploded
    ORDER BY signal_id, generated_at DESC
) deduped
ORDER BY impact_magnitude DESC NULLS LAST, generated_at
""")


async def _build_signals(session: AsyncSession, start: datetime, end: datetime) -> list[dict]:
    rows = (await session.execute(_SIGNALS_SQL, {"start": start, "end": end})).mappings().all()
    signals: list[dict] = []
    for r in rows:
        d = dict(r)
        for k in ("generated_at", "news_published_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        # affected_* arrive as JSON already (jsonb); keep as-is
        signals.append(d)
    return signals


def _summarize(signals: list[dict]) -> dict:
    by_conv = Counter(s.get("conviction") or "unknown" for s in signals)
    by_instr = Counter(s.get("instrument_type") or "unknown" for s in signals)
    by_dir = Counter(s.get("direction") or "unknown" for s in signals)
    by_sector = Counter(s.get("sector") or "UNKNOWN" for s in signals)
    by_theme = Counter(s.get("macro_theme") or "UNKNOWN" for s in signals)
    confs = [s["relevance_confidence"] for s in signals if s.get("relevance_confidence") is not None]
    return {
        "by_direction": dict(by_dir),
        "by_conviction": dict(by_conv),
        "by_instrument": dict(by_instr),
        "by_sector": dict(by_sector),
        "by_macro_theme": dict(by_theme),
        "avg_relevance_confidence": round(sum(confs) / len(confs), 4) if confs else None,
    }


async def build_report(session: AsyncSession, trading_day: date) -> dict:
    """Build (not persist) the comprehensive report for a trading day."""
    prev = await prev_trading_day(session, trading_day)
    start, end = report_window(prev, trading_day)
    signals = await _build_signals(session, start, end)
    summary = _summarize(signals)
    return {
        "trading_day": trading_day,
        "prev_trading_day": prev,
        "window_start_utc": start,
        "window_end_utc": end,
        "generated_at": datetime.now(timezone.utc),
        "n_signals": len(signals),
        "n_long": sum(1 for s in signals if s.get("direction") == "long"),
        "n_short": sum(1 for s in signals if s.get("direction") == "short"),
        "n_high_conviction": sum(1 for s in signals if s.get("conviction") == HIGH_CONVICTION),
        "distinct_symbols": len({s.get("symbol") for s in signals}),
        "distinct_sources": len({s.get("news_source") for s in signals if s.get("news_source")}),
        "avg_relevance_confidence": summary["avg_relevance_confidence"],
        "summary": summary,
        "signals": signals,
    }


_UPSERT_SQL = text("""
INSERT INTO signal_reports
    (trading_day, prev_trading_day, window_start_utc, window_end_utc, generated_at,
     n_signals, n_long, n_short, n_high_conviction, distinct_symbols, distinct_sources,
     avg_relevance_confidence, summary, signals)
VALUES
    (:trading_day, :prev_trading_day, :window_start_utc, :window_end_utc, :generated_at,
     :n_signals, :n_long, :n_short, :n_high_conviction, :distinct_symbols, :distinct_sources,
     :avg_relevance_confidence, CAST(:summary AS jsonb), CAST(:signals AS jsonb))
ON CONFLICT (trading_day) DO UPDATE SET
    prev_trading_day = EXCLUDED.prev_trading_day,
    window_start_utc = EXCLUDED.window_start_utc,
    window_end_utc   = EXCLUDED.window_end_utc,
    generated_at     = EXCLUDED.generated_at,
    n_signals        = EXCLUDED.n_signals,
    n_long           = EXCLUDED.n_long,
    n_short          = EXCLUDED.n_short,
    n_high_conviction = EXCLUDED.n_high_conviction,
    distinct_symbols = EXCLUDED.distinct_symbols,
    distinct_sources = EXCLUDED.distinct_sources,
    avg_relevance_confidence = EXCLUDED.avg_relevance_confidence,
    summary          = EXCLUDED.summary,
    signals          = EXCLUDED.signals
""")


async def persist_report(session: AsyncSession, report: dict) -> None:
    params = dict(report)
    params["summary"] = json.dumps(report["summary"], default=str)
    params["signals"] = json.dumps(report["signals"], default=str)
    await session.execute(_UPSERT_SQL, params)
    await session.commit()


async def generate(session: AsyncSession, trading_day: date, persist: bool = True) -> dict:
    report = await build_report(session, trading_day)
    if persist:
        await persist_report(session, report)
        logger.info("generated signal report for %s: %d signals", trading_day, report["n_signals"])
    return report


async def get_stored_report(session: AsyncSession, trading_day: date) -> Optional[dict]:
    row = (await session.execute(
        text("SELECT * FROM signal_reports WHERE trading_day = :d"), {"d": trading_day}
    )).mappings().first()
    return dict(row) if row else None


async def get_or_generate(session: AsyncSession, trading_day: date) -> dict:
    stored = await get_stored_report(session, trading_day)
    if stored is not None:
        return stored
    return await generate(session, trading_day, persist=True)


# ── CLI ─────────────────────────────────────────────────────────────────────

async def _cli() -> None:
    from bharat_terminal.research.db import session_scope

    p = argparse.ArgumentParser(description="Generate per-trading-day signal reports")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="Generate for a specific trading day (YYYY-MM-DD)")
    g.add_argument("--latest", action="store_true", help="Generate for the current actionable trading day")
    g.add_argument("--backfill-days", type=int, help="Generate for every trading day in the last N days")
    args = p.parse_args()

    async with session_scope() as session:
        if args.date:
            day = await resolve_trading_day(session, date.fromisoformat(args.date))
            rep = await generate(session, day)
            print(f"{day}: {rep['n_signals']} signals "
                  f"(long={rep['n_long']} short={rep['n_short']} high-conv={rep['n_high_conviction']})")
        elif args.latest:
            day = await current_actionable_trading_day(session)
            rep = await generate(session, day)
            print(f"{day}: {rep['n_signals']} signals "
                  f"(long={rep['n_long']} short={rep['n_short']} high-conv={rep['n_high_conviction']})")
        else:
            today = datetime.now(IST).date()
            since = today - timedelta(days=args.backfill_days)
            days = (await session.execute(
                text("SELECT DISTINCT date FROM price_points WHERE date >= :s AND date <= :t ORDER BY date"),
                {"s": since, "t": today},
            )).scalars().all()
            for day in days:
                rep = await generate(session, day)
                print(f"{day}: {rep['n_signals']} signals")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    asyncio.run(_cli())
