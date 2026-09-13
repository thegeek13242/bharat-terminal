"""Durable research workflows.

DBOS decorates ordinary functions to make them durable; the same functions run
perfectly well without it. So the workflow bodies live in `steps.py` as plain
callables, and `runner.py` executes them either through DBOS (when installed)
or sequentially in-process (when not). That keeps the whole research cycle
unit-testable in the existing images, and makes the DBOS dependency a
deployment choice rather than a design commitment.

Nothing here writes to the database unless explicitly told to: `LedgerWriter`
defaults to dry-run and prints the rows it would insert.
"""
from __future__ import annotations

from bharat_terminal.factory.workflows.ledger import LedgerWriter, PendingWrite
from bharat_terminal.factory.workflows.runner import (
    CycleResult,
    StepOutcome,
    run_research_cycle,
)
from bharat_terminal.factory.workflows.steps import (
    CycleContext,
    step_compile,
    step_backtest,
    step_gate,
    step_statistics,
)

__all__ = [
    "CycleContext",
    "CycleResult",
    "LedgerWriter",
    "PendingWrite",
    "StepOutcome",
    "run_research_cycle",
    "step_backtest",
    "step_compile",
    "step_gate",
    "step_statistics",
]
