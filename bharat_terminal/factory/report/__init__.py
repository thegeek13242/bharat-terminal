"""The 24-hour research report.

Three rules shape this package:

1. **Numbers come from the ledger, never from an agent.** `facts.py` builds a
   typed payload; the renderer only formats it.
2. **Narrative is reconciled before it ships.** Every number and every ID in
   agent prose must resolve against the facts, or the sentence is dropped
   (`reconcile.py`). The pattern is borrowed from
   `bharat_terminal/outreach/checks.py`, which exists because a first-impression
   email must not contain a wrong figure.
3. **A quiet day is reported as a quiet day.** Low activity is stated, never
   padded — v1 spec §24: "If little research occurred, explicitly state that
   research activity was low rather than inventing conclusions."

Delivery is deliberately inert while AWS is on hold: `deliver.py` builds the
payload and refuses to send unless handed an explicit transport.
"""
from __future__ import annotations

from bharat_terminal.factory.report.facts import (
    ReportFacts,
    Scorecard,
    build_facts,
    compute_action_required,
)
from bharat_terminal.factory.report.reconcile import (
    ReconcileResult,
    reconcile_narrative,
)
from bharat_terminal.factory.report.render import render_text

__all__ = [
    "ReconcileResult",
    "ReportFacts",
    "Scorecard",
    "build_facts",
    "compute_action_required",
    "reconcile_narrative",
    "render_text",
]
