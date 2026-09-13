"""The research cycle: sequencing, ledger dry-run, and stop-on-failure."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence

import pytest

from bharat_terminal.factory.workflows.ledger import LedgerWriter
from bharat_terminal.factory.workflows.runner import run_research_cycle
from bharat_terminal.factory.workflows.steps import CycleContext
from tests.factory.conftest import make_ctx, make_spec


@dataclass
class FakeStudy:
    cohorts: Sequence
    events_used: int = 0
    exclusions: Dict[str, int] = None

    def __post_init__(self):
        self.exclusions = self.exclusions or {}


def strong_cohorts(n: int = 40, mean: float = 0.02) -> List:
    """Cohorts with a clear positive effect and little noise."""
    return [(f"2024-{(i % 12) + 1:02d}-0{(i % 4) + 1}", 12,
             mean + (0.002 if i % 2 else -0.002)) for i in range(n)]


def weak_cohorts(n: int = 40) -> List:
    return [(f"2024-{(i % 12) + 1:02d}-0{(i % 4) + 1}", 12,
             0.02 if i % 2 else -0.02) for i in range(n)]


def build(cohorts, **kw) -> CycleContext:
    ledger = LedgerWriter(dataset_version="DS-20260912-01", code_commit="abc123")
    defaults = dict(
        spec=make_spec(),
        compile_ctx=make_ctx(),
        ledger=ledger,
        backtest_fn=lambda spec, plan: FakeStudy(cohorts=cohorts, events_used=480),
        lord_rejected=True,
        rate_card_verified=False,
    )
    defaults.update(kw)
    return CycleContext(**defaults)


# ── sequencing ───────────────────────────────────────────────────────────

def test_full_cycle_runs_all_four_steps():
    result = run_research_cycle(build(strong_cohorts()))
    assert [s.name for s in result.steps] == ["compile", "backtest", "statistics", "gate"]
    assert result.ok
    assert result.result_hash


def test_cycle_is_reproducible():
    a = run_research_cycle(build(strong_cohorts()))
    b = run_research_cycle(build(strong_cohorts()))
    assert a.result_hash == b.result_hash


def test_compile_failure_stops_the_cycle_immediately():
    ctx = build(strong_cohorts(), spec=make_spec(controls=["sector_nse"]))
    result = run_research_cycle(ctx)
    assert not result.ok
    assert result.failed_step == "compile"
    assert len(result.steps) == 1          # no backtest was attempted


def test_empty_study_stops_before_statistics():
    ctx = build([], backtest_fn=lambda spec, plan: FakeStudy(cohorts=[]))
    result = run_research_cycle(ctx)
    assert result.failed_step == "backtest"
    assert len(result.steps) == 2


def test_missing_backtest_function_fails_loudly():
    ctx = build(strong_cohorts(), backtest_fn=None)
    result = run_research_cycle(ctx)
    assert result.failed_step == "backtest"


# ── the gate is the only thing that promotes ─────────────────────────────

def test_weak_evidence_is_not_promoted():
    result = run_research_cycle(build(weak_cohorts()))
    assert result.ok                       # the cycle ran fine
    assert result.promoted is False        # the finding did not
    assert result.to_status == "SIMULATED"


def test_strong_evidence_promotes_to_robust():
    result = run_research_cycle(build(strong_cohorts(), lord_rejected=True))
    assert result.promoted is True
    assert result.to_status == "ROBUST"


def test_alpha_wealth_not_tested_blocks_promotion():
    result = run_research_cycle(build(strong_cohorts(), lord_rejected=None))
    assert result.promoted is False


# ── ledger stays dry ─────────────────────────────────────────────────────

def test_ledger_is_dry_run_by_default_and_writes_nothing():
    ctx = build(strong_cohorts())
    run_research_cycle(ctx)
    assert ctx.ledger.dry_run is True
    assert ctx.ledger.flush() == 0         # no-op
    assert ctx.ledger.pending              # but the rows were captured


def test_ledger_captures_every_expected_table():
    ctx = build(strong_cohorts())
    run_research_cycle(ctx)
    tables = {w.table for w in ctx.ledger.pending}
    assert tables == {
        "factory.hypotheses", "factory.experiments", "factory.experiment_runs",
        "factory.stat_tests", "factory.gate_decisions",
    }


def test_gate_decision_row_records_every_rule():
    ctx = build(strong_cohorts())
    run_research_cycle(ctx)
    row = next(w.row for w in ctx.ledger.pending if w.table == "factory.gate_decisions")
    assert row["from_status"] == "SIMULATED"
    assert row["gate_version"] == "gates_v1"
    assert "cohort_tstat" in row["rule_results"]


def test_live_writer_requires_an_explicit_executor():
    with pytest.raises(ValueError):
        LedgerWriter(dataset_version="DS-1", dry_run=False)


def test_live_writer_applies_rows_through_the_injected_executor():
    calls = []
    w = LedgerWriter(dataset_version="DS-1", dry_run=False,
                     executor=lambda sql, params: calls.append((sql, params)))
    w.stat_test("cohort_nw_t", {"t": 3.4}, None, True)
    assert w.flush() == 1
    assert calls[0][0].startswith("INSERT INTO factory.stat_tests")
    assert w.pending == []


def test_review_is_human_readable():
    ctx = build(strong_cohorts())
    run_research_cycle(ctx)
    text = ctx.ledger.review()
    assert "pending write(s)" in text
    assert "factory.gate_decisions" in text


# ── durability is a deployment choice, not a behaviour change ────────────

def test_cycle_reports_whether_it_ran_durably():
    result = run_research_cycle(build(strong_cohorts()))
    # DBOS is absent in the test image; the cycle still completes.
    assert result.durable in (True, False)
    assert result.ok


def test_summary_and_dict_are_serializable():
    result = run_research_cycle(build(strong_cohorts()))
    assert "compile" in result.summary()
    d = result.as_dict()
    assert d["experiment_id"] == "EXP-000001"
    assert len(d["steps"]) == 4
