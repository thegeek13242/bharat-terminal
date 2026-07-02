"""Internal paper-testing of analysis output (never customer-facing).

Simulates trading the evaluated rows in `signal_outcomes` to answer one
question honestly: does the analysis carry tradeable information content
net of costs — or is the raw hit-rate an artifact of clustering and drift?

Design decisions (each addresses a known weakness of the raw hit-rate):
  * Unit of independence = the ENTRY-DAY COHORT, not the trade. Signals
    sharing an entry day are one clustered bet on that day's tape; the
    t-stat is computed across cohorts (n = number of entry days).
  * Equal weight within a cohort. Stage-5 position sizes are unvalidated
    and deliberately ignored here.
  * Costs are explicit: --cost-bps-side (default 35 bps/side ≈ delivery
    STT + charges + midcap slippage) deducted from every round trip.
  * Benchmark = MEDIAN same-horizon forward return across every symbol
    priced on the entry date (no index series exists in price_points —
    verified 2026-07-02). Excess = signed return minus signed benchmark.
  * Multi-day cash shorts are not implementable in India; short trades
    are reported separately and labeled futures-assumed.
  * Trades with a split/bonus ex-date inside the holding window are
    excluded (price_points closes are unadjusted) and counted.

Read-only over signal_outcomes / price_points / corporate_actions.
Deterministic: no sampling; seed declared per project rule.

Usage:
  python -m bharat_terminal.research.paper_trading run --horizon 5
  python -m bharat_terminal.research.paper_trading run --horizon 10 \
      --cost-bps-side 35 --shorts only --out /tmp/paper_10d.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
from sqlalchemy import bindparam, text

from bharat_terminal.research.db import session_scope

np.random.seed(42)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("paper_trading")

HORIZONS = (1, 5, 10, 20)


# ── data loading ────────────────────────────────────────────────────────────
async def _load_trades(horizon: int, since_days: int) -> list[dict]:
    col = f"ret_{horizon}d"
    async with session_scope() as s:
        rows = (await s.execute(text(f"""
            SELECT signal_id, symbol, direction, conviction, sector, macro_theme,
                   entry_date, entry_price, {col} AS ret
            FROM signal_outcomes
            WHERE {col} IS NOT NULL AND entry_date IS NOT NULL
              AND direction IN ('long', 'short')
              AND signal_created_at >= now() - make_interval(days => :since)
            ORDER BY entry_date, signal_id
        """), {"since": since_days})).mappings().all()
    return [dict(r) for r in rows]


async def _load_benchmarks(horizon: int, entry_dates: list[date]) -> dict[date, float]:
    """Median forward return over `horizon` trading days across all priced
    symbols, per entry date. Median (not mean) so unadjusted corporate-action
    jumps in individual names don't pollute the market proxy."""
    stmt = text("""
        WITH ordered AS (
            SELECT symbol, date, close,
                   row_number() OVER (PARTITION BY symbol ORDER BY date) AS rn
            FROM price_points
        ),
        fwd AS (
            SELECT o1.date,
                   (o2.close / NULLIF(o1.close, 0) - 1.0) * 100.0 AS fwd_ret
            FROM ordered o1
            JOIN ordered o2 ON o2.symbol = o1.symbol AND o2.rn = o1.rn + :h
        )
        SELECT date,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY fwd_ret) AS bench,
               count(*) AS n
        FROM fwd
        WHERE date IN :dates
        GROUP BY date
    """).bindparams(bindparam("dates", expanding=True))
    async with session_scope() as s:
        rows = (await s.execute(stmt, {"h": horizon, "dates": list(entry_dates)})).all()
    return {d: float(b) for d, b, _n in rows if b is not None}


async def _load_ca_windows() -> dict[str, list[date]]:
    """Split/bonus ex-dates per symbol (unadjusted closes make these fake moves)."""
    async with session_scope() as s:
        rows = (await s.execute(text("""
            SELECT symbol, ex_date FROM corporate_actions
            WHERE ex_date IS NOT NULL
              AND (action_type ILIKE '%split%' OR action_type ILIKE '%bonus%')
        """))).all()
    out: dict[str, list[date]] = defaultdict(list)
    for sym, ex in rows:
        out[sym].append(ex)
    return out


# ── simulation ──────────────────────────────────────────────────────────────
def _signed(ret: float, direction: str) -> float:
    return ret if direction == "long" else -ret


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    a = np.asarray(values, dtype=float)
    out = {
        "n": int(a.size),
        "mean_pct": round(float(a.mean()), 3),
        "median_pct": round(float(np.median(a)), 3),
        "std_pct": round(float(a.std(ddof=1)), 3) if a.size > 1 else None,
        "win_rate_pct": round(float((a > 0).mean() * 100.0), 1),
    }
    if a.size > 1 and a.std(ddof=1) > 0:
        out["t_stat"] = round(float(a.mean() / (a.std(ddof=1) / math.sqrt(a.size))), 2)
    return out


