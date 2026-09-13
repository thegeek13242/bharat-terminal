"""Execute a research cycle, with or without DBOS.

If `dbos` is importable the steps are registered as durable steps and the cycle
becomes crash-recoverable; if not, the identical sequence runs in-process. The
research result is the same either way — durability changes where the cycle can
resume from, never what it computes.

A cycle stops at the first failed step. There is no "continue anyway" path:
a compile failure or an empty study means there is nothing to be statistically
confident about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bharat_terminal.factory.workflows.steps import (
    CycleContext,
    StepOutcome,
    step_backtest,
    step_compile,
    step_gate,
    step_statistics,
)

try:  # pragma: no cover - exercised only where dbos is installed
    from dbos import DBOS  # type: ignore
    DBOS_AVAILABLE = True
except Exception:  # ImportError, or DBOS present but unconfigured
    DBOS = None  # type: ignore
    DBOS_AVAILABLE = False


@dataclass
class CycleResult:
    hypothesis_id: str
    experiment_id: str
    steps: List[StepOutcome] = field(default_factory=list)
    promoted: bool = False
    to_status: Optional[str] = None
    result_hash: Optional[str] = None
    durable: bool = False

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    @property
    def failed_step(self) -> Optional[str]:
        for s in self.steps:
            if not s.ok:
                return s.name
        return None

    def summary(self) -> str:
        head = (f"{self.experiment_id} ({self.hypothesis_id}) "
                f"{'durable' if self.durable else 'in-process'}")
        lines = [head]
        for s in self.steps:
            mark = "ok  " if s.ok else "FAIL"
            lines.append(f"  [{mark}] {s.name:<11} {s.elapsed_s:>6.3f}s  {s.detail}")
        return "\n".join(lines)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "experiment_id": self.experiment_id,
            "promoted": self.promoted,
            "to_status": self.to_status,
            "result_hash": self.result_hash,
            "durable": self.durable,
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail,
                       "elapsed_s": s.elapsed_s} for s in self.steps],
        }


def run_research_cycle(ctx: CycleContext) -> CycleResult:
    """compile -> backtest -> statistics -> gate. Stops at the first failure."""
    result = CycleResult(hypothesis_id=ctx.spec.id, experiment_id=ctx.experiment_id,
                         durable=DBOS_AVAILABLE)

    compiled = step_compile(ctx)
    result.steps.append(compiled)
    if not compiled.ok:
        return result
    plan = compiled.data["plan"]

    bt = step_backtest(ctx, plan)
    result.steps.append(bt)
    if not bt.ok:
        return result

    stats_step = step_statistics(ctx, bt.data["cohorts"], plan)
    result.steps.append(stats_step)
    if not stats_step.ok:
        return result
    result.result_hash = stats_step.data["result_hash"]

    gate_step = step_gate(ctx, stats_step.data["stats"],
                          stats_step.data["dsr"].probability, plan)
    result.steps.append(gate_step)
    outcome = gate_step.data["outcome"]
    result.promoted = outcome.promoted
    result.to_status = outcome.to_status.value
    return result


def register_with_dbos() -> bool:
    """Register the cycle as a durable DBOS workflow, if DBOS is present.

    Deliberately explicit rather than automatic at import: a research cycle
    should not silently become durable (or silently not) depending on which
    image it happens to run in.
    """
    if not DBOS_AVAILABLE:
        return False
    DBOS.workflow()(run_research_cycle)  # type: ignore[union-attr]
    return True
