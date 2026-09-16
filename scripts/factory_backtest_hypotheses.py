"""Backtest agent-proposed hypotheses against the snapshot, with a matched baseline.

Reads specs the Scientist wrote from the ledger, runs each through the SAME
event-study machinery EXP-000001 used, and reports the gate verdict.

THE BASELINE IS RE-RUN HERE, NOT QUOTED. Comparing a new arm against a number
from an earlier run is how STAGE4A's verdict survived a 264-session benchmark
hole for two weeks. Every arm below is computed on one snapshot, in one
process, so the deltas mean something.

Read-only: nothing is written to the ledger. factory_gate has no login member,
so a verdict cannot be recorded even in principle -- it is computed and printed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import factory_exp_000001 as exp          # the verified loaders, reused not rewritten

from bharat_terminal.factory import FACTORY_SEED
from bharat_terminal.factory.backtest.calendar import TradingCalendar
from bharat_terminal.factory.backtest.engine import EventInput, StudyConfig, run_event_study
from bharat_terminal.factory.costs.india_cash import CASH_DELIVERY_V1
from bharat_terminal.factory.gates.engine import GateInput, Status, evaluate_gate
from bharat_terminal.factory.stats.bootstrap import block_bootstrap_ci
from bharat_terminal.factory.stats.cohort import cohort_stats
from bharat_terminal.factory.stats.dsr import deflated_sharpe
from bharat_terminal.factory.tools_api.pools import PoolSpec, make_executor, open_pool


def arm_from_spec(hid: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the mechanics that actually differ between these hypotheses."""
    return {
        "id": hid,
        "min_mtv": float(spec["universe"].get("min_median_traded_value_cr") or 0),
        "hold": int(spec["statistics"]["primary_holding_days"]),
        "direction": spec["signal"]["direction"],
        "entry": spec["entry"]["rule"],
        "decile": int(spec["event"]["filters"][0]["value"]),
    }


def run_arm(arm, events, prices, bench, calendar, cas) -> Dict[str, Any]:
    inputs = [EventInput(symbol=e["symbol"], event_date=e["event_date"],
                         median_traded_value_cr=e["mtv"])
              for e in events if e["decile"] == arm["decile"]]
    cfg = StudyConfig(entry_rule=arm["entry"], holding_days=arm["hold"],
                      direction=arm["direction"], rate_card=CASH_DELIVERY_V1,
                      min_median_traded_value_cr=arm["min_mtv"])
    out = run_event_study(inputs, prices, bench, calendar, cfg, corporate_actions=cas)
    coh = list(out.cohorts)
    if not coh:
        return {"arm": arm, "events": 0, "cohorts": 0, "exclusions": out.exclusions}
    st = cohort_stats(coh, holding_days=arm["hold"])
    means = [m for _, _, m in coh]
    ci = block_bootstrap_ci(means, n_resamples=10_000, seed=FACTORY_SEED)
    dsr = deflated_sharpe(means, n_trials=3)   # three arms compared here
    net = sum(r.net_abnormal for r in out.results) / len(out.results)
    gross = sum(r.gross_abnormal for r in out.results) / len(out.results)
    return {"arm": arm, "events": out.events_used, "cohorts": len(coh),
            "net_pct": 100 * net, "gross_pct": 100 * gross,
            "cohort_mean_pct": 100 * st.mean, "t": st.tstat, "nw_lag": st.lag,
            "pos_share": st.positive_cohort_share, "by_year": st.by_year,
            "ci": (100 * ci.lo, 100 * ci.hi), "dsr": dsr.probability,
            "cost_bps": out.mean_cost_bps, "exclusions": out.exclusions, "stats": st}


