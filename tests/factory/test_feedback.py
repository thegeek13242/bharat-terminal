"""Divergence classification.

The ordering is the substance: a strategy must not be written off as a failed
finding while a mechanical explanation is still standing.
"""
from __future__ import annotations

import pytest

from bharat_terminal.factory.feedback.classifier import (
    COUNTERFACTUAL_RULE,
    DivergenceThresholds,
    Realized,
    SpecReplay,
    classify_divergence,
    followup_question,
)
from bharat_terminal.factory.feedback.envelope import (
    Envelope,
    envelope_breach,
    regime_z,
    slippage_ratio,
)

ENV = Envelope(
    expected_mean=0.012, ci_low=0.004, ci_high=0.020,
    expected_hit_rate=0.56, modelled_slippage_bps=10.0,
    expected_holding_days=5,
    regime_means={"results_season": 0.015, "off_season": 0.006},
)
CLEAN = SpecReplay(trades_total=120)


def ok(**kw) -> Realized:
    base = dict(n_events=120, trading_days=45, mean=0.011, sd=0.03, slippage_bps=11.0)
    base.update(kw)
    return Realized(**base)


# ── envelope helpers ─────────────────────────────────────────────────────

def test_envelope_rejects_inverted_interval():
    with pytest.raises(ValueError):
        Envelope(expected_mean=0.01, ci_low=0.02, ci_high=0.01)


def test_envelope_breach_boundaries():
    assert not envelope_breach(ENV, 0.004)
    assert not envelope_breach(ENV, 0.020)
    assert envelope_breach(ENV, 0.0039)
    assert envelope_breach(ENV, -0.01)


def test_slippage_ratio_and_regime_z():
    assert slippage_ratio(ENV, 15.0) == 1.5
    z = regime_z(ENV, "off_season", realized_mean=0.000, realized_sd=0.03, n=100)
    assert z is not None and z < 0
    assert regime_z(ENV, "unknown_cell", 0.0, 0.03, 100) is None


# ── the ordering ─────────────────────────────────────────────────────────

def test_clean_record_is_within_tolerance():
    out = classify_divergence(ENV, ok(), CLEAN)
    assert out.classification == "within_tolerance"
    assert out.action == "none"
    assert followup_question("s", 1, out, ok()) is None


def test_implementation_bug_wins_over_everything_else():
    """Even a catastrophic return must not be blamed on the research while the
    engine is demonstrably not running the spec."""
    replay = SpecReplay(trades_total=134, exit_rule_violations=97)
    out = classify_divergence(ENV, ok(n_events=134, mean=-0.0143), replay)
    assert out.classification == "implementation_bug"
    assert out.action == "pause"
    kinds = {e["kind"] for e in out.evidence}
    assert {"violation_share", "exit_rule_violations"} <= kinds


def test_orderwin_long_followup_demands_the_full_counterfactual():
    replay = SpecReplay(trades_total=134, exit_rule_violations=97)
    realized = ok(n_events=134, mean=-0.0143)
    out = classify_divergence(ENV, realized, replay)
    q = followup_question("orderwin_long", 1, out, realized)
    assert "exit rule" in q
    assert COUNTERFACTUAL_RULE in q
    assert "never compare" in q


def test_small_violation_share_does_not_trigger_implementation_bug():
    replay = SpecReplay(trades_total=120, stale_fill_count=2)   # 1.7%
    assert classify_divergence(ENV, ok(), replay).classification == "within_tolerance"


def test_data_mismatch_outranks_execution_and_decay():
    out = classify_divergence(
        ENV, ok(feature_mismatch_share=0.20, slippage_bps=30.0, rolling_slope_t=-5.0), CLEAN)
    assert out.classification == "data_mismatch"
    assert out.action == "pause"


def test_execution_error_on_slippage():
    out = classify_divergence(ENV, ok(slippage_bps=16.0), CLEAN)
    assert out.classification == "execution_error"
    assert out.action == "followup"


def test_execution_error_on_fill_timing():
    replay = SpecReplay(trades_total=120, fill_timing_violations=4)
    out = classify_divergence(ENV, ok(), replay)
    assert out.classification == "execution_error"


def test_slippage_within_tolerance_is_not_an_error():
    assert classify_divergence(ENV, ok(slippage_bps=15.0), CLEAN).classification == "within_tolerance"


def test_model_drift_requires_both_a_change_and_a_breach():
    drifted = ok(mean=-0.002, upstream_artifact_changed=True)
    assert classify_divergence(ENV, drifted, CLEAN).classification == "model_drift"
    # A version change with performance still inside the envelope is not drift.
    fine = ok(mean=0.011, upstream_artifact_changed=True)
    assert classify_divergence(ENV, fine, CLEAN).classification == "within_tolerance"


def test_regime_change_detected_when_concentrated():
    out = classify_divergence(
        ENV,
        ok(mean=0.0005, regime_cell="off_season", regime_mean=-0.004, regime_n=80),
        CLEAN,
    )
    assert out.classification == "regime_change"
    assert any(e["kind"] == "regime_z" for e in out.evidence)


def test_signal_decay_when_execution_is_clean():
    out = classify_divergence(ENV, ok(mean=0.0035, rolling_slope_t=-2.6), CLEAN)
    assert out.classification == "signal_decay"


def test_research_error_only_after_everything_else_is_excluded():
    out = classify_divergence(ENV, ok(n_events=90, mean=-0.004), CLEAN)
    assert out.classification == "research_error"
    assert out.action == "pause"
    assert "excluded" in out.detail


def test_thin_sample_cannot_convict_the_research():
    """Below the sample floor a breach is inconclusive, not a verdict."""
    out = classify_divergence(ENV, ok(n_events=20, mean=-0.004), CLEAN)
    assert out.classification == "within_tolerance"
    assert out.action == "none"


def test_thresholds_are_configurable_for_replay():
    strict = DivergenceThresholds(research_error_min_events=10)
    out = classify_divergence(ENV, ok(n_events=20, mean=-0.004), CLEAN, strict)
    assert out.classification == "research_error"


def test_outcome_serializes_for_the_ledger():
    d = classify_divergence(ENV, ok(slippage_bps=16.0), CLEAN).as_dict()
    assert d["classification"] == "execution_error"
    assert d["action"] == "followup"
    assert isinstance(d["evidence"], list) and d["detail"]
