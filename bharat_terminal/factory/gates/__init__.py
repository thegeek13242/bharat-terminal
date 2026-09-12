"""Promotion gates — the only place a hypothesis changes status.

An LLM may recommend; only this package decides. Everything here is a pure
function over recorded evidence, so any decision in the ledger can be recomputed
later from its inputs and must come out the same.
"""
from __future__ import annotations

from bharat_terminal.factory.gates.engine import (
    GateInput,
    GateOutcome,
    GateThresholds,
    RuleResult,
    Status,
    evaluate_gate,
    family_pause_check,
    next_status,
)

__all__ = [
    "GateInput",
    "GateOutcome",
    "GateThresholds",
    "RuleResult",
    "Status",
    "evaluate_gate",
    "family_pause_check",
    "next_status",
]