def gate_for(r: Dict[str, Any]) -> Any:
    st = r["stats"]
    by_year = st.by_year or {}
    gi = GateInput(
        hypothesis_id=r["arm"]["id"], version=1, from_status=Status.SIMULATED,
        data_tier="H", cohort_tstat=st.tstat, cohort_mean=st.mean,
        positive_year_share=(sum(1 for v in by_year.values() if v > 0) / len(by_year))
        if by_year else None,
        n_events=st.n_events, n_cohorts=st.n_cohorts,
        dsr_probability=r["dsr"], lord_rejected=None,
        critical_battery_failures=(), rate_card_verified=False)
    return evaluate_gate(gi)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="/snapshots/DS-20260913-02")
    args = ap.parse_args()

    pool = await open_pool(PoolSpec("FACTORY_DB_URL", "factory_api", max_size=2))
    ex = make_executor(pool)
    rows = await ex("SELECT hypothesis_id, spec_json FROM factory.hypotheses "
                    "ORDER BY hypothesis_id", {})
    await pool.close()
    if not rows:
        print("no hypotheses in the ledger"); return 1

    specs = [(r["hypothesis_id"],
              json.loads(r["spec_json"]) if isinstance(r["spec_json"], str) else r["spec_json"])
             for r in rows]
    arms = [arm_from_spec(h, s) for h, s in specs]
    # The matched baseline: the incumbent mechanics, same snapshot, same process.
    arms.insert(0, {"id": "BASELINE(H-000001)", "min_mtv": 1.0, "hold": 20,
                    "direction": "long", "entry": "next_session_open", "decile": 10})

    print(f"snapshot: {args.snapshot}")
    events, drops = exp.load_events(os.path.join(args.snapshot, "earnings_surprises.csv.gz"))
    symbols = {e["symbol"] for e in events} | {exp.BENCHMARK}
    prices, market_dates = exp.load_prices(
        os.path.join(args.snapshot, "price_points.csv.gz"), symbols)
    cas = exp.load_corporate_actions(
        os.path.join(args.snapshot, "corporate_actions.csv.gz"), symbols)
    bench = prices.get(exp.BENCHMARK)
    calendar = TradingCalendar(market_dates)
    print(f"events {len(events)}  priced symbols {len(prices)}  sessions {len(calendar)}\n")

    results = []
    for a in arms:
        r = run_arm(a, events, prices, bench, calendar, cas)
        results.append(r)
        print(f"--- {a['id']}   min_mtv>={a['min_mtv']}Cr  hold={a['hold']}d")
        if not r["cohorts"]:
            print(f"    NO COHORTS. exclusions={r['exclusions']}")
            continue
        print(f"    events {r['events']:>5}  cohorts {r['cohorts']:>4}  "
              f"cost {r['cost_bps']:.1f}bps")
        print(f"    net {r['net_pct']:+.3f}%  (gross {r['gross_pct']:+.3f}%)")
        print(f"    cohort mean {r['cohort_mean_pct']:+.3f}%  t={r['t']:.2f} "
              f"(NW lag {r['nw_lag']})  {r['pos_share']:.0%} positive")
        print(f"    bootstrap 95% CI [{r['ci'][0]:+.3f}%, {r['ci'][1]:+.3f}%]  "
              f"DSR p={r['dsr']:.3f}")
        g = gate_for(r)
        print(f"    GATE {g.from_status.value} -> {g.to_status.value}"
              f"{'  PROMOTED' if g.promoted else ''}")
        if g.blocking:
            for b in g.blocking:
                print(f"         blocked: {b}")

    base = results[0]
    print("\n=== versus the matched baseline ===")
    for r in results[1:]:
        if not r["cohorts"] or not base["cohorts"]:
            print(f"  {r['arm']['id']}: not comparable (no cohorts)"); continue
        print(f"  {r['arm']['id']}: net {r['net_pct']:+.3f}% vs {base['net_pct']:+.3f}%  "
              f"(delta {r['net_pct'] - base['net_pct']:+.3f}pp)   "
              f"t {r['t']:.2f} vs {base['t']:.2f}   "
              f"events {r['events']} vs {base['events']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