def simulate(trades: list[dict], benchmarks: dict[date, float],
             ca_windows: dict[str, list[date]], horizon: int,
             cost_bps_side: float, shorts: str) -> dict:
    # Holding window in calendar days for the corporate-action exclusion
    # (horizon is in trading days; ~7/5 calendar ratio + settlement buffer).
    window_days = math.ceil(horizon * 7 / 5) + 3
    round_trip_cost_pct = 2 * cost_bps_side / 100.0

    kept, excluded_ca, excluded_dir, no_bench = [], 0, 0, 0
    for t in trades:
        if shorts == "exclude" and t["direction"] == "short":
            excluded_dir += 1
            continue
        if shorts == "only" and t["direction"] != "short":
            excluded_dir += 1
            continue
        entry = t["entry_date"]
        if any(entry < ex <= entry + timedelta(days=window_days)
               for ex in ca_windows.get(t["symbol"], ())):
            excluded_ca += 1
            continue
        if entry not in benchmarks:
            no_bench += 1
            continue
        gross = _signed(float(t["ret"]), t["direction"])
        bench = _signed(benchmarks[entry], t["direction"])
        t = dict(t)
        t["gross_pct"] = gross
        t["net_pct"] = gross - round_trip_cost_pct
        t["excess_net_pct"] = gross - bench - round_trip_cost_pct
        kept.append(t)

    cohorts: dict[date, list[dict]] = defaultdict(list)
    for t in kept:
        cohorts[t["entry_date"]].append(t)
    cohort_rows = [
        {
            "entry_date": d.isoformat(),
            "n_trades": len(ts),
            "n_long": sum(1 for t in ts if t["direction"] == "long"),
            "n_short": sum(1 for t in ts if t["direction"] == "short"),
            "net_pct": round(float(np.mean([t["net_pct"] for t in ts])), 3),
            "excess_net_pct": round(float(np.mean([t["excess_net_pct"] for t in ts])), 3),
        }
        for d, ts in sorted(cohorts.items())
    ]

    # Compounded strategy return: each cohort gets 1/horizon of capital (at
    # most `horizon` cohorts are ever open at once). Approximation — cohorts
    # are compounded sequentially, not marked to market daily.
    nav = 1.0
    for c in cohort_rows:
        nav *= 1.0 + (c["net_pct"] / 100.0) / horizon

    def _slice(key) -> dict:
        groups: dict[str, list[dict]] = defaultdict(list)
        for t in kept:
            groups[str(t.get(key) or "UNKNOWN")].append(t)
        top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:10]
        return {
            k: {
                "trades": _stats([t["net_pct"] for t in ts]),
                "excess": _stats([t["excess_net_pct"] for t in ts]),
            }
            for k, ts in top
        }

    return {
        "config": {
            "horizon_days": horizon,
            "cost_bps_per_side": cost_bps_side,
            "shorts": shorts,
            "benchmark": "median same-horizon forward return, all priced symbols (no index series available)",
            "weighting": "equal weight per trade within entry-day cohort",
        },
        "sample": {
            "trades_used": len(kept),
            "entry_days": len(cohorts),
            "excluded_corporate_action": excluded_ca,
            "excluded_direction_filter": excluded_dir,
            "excluded_no_benchmark": no_bench,
            "date_range": [cohort_rows[0]["entry_date"], cohort_rows[-1]["entry_date"]] if cohort_rows else None,
        },
        "per_trade": {
            "net": _stats([t["net_pct"] for t in kept]),
            "excess_net": _stats([t["excess_net_pct"] for t in kept]),
        },
        "per_cohort": {
            "net": _stats([c["net_pct"] for c in cohort_rows]),
            "excess_net": _stats([c["excess_net_pct"] for c in cohort_rows]),
            "note": "cohort-level stats are the honest read; per-trade n overstates independence",
        },
        "strategy": {
            "compounded_net_return_pct": round((nav - 1.0) * 100.0, 2),
            "note": "1/horizon capital per cohort, sequential compounding approximation",
        },
        "slices": {
            "direction": _slice("direction"),
            "conviction": _slice("conviction"),
            "sector": _slice("sector"),
            "macro_theme": _slice("macro_theme"),
        },
        "caveats": [
            "shorts assume futures execution; multi-day cash shorts are not implementable",
            "price_points closes are unadjusted; split/bonus windows excluded, other actions may remain",
            "benchmark is an equal-universe median, not an investable index",
            "entry uses the evaluated close from score_signals (no intraday fill modeling)",
        ],
    }


# ── cli ─────────────────────────────────────────────────────────────────────
async def run(horizon: int, since_days: int, cost_bps_side: float,
              shorts: str, out: str | None) -> None:
    trades = await _load_trades(horizon, since_days)
    if not trades:
        logger.info("no evaluated trades for this horizon — run score_signals first")
        return
    entry_dates = sorted({t["entry_date"] for t in trades})
    benchmarks = await _load_benchmarks(horizon, entry_dates)
    ca_windows = await _load_ca_windows()
    report = simulate(trades, benchmarks, ca_windows, horizon, cost_bps_side, shorts)

    print(json.dumps(report, indent=2, default=str))
    if out:
        with open(out, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"report written to {out}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="paper_trading")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--horizon", type=int, default=5, choices=HORIZONS)
    p.add_argument("--since-days", type=int, default=365)
    p.add_argument("--cost-bps-side", type=float, default=35.0)
    p.add_argument("--shorts", choices=("include", "exclude", "only"), default="include")
    p.add_argument("--out", default=None)
    args = ap.parse_args()
    asyncio.run(run(args.horizon, args.since_days, args.cost_bps_side, args.shorts, args.out))


if __name__ == "__main__":
    main()
