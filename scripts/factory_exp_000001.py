"""EXP-000001 — reproduce the incumbent PEAD composite-SUE recipe on a snapshot.

The Day 1-3 acceptance test for the deterministic core. It deliberately drives
the REAL components (DSL compiler -> backtester -> cohort statistics), not a
bespoke calculation, so a pass means the pipeline works, not that this script
does.

What it does NOT claim: an exact match to the 2026-07-04 validated study
(+2.11% D10-D1 @20d, t=4.7, 51 monthly cohorts) or to STAGE4A (+2.84%, t=5.69,
18 quarterly cohorts). This runs a different construction on purpose - weekly
cohorts, NIFTYBEES benchmark, next-session-OPEN entry, statutory costs and
liquidity slippage netted, corporate-action windows dropped, and only the
in-sample window before the sealed holdout. The deltas are printed so the
comparison is honest rather than flattering.

Run it in-container (numpy lives there, not on the host):

    docker compose run --rm --no-deps \
      -v "$PWD/bharat_terminal:/app/bharat_terminal:ro" \
      -v "$PWD/scripts:/app/scripts:ro" \
      -v "/home/ec2-user/factory_snapshots:/snapshots:ro" \
      --entrypoint python papertrade scripts/factory_exp_000001.py \
      --snapshot /snapshots/DS-20260912-01
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bharat_terminal.factory import FACTORY_SEED
from bharat_terminal.factory.backtest.calendar import TradingCalendar
from bharat_terminal.factory.backtest.engine import (
    EventInput,
    StudyConfig,
    run_event_study,
)
from bharat_terminal.factory.backtest.returns import Bar, PriceSeries
from bharat_terminal.factory.costs.india_cash import CASH_DELIVERY_V1
from bharat_terminal.factory.dsl.compiler import (
    CompileContext,
    EventCounts,
    compile_spec,
)
from bharat_terminal.factory.dsl.hashing import result_hash
from bharat_terminal.factory.dsl.models import HypothesisSpec
from bharat_terminal.factory.snapshot.integrity import check_series_continuity
from bharat_terminal.factory.snapshot.manifest import Manifest
from bharat_terminal.factory.stats.bootstrap import block_bootstrap_ci
from bharat_terminal.factory.stats.cohort import cohort_stats
from bharat_terminal.factory.stats.dsr import deflated_sharpe

BENCHMARK = "NIFTYBEES"
SEALED_FROM = date(2025, 9, 12)
HOLDING_DAYS = 20

# The incumbent recipe, expressed in the factory's own DSL.
SPEC_YAML: Dict = {
    "dsl_version": 1,
    "id": "H-000001",
    "version": 1,
    "parents": [],
    "family": "pead_composite_sue",
    "question": None,
    "horizon": "swing",
    "claim": {
        "description": "Top-decile composite-SUE surprises drift upward over 20 trading days.",
        "mechanism": "Investors anchor on the naive headline and under-react to the surprise.",
        "falsified_if": "Net abnormal return <= 0, or the D10-D1 cohort t-stat falls below 2.",
    },
    "universe": {
        "base": "listed_equity_nse_bse",
        "min_median_traded_value_cr": 1.0,
        "exclude": ["fund_scheme", "suspended"],
        "max_participation_pct_adv": 2.0,
    },
    "event": {
        "source": "earnings_surprises",
        "clock": "announcement_date",
        "filters": [{"feature": "sue_composite@2.1", "op": "decile_ge", "value": 10,
                     "reference": "trailing_365d"}],
    },
    "signal": {"feature": "sue_composite@2.1", "direction": "long"},
    "entry": {"rule": "next_session_open", "delay_minutes": 0},
    "exit": {"holding_days": [5, 10, 20], "stop": "none", "squareoff_intraday": False},
    "benchmark": "niftybees",
    "costs": {"model": "india_cash_v1", "slippage": "adv_bucket_v1", "multiplier": 1.0},
    "controls": ["liquidity_tier"],
    "statistics": {"unit": "calendar_week", "primary_metric": "net_abnormal_return",
                   "primary_holding_days": 20, "min_events": 300, "min_cohorts": 12},
    "validation": {"holdout": "sealed_h_v1", "battery": "default_v1"},
    "sweep": {},
}


def rows(path: str):
    with gzip.open(path, "rt", newline="") as fh:
        yield from csv.DictReader(fh)


def parse_date(v: str) -> Optional[date]:
    if not v:
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def load_events(path: str) -> Tuple[List[dict], Dict[str, int]]:
    """Decile-1 and decile-10 events inside the in-sample window."""
    kept: List[dict] = []
    drops: Dict[str, int] = defaultdict(int)
    for r in rows(path):
        dec = r.get("decile")
        if dec not in ("1", "10"):
            drops["decile_not_extreme"] += 1
            continue
        ann = r.get("announced_at") or ""
        if not ann:
            drops["no_announced_at"] += 1
            continue
        # IST calendar date of the announcement - the clock the compiler allows.
        try:
            ts = datetime.fromisoformat(ann.replace(" ", "T"))
        except ValueError:
            drops["unparseable_announced_at"] += 1
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ist_date = (ts.astimezone(timezone.utc).timestamp() + 5.5 * 3600)
        ev_date = datetime.fromtimestamp(ist_date, tz=timezone.utc).date()
        if ev_date >= SEALED_FROM:
            drops["sealed_holdout"] += 1
            continue
        if (r.get("liquidity_pass") or "").lower() not in ("t", "true"):
            drops["liquidity_pass_false"] += 1
            continue
        try:
            mtv = float(r.get("median_traded_value_cr") or 0.0)
        except ValueError:
            mtv = 0.0
        kept.append({"symbol": r["symbol"], "event_date": ev_date,
                     "decile": int(dec), "mtv": mtv})
    return kept, dict(drops)


def load_prices(path: str, symbols: set) -> Tuple[Dict[str, PriceSeries], List[date]]:
    """Bars for the symbols we need, plus EVERY trading date in the table.

    The calendar must come from the whole market, not from one series. Built
    from NIFTYBEES alone it inherited that ETF's 392-day hole (2025-05-08 ->
    2026-06-04), which silently pushed entry dates a year forward.
    """
    bars: Dict[str, List[Bar]] = defaultdict(list)
    all_dates: set = set()
    for r in rows(path):
        d = parse_date(r["date"])
        if d is None:
            continue
        all_dates.add(d)
        sym = r["symbol"]
        if sym not in symbols:
            continue
        try:
            o, c = float(r["open"] or 0), float(r["close"] or 0)
        except ValueError:
            continue
        if o <= 0 or c <= 0:
            continue
        bars[sym].append(Bar(d, o, c))
    return {s: PriceSeries(s, b) for s, b in bars.items()}, sorted(all_dates)


def load_corporate_actions(path: str, symbols: set) -> Dict[str, List[date]]:
    cas: Dict[str, List[date]] = defaultdict(list)
    for r in rows(path):
        sym = r["symbol"]
        if sym not in symbols:
            continue
        if (r.get("action_type") or "").upper() not in ("SPLIT", "BONUS", "RIGHTS"):
            continue
        d = parse_date(r["ex_date"])
        if d:
            cas[sym].append(d)
    return dict(cas)


def run_leg(events, prices, benchmark, calendar, cas, entry_rule: str, decile: int,
            direction: str = "long"):
    inputs = [EventInput(symbol=e["symbol"], event_date=e["event_date"],
                         median_traded_value_cr=e["mtv"])
              for e in events if e["decile"] == decile]
    cfg = StudyConfig(entry_rule=entry_rule, holding_days=HOLDING_DAYS,
                      direction=direction, rate_card=CASH_DELIVERY_V1,
                      min_median_traded_value_cr=1.0)
    return run_event_study(inputs, prices, benchmark, calendar, cfg, corporate_actions=cas)


def spread_cohorts(out_long, out_short) -> List[Tuple[str, int, float]]:
    """Cohort-level long/short portfolio, weeks present in both legs.

    The D1 leg is run SHORT so the engine charges its costs and signs its
    benchmark. Netting net(D10) - net(D1) would have cancelled the costs of
    both legs and reported a gross number as net.
    """
    a = {k: (n, m) for k, n, m in out_long.cohorts}
    b = {k: (n, m) for k, n, m in out_short.cohorts}
    return [(k, a[k][0] + b[k][0], a[k][1] + b[k][1]) for k in sorted(set(a) & set(b))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    snap = args.snapshot
    manifest = Manifest.from_dict(json.load(open(os.path.join(snap, "manifest.json"))))
    problems = manifest.verify(snap)
    print(f"snapshot         : {manifest.dataset_version}")
    print(f"manifest verify  : {'OK' if not problems else problems}")
    if problems:
        return 2

    # ---- compile the spec through the real compiler -------------------
    spec = HypothesisSpec.model_validate(SPEC_YAML)
    ctx = CompileContext(
        dataset_version=manifest.dataset_version,
        snapshot_age_hours=manifest.age_hours(),
        integrity_flags=frozenset({"clock_lags_dissemination"}),
        sealed_from=SEALED_FROM,
        event_counter=lambda s: EventCounts(events=count10, cohorts=cohorts10, symbols=syms10),
    )

    print("\nloading snapshot ...")
    events, drops = load_events(os.path.join(snap, "earnings_surprises.csv.gz"))
    symbols = {e["symbol"] for e in events} | {BENCHMARK}
    prices, market_dates = load_prices(os.path.join(snap, "price_points.csv.gz"), symbols)
    cas = load_corporate_actions(os.path.join(snap, "corporate_actions.csv.gz"), symbols)
    bench = prices.get(BENCHMARK)
    if bench is None:
        print(f"FAIL: benchmark {BENCHMARK} missing from the snapshot")
        return 2
    calendar = TradingCalendar(market_dates)

    print(f"events (D1+D10)  : {len(events)}  | dropped: {drops}")
    print(f"symbols priced   : {len(prices)}  | calendar sessions: {len(calendar)}")

    # The benchmark is holed (verified 2026-09-12): events inside the hole have
    # no benchmark leg and the engine drops them as `no_benchmark_price`.
    cont = check_series_continuity(BENCHMARK, bench.dates, market_dates)
    print(f"benchmark check  : [{cont.status.upper()}] {cont.detail}")

    results = {}
    for entry_rule in ("next_session_open", "next_session_close"):
        out10 = run_leg(events, prices, bench, calendar, cas, entry_rule, 10, "long")
        out1 = run_leg(events, prices, bench, calendar, cas, entry_rule, 1, "short")
        sc = spread_cohorts(out10, out1)
        st = cohort_stats(sc, holding_days=HOLDING_DAYS)
        ci = block_bootstrap_ci([m for _, _, m in sc], n_resamples=10_000)
        dsr = deflated_sharpe([m for _, _, m in sc], n_trials=1)

        d10_mean = (sum(r.net_abnormal for r in out10.results) / len(out10.results)) if out10.results else 0.0
        d1_mean = (sum(r.net_abnormal for r in out1.results) / len(out1.results)) if out1.results else 0.0
        d10_gross = (sum(r.gross_abnormal for r in out10.results) / len(out10.results)) if out10.results else 0.0

        results[entry_rule] = {
            "d10_events": out10.events_used, "d1_events": out1.events_used,
            "d10_net_mean_pct": 100 * d10_mean, "d1_net_mean_pct": 100 * d1_mean,
            "d10_gross_mean_pct": 100 * d10_gross,
            "spread_net_pct": 100 * (d10_mean - d1_mean),
            "cohorts": len(sc), "cohort_spread_mean_pct": 100 * st.mean,
            "cohort_t": st.tstat, "nw_lag": st.lag,
            "positive_cohort_share": st.positive_cohort_share,
            "bootstrap_ci_pct": [100 * ci.lo, 100 * ci.hi],
            "dsr_probability": dsr.probability,
            "mean_cost_bps": out10.mean_cost_bps,
            "exclusions_d10": out10.exclusions, "exclusions_d1": out1.exclusions,
            "by_year_spread_pct": {y: round(100 * v, 3) for y, v in sorted(st.by_year.items())},
        }

    count10 = results["next_session_open"]["d10_events"]
    cohorts10 = results["next_session_open"]["cohorts"]
    syms10 = len({e["symbol"] for e in events if e["decile"] == 10})
    compiled = compile_spec(spec, ctx)

    print("\ncompiler         :", "OK" if compiled.ok else [str(e) for e in compiled.errors])
    if compiled.plan:
        print(f"  data_tier={compiled.plan.data_tier} holdout={compiled.plan.holdout} "
              f"trials={compiled.plan.trial_count}")
        print(f"  bias={list(compiled.plan.bias_flags)} constraints={list(compiled.plan.constraints)}")

    for rule, r in results.items():
        print(f"\n--- entry: {rule} | hold {HOLDING_DAYS}d | net of costs ---")
        print(f"  LONG-ONLY D10 {r['d10_events']:>6} events   net mean {r['d10_net_mean_pct']:+.3f}%"
              f"   (gross {r['d10_gross_mean_pct']:+.3f}%)   <- the tradable claim")
        print(f"  SHORT     D1  {r['d1_events']:>6} events   net mean {r['d1_net_mean_pct']:+.3f}%"
              f"   (costs charged on this leg too)")
        print(f"  long/short portfolio per event  {r['spread_net_pct']:+.3f}%")
        print(f"  cohort spread {r['cohort_spread_mean_pct']:+.3f}%  t={r['cohort_t']:.2f} "
              f"(NW lag {r['nw_lag']}, {r['cohorts']} weekly cohorts, "
              f"{r['positive_cohort_share']:.0%} positive)")
        print(f"  bootstrap 95% CI [{r['bootstrap_ci_pct'][0]:+.3f}%, {r['bootstrap_ci_pct'][1]:+.3f}%]"
              f"   DSR p={r['dsr_probability']:.3f}   mean cost {r['mean_cost_bps']:.1f} bps")
        print(f"  by year: {r['by_year_spread_pct']}")
        print(f"  D10 exclusions: {r['exclusions_d10']}")

    payload = {
        "experiment_id": "EXP-000001",
        "dataset_version": manifest.dataset_version,
        "manifest_digest": manifest.digest,
        "seed": FACTORY_SEED,
        "sealed_from": SEALED_FROM.isoformat(),
        "holding_days": HOLDING_DAYS,
        "benchmark": BENCHMARK,
        "spec_hash": compiled.plan.spec_hash if compiled.plan else None,
        "results": results,
        "event_drops": drops,
    }
    payload["result_hash"] = result_hash(payload["results"])
    print(f"\nresult_hash      : {payload['result_hash'][:16]}")

    out = args.out or os.path.join("/tmp", "EXP-000001.json")
    with open(out, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"written          : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
