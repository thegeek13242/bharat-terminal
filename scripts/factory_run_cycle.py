"""Close the loop: ledger hypothesis -> experiment -> backtest -> alpha wealth -> verdict.

This is the piece that turns a proposal into a recorded decision. Until it
existed, the Scientist could write hypotheses all day and nothing on earth
could judge them: factory_gate had no login member, so `gate_decisions` was
unwritable by anything.

TWO EXECUTORS, ON PURPOSE
The privilege split is not decoration, so this script honours it rather than
running everything as one convenient superuser:

    factory_api        writes experiments and experiment_runs (research)
    factory_api_gate   writes gate_decisions, hypothesis_status, alpha_wealth

Neither can do the other's job -- verified against live sessions. If this
script is ever "simplified" to a single connection, that separation is gone and
the gate becomes advisory.

DRY RUN BY DEFAULT. A verdict is durable and a hypothesis_status is the thing
downstream code trusts, so the first pass prints what it would decide and
writes nothing.

ALPHA WEALTH IS SPENT BEFORE THE GATE, NOT AFTER
The gate needs `lord_rejected`, which only exists once the test is charged
against the error budget. Spending is therefore part of judging, not a
bookkeeping step afterwards -- which is what makes testing repeatedly costly
rather than free.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import factory_exp_000001 as exp

from bharat_terminal.factory import FACTORY_SEED
from bharat_terminal.factory.backtest.calendar import TradingCalendar
from bharat_terminal.factory.backtest.engine import EventInput, StudyConfig, run_event_study
from bharat_terminal.factory.backtest.signals import (
    UnbacktestableSpec, check_signal_supported)
from bharat_terminal.factory.costs.india_cash import CASH_DELIVERY_V1
from bharat_terminal.factory.dsl.dedup import known_mechanics, without_self
from bharat_terminal.factory.dsl.compiler import CompileContext, EventCounts, compile_spec
from bharat_terminal.factory.dsl.hashing import result_hash
from bharat_terminal.factory.dsl.models import HypothesisSpec
from bharat_terminal.factory.gates.alpha_wealth import (
    remaining,
    spend_and_record,
    two_sided_p,
)
from bharat_terminal.factory.gates.engine import GateInput, Status, evaluate_gate
from bharat_terminal.factory.stats.bootstrap import block_bootstrap_ci
from bharat_terminal.factory.stats.cohort import cohort_stats
from bharat_terminal.factory.stats.dsr import deflated_sharpe
from bharat_terminal.factory.tools_api.pools import PoolSpec, make_executor, open_pool


async def build_ctx(ex, snapshot_dir: str) -> CompileContext:
    ds = await ex("""SELECT dataset_version, sealed_from,
                            EXTRACT(EPOCH FROM (now()-created_at))/3600.0 AS age_h
                       FROM factory.dataset_versions ORDER BY created_at DESC LIMIT 1""", {})
    row = ds[0]
    return CompileContext(
        dataset_version=row["dataset_version"],
        snapshot_age_hours=float(row["age_h"] or 0),
        integrity_flags=frozenset({"clock_lags_dissemination"}),
        sealed_from=row["sealed_from"],
        known_spec_hashes=await known_mechanics(ex),
        event_counter=lambda _s: EventCounts(events=900, cohorts=40, symbols=250),
    )


async def next_experiment_id(ex) -> str:
    r = await ex("""SELECT COALESCE(MAX((substring(experiment_id from '^EXP-([0-9]+)$'))::int),0)+1 n
                      FROM factory.experiments WHERE experiment_id ~ '^EXP-[0-9]+$'""", {})
    return f"EXP-{int(r[0]['n'] or 1):06d}"


def backtest(spec: HypothesisSpec, data) -> Optional[Dict[str, Any]]:
    """Run the study the spec describes. None when it yields no cohorts.

    REFUSES rather than substitutes. Until 2026-09-17 this function read
    spec.signal.direction and never spec.signal.feature, then filtered on the
    snapshot's `decile` column -- which is composite SUE. A hypothesis signalled
    on expectation_surprise_gbm therefore ran a SUE study and reported real
    looking numbers for it: H-000026 and H-000027 came back byte-identical
    (1,751 events, t=1.00) despite carrying different signals.

    Silent substitution that reports success is the failure mode this
    repository keeps an audit trail about, so the mismatch is now fatal. The
    cost of refusing is a hypothesis that stays un-evaluated, which is true.
    The cost of substituting is a fabricated result that looks like evidence.
    """
    events, prices, bench, calendar, cas = data
    check_signal_supported(spec.signal.feature)
    decile = int(spec.event.filters[0].value)
    hold = int(spec.statistics.primary_holding_days)
    inputs = [EventInput(symbol=e["symbol"], event_date=e["event_date"],
                         median_traded_value_cr=e["mtv"])
              for e in events if e["decile"] == decile]
    cfg = StudyConfig(entry_rule=spec.entry.rule.value, holding_days=hold,
                      direction=spec.signal.direction.value, rate_card=CASH_DELIVERY_V1,
                      min_median_traded_value_cr=float(
                          spec.universe.min_median_traded_value_cr or 0))
    out = run_event_study(inputs, prices, bench, calendar, cfg, corporate_actions=cas)
    coh = list(out.cohorts)
    if not coh:
        return None
    st = cohort_stats(coh, holding_days=hold)
    means = [m for _, _, m in coh]
    ci = block_bootstrap_ci(means, n_resamples=10_000, seed=FACTORY_SEED)
    dsr = deflated_sharpe(means, n_trials=max(1, len(coh) // 40))
    payload = {"n_events": st.n_events, "n_cohorts": st.n_cohorts, "mean": st.mean,
               "tstat": st.tstat, "nw_lag": st.lag,
               "positive_cohort_share": st.positive_cohort_share, "by_year": st.by_year,
               "ci_low": ci.lo, "ci_high": ci.hi, "dsr": dsr.probability,
               "mean_cost_bps": out.mean_cost_bps, "seed": FACTORY_SEED}
    return {"stats": st, "dsr": dsr.probability, "payload": payload,
            "result_hash": result_hash(payload), "exclusions": out.exclusions}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="/snapshots/DS-20260916-01")
    ap.add_argument("--apply", action="store_true",
                    help="write experiments, runs, alpha wealth and verdicts "
                         "(default: dry run)")
    args = ap.parse_args()

    for var in ("FACTORY_DB_URL", "FACTORY_GATE_DB_URL"):
        if not os.getenv(var):
            print(f"{var} is not set; refusing to run as a single privileged role")
            return 2

    wpool = await open_pool(PoolSpec("FACTORY_DB_URL", "factory_api", max_size=3))
    gpool = await open_pool(PoolSpec("FACTORY_GATE_DB_URL", "factory_api_gate", max_size=2))
    w, g = make_executor(wpool), make_executor(gpool)

    mode = "APPLY (writes verdicts)" if args.apply else "DRY RUN (writes nothing)"
    print(f"mode     : {mode}")
    ctx = await build_ctx(w, args.snapshot)
    print(f"dataset  : {ctx.dataset_version}  age={ctx.snapshot_age_hours:.2f}h  "
          f"sealed_from={ctx.sealed_from}")
    aw = await remaining(g)
    print(f"alpha    : wealth {aw['wealth_remaining']:.6f}  "
          f"next test may spend {aw['alpha_for_next']:.6f}  tested={aw['n_tested']}\n")

    rows = await w("SELECT hypothesis_id, version, spec_json FROM factory.hypotheses "
                   "ORDER BY hypothesis_id", {})
    if not rows:
        print("no hypotheses to judge")
        return 0

    print(f"loading snapshot {args.snapshot} ...")
    events, drops = exp.load_events(os.path.join(args.snapshot, "earnings_surprises.csv.gz"))
    symbols = {e["symbol"] for e in events} | {exp.BENCHMARK}
    prices, market_dates = exp.load_prices(
        os.path.join(args.snapshot, "price_points.csv.gz"), symbols)
    cas = exp.load_corporate_actions(
        os.path.join(args.snapshot, "corporate_actions.csv.gz"), symbols)
    data = (events, prices, prices.get(exp.BENCHMARK), TradingCalendar(market_dates), cas)
    print(f"events {len(events)}  sessions {len(market_dates)}\n")

    for r in rows:
        raw = r["spec_json"]
        spec = HypothesisSpec.model_validate(
            json.loads(raw) if isinstance(raw, str) else raw)
        print(f"--- {spec.id} v{spec.version}")

        # A stored hypothesis must not be refused as a duplicate of itself.
        own_ctx = dataclasses.replace(
            ctx, known_spec_hashes=without_self(ctx.known_spec_hashes, spec.id))
        compiled = compile_spec(spec, own_ctx)
        if not compiled.ok or compiled.plan is None:
            print(f"    COMPILE FAILED: {[e.code for e in compiled.errors]}")
            for e in compiled.errors[:3]:
                print(f"      {e.code} at {e.path}: {e.message[:120]}")
            continue

        try:
            res = backtest(spec, data)
        except UnbacktestableSpec as exc:
            print(f"    CANNOT EVALUATE: {exc}")
            continue
        if res is None:
            print("    no cohorts; nothing to judge")
            continue
        st = res["stats"]
        print(f"    {st.n_events} events  {st.n_cohorts} cohorts  "
              f"mean {100*st.mean:+.3f}%  t={st.tstat:.2f}  DSR={res['dsr']:.3f}")

        p = two_sided_p(st.tstat, st.n_cohorts)
        if p is None:
            print("    too few cohorts for a p-value; gate will treat it as missing")
            lord_rejected = None
        elif args.apply:
            d = await spend_and_record(g, spec.id, spec.version, p)
            lord_rejected = d.rejected
            print(f"    alpha    p={p:.4f} vs budget {d.alpha_tested:.6f} -> "
                  f"{'REJECT null' if d.rejected else 'no rejection'}  "
                  f"wealth {d.wealth_before:.6f} -> {d.wealth_after:.6f}")
        else:
            cur = await remaining(g)
            lord_rejected = p <= cur["alpha_for_next"]
            print(f"    alpha    p={p:.4f} vs budget {cur['alpha_for_next']:.6f} -> "
                  f"{'would REJECT null' if lord_rejected else 'no rejection'} (not charged)")

        by_year = st.by_year or {}
        outcome = evaluate_gate(GateInput(
            hypothesis_id=spec.id, version=spec.version, from_status=Status.SIMULATED,
            data_tier=compiled.plan.data_tier, cohort_tstat=st.tstat, cohort_mean=st.mean,
            positive_year_share=(sum(1 for v in by_year.values() if v > 0) / len(by_year))
            if by_year else None,
            n_events=st.n_events, n_cohorts=st.n_cohorts, dsr_probability=res["dsr"],
            lord_rejected=lord_rejected, critical_battery_failures=(),
            rate_card_verified=False))
        print(f"    GATE     {outcome.from_status.value} -> {outcome.to_status.value}"
              f"{'  PROMOTED' if outcome.promoted else ''}")
        for b in outcome.blocking:
            print(f"      blocked: {b}")

        if not args.apply:
            continue

        eid = await next_experiment_id(w)
        ins = await w(
            """INSERT INTO factory.experiments
                 (experiment_id, hypothesis_id, hypothesis_version, spec, spec_hash,
                  trial_count, bias_flags, constraints, created_by)
               VALUES (:e,:h,:v,:s,:sh,:tc,:bf,:cn,'cycle_runner')
               ON CONFLICT (spec_hash) DO NOTHING RETURNING experiment_id""",
            {"e": eid, "h": spec.id, "v": spec.version,
             "s": json.dumps(spec.model_dump(mode="json", by_alias=True)),
             "sh": compiled.plan.spec_hash, "tc": compiled.plan.trial_count,
             "bf": json.dumps(list(compiled.plan.bias_flags)),
             "cn": json.dumps(list(compiled.plan.constraints))})
        if not ins:
            prev = await w("SELECT experiment_id FROM factory.experiments WHERE spec_hash=:sh",
                           {"sh": compiled.plan.spec_hash})
            eid = prev[0]["experiment_id"]
            print(f"    experiment {eid} already existed (same mechanics)")
        else:
            print(f"    experiment {eid} recorded")

        await w("""INSERT INTO factory.experiment_runs
                     (experiment_id, dataset_version, feature_versions, code_commit,
                      seed, window_role, status, result, result_hash)
                   VALUES (:e,:d,:fv,:c,:s,'in_sample','succeeded',:r,:rh)""",
                {"e": eid, "d": ctx.dataset_version,
                 "fv": json.dumps(compiled.plan.feature_versions),
                 "c": os.getenv("CODE_COMMIT", "unknown")[:40], "s": FACTORY_SEED,
                 "r": json.dumps(res["payload"]), "rh": res["result_hash"]})

        dec = await g(
            """INSERT INTO factory.gate_decisions
                 (hypothesis_id, version, from_status, to_status, gate_version,
                  inputs, rule_results, decided_by)
               VALUES (:h,:v,:f,:t,:gv,:i,:rr,'cycle_runner') RETURNING decision_id""",
            {"h": spec.id, "v": spec.version, "f": outcome.from_status.value,
             "t": outcome.to_status.value, "gv": outcome.gate_version,
             "i": json.dumps({"dataset_version": ctx.dataset_version,
                              "experiment_id": eid, "p_value": p}),
             "rr": json.dumps([r.as_dict() for r in outcome.rule_results])})
        did = dec[0]["decision_id"] if dec else None
        await g("""INSERT INTO factory.hypothesis_status
                     (hypothesis_id, version, status, last_decision_id, updated_at)
                   VALUES (:h,:v,:s,:d, now())
                   -- PK is hypothesis_id ALONE, not (hypothesis_id, version):
                   -- a hypothesis holds one ladder position, and `version`
                   -- records which version reached it.
                   ON CONFLICT (hypothesis_id)
                   DO UPDATE SET version=EXCLUDED.version,
                                 status=EXCLUDED.status,
                                 last_decision_id=EXCLUDED.last_decision_id,
                                 updated_at=now()""",
                {"h": spec.id, "v": spec.version, "s": outcome.to_status.value, "d": did})
        print(f"    verdict recorded as decision {did}, status {outcome.to_status.value}")

    await wpool.close(); await gpool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
