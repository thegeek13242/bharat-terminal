"""The out-of-sample test. Consumes a one-shot resource on purpose.

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER RUNNER HERE
Everything else in the factory is safe to run repeatedly. This is not. The
sealed holdout is the only genuinely out-of-sample evidence a Tier-H hypothesis
will ever get, and it is spent by LOOKING at it: once the answer is known it
cannot be un-known, and a second look at the same window is no longer
out-of-sample for anything that was tuned in between.

SO THERE IS NO DRY MODE FOR THE ACCESS LOG
`factory.holdout_access_log` records the access unconditionally, including on
what is otherwise a read-only run. Making that conditional on --apply would
create a free peek and the log would then describe less than what happened,
which is the one thing this repository's audit history says must never be true.
--apply controls only whether a VERDICT is recorded.

RANKING ORDER
Holdout events are ranked against their own past, which includes the whole
in-sample period. See backtest.events.load_ranked_events: windowing happens
after ranking, deliberately.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import factory_exp_000001 as exp
from factory_run_cycle import backtest, build_ctx

from bharat_terminal.factory.backtest.calendar import TradingCalendar
from bharat_terminal.factory.backtest.events import HOLDOUT, IN_SAMPLE, load_ranked_events
from bharat_terminal.factory.dsl.models import HypothesisSpec
from bharat_terminal.factory.gates.engine import GateInput, Status, evaluate_gate
from bharat_terminal.factory.tools_api.pools import PoolSpec, make_executor, open_pool


async def already_used(ex, hid: str, version: int) -> bool:
    rows = await ex("""SELECT 1 FROM factory.holdout_access_log
                        WHERE hypothesis_id = :h AND version = :v LIMIT 1""",
                    {"h": hid, "v": version})
    return bool(rows)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hypothesis", required=True)
    ap.add_argument("--version", type=int, default=1)
    ap.add_argument("--snapshot", default="auto")
    ap.add_argument("--apply", action="store_true",
                    help="record a gate verdict (the ACCESS is logged either way)")
    args = ap.parse_args()

    wpool = await open_pool(PoolSpec("FACTORY_DB_URL", "factory_api", max_size=4))
    gdsn = os.getenv("FACTORY_GATE_DB_URL")
    if not gdsn:
        raise SystemExit("FACTORY_GATE_DB_URL is not set; the gate writes its own rows")
    gpool = await open_pool(PoolSpec("FACTORY_GATE_DB_URL", "factory_api_gate", max_size=2))
    w, g = make_executor(wpool), make_executor(gpool)

    ctx = await build_ctx(w, args.snapshot)
    if args.snapshot == "auto":
        args.snapshot = f"/snapshots/{ctx.dataset_version}"
    elif os.path.basename(args.snapshot.rstrip("/")) != ctx.dataset_version:
        raise SystemExit(f"--snapshot is not the ledger's current dataset ({ctx.dataset_version})")

    rows = await w("""SELECT spec_json FROM factory.hypotheses
                       WHERE hypothesis_id = :h AND version = :v""",
                   {"h": args.hypothesis, "v": args.version})
    if not rows:
        raise SystemExit(f"{args.hypothesis} v{args.version} is not in the ledger")
    raw = rows[0]["spec_json"]
    spec = HypothesisSpec.model_validate(json.loads(raw) if isinstance(raw, str) else raw)

    print(f"hypothesis : {args.hypothesis} v{args.version}  signal={spec.signal.feature} "
          f"{spec.signal.direction.value}  hold={spec.statistics.primary_holding_days}")
    print(f"dataset    : {ctx.dataset_version}  sealed_from={ctx.sealed_from}")
    print(f"snapshot   : {args.snapshot}")

    if await already_used(w, args.hypothesis, args.version):
        raise SystemExit(
            f"REFUSED: {args.hypothesis} v{args.version} has already used its holdout "
            f"access. A second look is not out-of-sample.")

    es = os.path.join(args.snapshot, "earnings_surprises.csv.gz")

    def events_for_window(win: str):
        cache: Dict[str, List[Dict[str, Any]]] = {}

        def inner(feature: str):
            if feature not in cache:
                evs, drops = load_ranked_events(es, feature, ctx.sealed_from, window=win)
                cache[feature] = evs
                print(f"  events[{win}][{feature}] = {len(evs)}")
            return cache[feature]
        return inner

    # Prices must cover both windows.
    syms = {exp.BENCHMARK}
    for win in (IN_SAMPLE, HOLDOUT):
        evs, _ = load_ranked_events(es, spec.signal.feature, ctx.sealed_from, window=win)
        syms |= {e["symbol"] for e in evs}
    prices, market_dates = exp.load_prices(
        os.path.join(args.snapshot, "price_points.csv.gz"), syms)
    cas = exp.load_corporate_actions(
        os.path.join(args.snapshot, "corporate_actions.csv.gz"), syms)
    cal, bench = TradingCalendar(market_dates), prices.get(exp.BENCHMARK)

    print("\n──── IN-SAMPLE (the result being tested, recomputed for comparability)")
    is_res = backtest(spec, (events_for_window(IN_SAMPLE), prices, bench, cal, cas))
    if is_res is None:
        raise SystemExit("in-sample study produced no cohorts; nothing to compare against")
    ist = is_res["stats"]
    print(f"  {ist.n_events} events  {ist.n_cohorts} cohorts  mean {100*ist.mean:+.3f}%  t={ist.tstat:.2f}")

    print("\n──── HOLDOUT (one shot; this is the access)")
    oos_res = backtest(spec, (events_for_window(HOLDOUT), prices, bench, cal, cas))

    access = {"h": args.hypothesis, "v": args.version,
              "win": json.dumps({"from": ctx.sealed_from.isoformat(),
                                 "dataset_version": ctx.dataset_version,
                                 "signal": spec.signal.feature})}
    # "window" is a reserved word in Postgres and must be quoted. The same bug
    # bit reads.py earlier in this build; unquoted it fails at parse time.
    await w("""INSERT INTO factory.holdout_access_log (hypothesis_id, version, "window")
               VALUES (:h, :v, CAST(:win AS jsonb))""", access)
    print("  holdout access RECORDED (logged whether or not a verdict is written)")

    if oos_res is None:
        print("  no cohorts in the holdout window; the test is inconclusive, "
              "and the access is spent regardless")
        await wpool.close(); await gpool.close()
        return 1
    ot = oos_res["stats"]
    print(f"  {ot.n_events} events  {ot.n_cohorts} cohorts  mean {100*ot.mean:+.3f}%  t={ot.tstat:.2f}")

    ratio = (abs(ot.mean) / abs(ist.mean)) if ist.mean else None

    # reproduced_on_fresh_snapshot compares the recomputed in-sample result
    # against the one recorded when the hypothesis was promoted. H-000007 has no
    # experiment_run, because its promotions were all dry -- so there is nothing
    # to reproduce AGAINST, and the honest value is None, which fails the rule.
    # Asserting True here because the numbers "look the same" would be exactly
    # the unverifiable claim this repository keeps an audit trail about.
    prior = await w("""SELECT r.result FROM factory.experiment_runs r
                         JOIN factory.experiments e USING (experiment_id)
                        WHERE e.hypothesis_id = :h AND e.hypothesis_version = :v
                        ORDER BY r.run_id DESC LIMIT 1""",
                    {"h": args.hypothesis, "v": args.version})
    reproduced: Optional[bool] = None
    if prior:
        rec = prior[0]["result"]
        rec = json.loads(rec) if isinstance(rec, str) else rec
        reproduced = (
            abs(float(rec.get("tstat", 0)) - ist.tstat) < 1e-6
            and abs(float(rec.get("mean", 0)) - ist.mean) < 1e-12
        )
        print(f"\n  reproduction vs recorded run: {'MATCH' if reproduced else 'DIVERGED'}")
    else:
        print("\n  reproduction: NO recorded experiment_run for this hypothesis "
              "(its promotions were dry), so there is nothing to reproduce against")

    print("\n──── GATE  ROBUST -> OUT_OF_SAMPLE")
    gi = GateInput(hypothesis_id=args.hypothesis, version=args.version,
                   from_status=Status.ROBUST, data_tier="H",
                   holdout_already_used=False, oos_tstat=ot.tstat, oos_mean=ot.mean,
                   cohort_mean=ist.mean, oos_reproduced=reproduced)
    dec = evaluate_gate(gi)
    for r in dec.rule_results:
        mark = "pass" if r.passed else "FAIL"
        print(f"  {mark:<5} {r.rule}: got {r.value!r} need {r.threshold!r}")
    print(f"  effect ratio: {ratio:.3f}" if ratio is not None else "  effect ratio: n/a")
    if dec.blocking:
        print(f"  blocking: {list(dec.blocking)}")
    print(f"  VERDICT: {dec.from_status.value} -> {dec.to_status.value}  "
          f"{'PROMOTED' if dec.promoted else 'HELD'}")

    if not args.apply:
        print("\n  (no verdict written: --apply not given. The ACCESS is already logged.)")
    await wpool.close(); await gpool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
