"""Paper/shadow feedback: compare what we expected with what happened.

A strategy that underperforms is not automatically a bad strategy — it may be
a bad implementation, a stale feature, a costlier market, or a different
regime. Guessing which is how research gets abandoned for the wrong reason, so
the classification is deterministic and ordered.
"""
from __future__ import annotations

from bharat_terminal.factory.feedback.classifier import (
    DivergenceOutcome,
    DivergenceThresholds,
    Realized,
    SpecReplay,
    classify_divergence,
    followup_question,
)
from bharat_terminal.factory.feedback.envelope import Envelope, envelope_breach

__all__ = [
    "DivergenceOutcome",
    "DivergenceThresholds",
    "Envelope",
    "Realized",
    "SpecReplay",
    "classify_divergence",
    "envelope_breach",
    "followup_question",
]
