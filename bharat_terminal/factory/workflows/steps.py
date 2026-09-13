"""The research cycle, as plain checkpointable steps.

Each step takes a CycleContext, returns a StepOutcome, and is pure with respect
to I/O it did not receive. DBOS wraps these with @DBOS.step when it is
installed; without DBOS they run in order. Either way the sequence, the
checkpoint boundaries and the failure semantics are identical - which is the
point: durability should not change what the research does.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence

from bharat_terminal.factory import FACTORY_SEED
from bharat_terminal.factory.dsl.compiler import CompileContext, CompileResult, compile_spec
from bharat_terminal.factory.dsl.hashing import result_hash
from bharat_terminal.factory.dsl.models import HypothesisSpec
from bharat_terminal.factory.gates.engine import (
    GateInput,
    GateOutcome,
    Status,
    evaluate_gate,
)
from bharat_terminal.factory.stats.bootstrap import block_bootstrap_ci
from bharat_terminal.factory.stats.cohort import CohortStats, cohort_stats
from bharat_terminal.factory.stats.dsr import deflated_sharpe
from bharat_terminal.factory.workflows.ledger import LedgerWriter


@dataclass
class CycleContext:
    """Everything one research cycle needs. No globals, no ambient state."""

    spec: HypothesisSpec
    compile_ctx: CompileContext
    ledger: LedgerWriter
    # Injected so the cycle never opens a file or a socket itself: given a
    # compiled plan, return (cohorts, extras) where cohorts is a sequence of
    # (cohort_key, n_events, mean_net_return).
    backtest_fn: Callable[[HypothesisSpec, Any], Any] = None  # type: ignore[assignment]
    experiment_id: str = "EXP-000001"
    created_by: str = "human"
    lord_rejected: Optional[bool] = None
    critical_battery_failures: Sequence[str] = ()
    rate_card_verified: bool = False
    trace_id: Optional[str] = None


@dataclass
class StepOutcome:
    name: str
    ok: bool
    detail: str
    data: Dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0


def _timed(name: str, fn: Callable[[], StepOutcome]) -> StepOutcome:
    started = time.monotonic()
    out = fn()
    return StepOutcome(out.name or name, out.ok, out.detail, out.data,
                       round(time.monotonic() - started, 3))


def step_compile(ctx: CycleContext) -> StepOutcome:
    """Compile the spec. A failure here ends the cycle - by design."""
    def _run() -> StepOutcome:
        result: CompileResult = compile_spec(ctx.spec, ctx.compile_ctx)
        if not result.ok:
            return StepOutcome("compile", False,
                               "; ".join(str(e) for e in result.errors),
                               {"errors": [e.code for e in result.errors]})
        plan = result.plan
        ctx.ledger.hypothesis(
            ctx.spec.model_dump(mode="json", by_alias=True),
            ctx.spec.claim.model_dump(), ctx.spec.family, plan.data_tier, ctx.created_by)
        ctx.ledger.experiment(
            ctx.experiment_id, ctx.spec.id, ctx.spec.version,
            ctx.spec.model_dump(mode="json", by_alias=True), plan.spec_hash,
            plan.trial_count, plan.bias_flags, plan.constraints, ctx.created_by)
        return StepOutcome("compile", True,
                           f"tier={plan.data_tier} holdout={plan.holdout} "
                           f"bias={list(plan.bias_flags)}",
                           {"plan": plan, "warnings": list(result.warnings)})
    return _timed("compile", _run)


def step_backtest(ctx: CycleContext, plan) -> StepOutcome:
    """Run the study. The backtest itself is injected, so this step stays pure."""
    def _run() -> StepOutcome:
        if ctx.backtest_fn is None:
            return StepOutcome("backtest", False, "no backtest_fn injected")
        study = ctx.backtest_fn(ctx.spec, plan)
        cohorts = list(getattr(study, "cohorts", []) or [])
        if not cohorts:
            return StepOutcome("backtest", False, "study produced no cohorts",
                               {"exclusions": getattr(study, "exclusions", {})})
        return StepOutcome("backtest", True,
                           f"{getattr(study, 'events_used', 0)} events, "
                           f"{len(cohorts)} cohorts",
                           {"study": study, "cohorts": cohorts})
    return _timed("backtest", _run)


def step_statistics(ctx: CycleContext, cohorts, plan) -> StepOutcome:
    """Cohort t, bootstrap CI and deflated Sharpe - recorded, not judged."""
    def _run() -> StepOutcome:
        holding = ctx.spec.statistics.primary_holding_days
        st: CohortStats = cohort_stats(cohorts, holding_days=holding)
        means = [m for _, _, m in cohorts]
        ci = block_bootstrap_ci(means, n_resamples=10_000, seed=FACTORY_SEED)
        dsr = deflated_sharpe(means, n_trials=max(1, plan.trial_count))

        payload = {
            "n_events": st.n_events, "n_cohorts": st.n_cohorts,
            "mean": st.mean, "tstat": st.tstat, "nw_lag": st.lag,
            "positive_cohort_share": st.positive_cohort_share,
            "by_year": st.by_year,
            "ci_low": ci.lo, "ci_high": ci.hi,
            "dsr": dsr.probability, "seed": FACTORY_SEED,
        }
        rhash = result_hash(payload)
        ctx.ledger.experiment_run(
            ctx.experiment_id, payload, rhash,
            plan.feature_versions, FACTORY_SEED, "in_sample", 0.0, ctx.trace_id)
        ctx.ledger.stat_test("cohort_nw_t", payload, None, None)
        ctx.ledger.stat_test("bootstrap", {"lo": ci.lo, "hi": ci.hi}, None, None)
        ctx.ledger.stat_test("dsr", {"probability": dsr.probability},
                             None, dsr.probability >= 0.95)
        return StepOutcome("statistics", True,
                           f"mean {st.mean:+.4f} t={st.tstat:.2f} "
                           f"dsr={dsr.probability:.3f}",
                           {"stats": st, "ci": ci, "dsr": dsr,
                            "result_hash": rhash, "payload": payload})
    return _timed("statistics", _run)


def step_gate(ctx: CycleContext, stats: CohortStats, dsr_probability: float,
              plan) -> StepOutcome:
    """The only step that may change a hypothesis's status."""
    def _run() -> StepOutcome:
        gi = GateInput(
            hypothesis_id=ctx.spec.id, version=ctx.spec.version,
            from_status=Status.SIMULATED, data_tier=plan.data_tier,
            cohort_tstat=stats.tstat, cohort_mean=stats.mean,
            positive_year_share=(sum(1 for v in stats.by_year.values() if v > 0)
                                 / len(stats.by_year)) if stats.by_year else None,
            n_events=stats.n_events, n_cohorts=stats.n_cohorts,
            dsr_probability=dsr_probability,
            lord_rejected=ctx.lord_rejected,
            critical_battery_failures=tuple(ctx.critical_battery_failures),
            rate_card_verified=ctx.rate_card_verified,
        )
        outcome: GateOutcome = evaluate_gate(gi)
        ctx.ledger.gate_decision(ctx.spec.id, ctx.spec.version, outcome.as_dict())
        return StepOutcome(
            "gate", True,
            f"{outcome.from_status.value} -> {outcome.to_status.value}"
            + ("" if outcome.promoted else f" (blocked: {list(outcome.blocking)})"),
            {"outcome": outcome})
    return _timed("gate", _run)
