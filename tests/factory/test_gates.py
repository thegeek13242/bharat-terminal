"""Promotion gates.

The rule under test throughout: absent evidence is a FAILED rule, never an
assumed pass. A gate that promotes on missing data is the failure mode this
whole project exists to avoid.
"""
from __future__ import annotations

from bharat_terminal.factory.gates.engine import (
    ForwardMetrics,
    GateInput,
    GateThresholds,
    Status,
    evaluate_gate,
    family_pause_check,
    next_status,
)

TH = GateThresholds()


def blocked(outcome) -> set:
    return set(outcome.blocking)


# ── the ladders ──────────────────────────────────────────────────────────

def test_tier_h_ladder_goes_through_out_of_sample():
    assert next_status(Status.SIMULATED, "H") is Status.ROBUST
    assert next_status(Status.ROBUST, "H") is Status.OUT_OF_SAMPLE
    assert next_status(Status.OUT_OF_SAMPLE, "H") is Status.PAPER


def test_tier_f_ladder_goes_through_shadow():
    assert next_status(Status.SIMULATED, "F") is Status.SHADOW
    assert next_status(Status.SHADOW, "F") is Status.SHADOW_VERIFIED
    assert next_status(Status.SHADOW_VERIFIED, "F") is Status.PAPER


def test_production_is_not_reachable_by_any_automated_step():
    assert next_status(Status.PAPER_VERIFIED, "H") is Status.PRODUCTION_CANDIDATE
    assert next_status(Status.PRODUCTION_CANDIDATE, "H") is None
    outcome = evaluate_gate(GateInput("H-1", 1, Status.PRODUCTION_CANDIDATE, "H"))
    assert outcome.promoted is False
    assert outcome.to_status is Status.PRODUCTION_CANDIDATE


# ── SIMULATED -> ROBUST (Tier H) ─────────────────────────────────────────

def robust_input(**kw) -> GateInput:
    base = dict(
        hypothesis_id="H-000031", version=1, from_status=Status.SIMULATED, data_tier="H",
        cohort_tstat=3.6, cohort_mean=0.0142, positive_year_share=0.83,
        n_events=1742, n_cohorts=255, dsr_probability=0.97, lord_rejected=True,
    )
    base.update(kw)
    return GateInput(**base)


def test_robust_promotion_on_complete_evidence():
    out = evaluate_gate(robust_input())
    assert out.promoted and out.to_status is Status.ROBUST
    assert out.gate_version == "gates_v1"


def test_robust_blocked_on_each_threshold():
    assert "cohort_tstat" in blocked(evaluate_gate(robust_input(cohort_tstat=2.4)))
    assert "positive_year_share" in blocked(evaluate_gate(robust_input(positive_year_share=0.4)))
    assert "deflated_sharpe" in blocked(evaluate_gate(robust_input(dsr_probability=0.80)))
    assert "lord_rejected" in blocked(evaluate_gate(robust_input(lord_rejected=False)))


def test_robust_blocked_when_alpha_wealth_never_tested():
    """LORD++ not run is not the same as LORD++ passing."""
    assert "lord_rejected" in blocked(evaluate_gate(robust_input(lord_rejected=None)))


def test_missing_statistics_block_rather_than_pass():
    out = evaluate_gate(robust_input(cohort_tstat=None, dsr_probability=None))
    assert not out.promoted
    assert {"cohort_tstat", "deflated_sharpe"} <= blocked(out)


def test_critical_battery_failure_blocks_promotion():
    out = evaluate_gate(robust_input(critical_battery_failures=("cost_x2", "drop_top5")))
    assert "critical_battery_failures" in blocked(out)


def test_upheld_adversary_veto_blocks_promotion():
    assert "adversary_veto_upheld" in blocked(evaluate_gate(robust_input(adversary_veto_upheld=True)))


def test_integrity_failure_blocks_every_transition():
    assert "integrity_ok" in blocked(evaluate_gate(robust_input(integrity_ok=False)))


# ── SIMULATED -> SHADOW (Tier F) ─────────────────────────────────────────

def test_tier_f_promotes_to_shadow_without_historical_significance():
    """Tier F has months of history, so it earns forward tracking, not belief."""
    out = evaluate_gate(GateInput(
        "H-000044", 1, Status.SIMULATED, "F",
        n_events=140, cohort_tstat=None, dsr_probability=None, lord_rejected=None,
    ))
    assert out.promoted and out.to_status is Status.SHADOW


def test_tier_f_still_needs_a_minimum_sample():
    out = evaluate_gate(GateInput("H-000044", 1, Status.SIMULATED, "F", n_events=40))
    assert "n_events" in blocked(out)


# ── ROBUST -> OUT_OF_SAMPLE ──────────────────────────────────────────────

def oos_input(**kw) -> GateInput:
    base = dict(
        hypothesis_id="H-000031", version=1, from_status=Status.ROBUST, data_tier="H",
        cohort_mean=0.0142, oos_tstat=2.3, oos_mean=0.0097, oos_reproduced=True,
        holdout_already_used=False,
    )
    base.update(kw)
    return GateInput(**base)


def test_out_of_sample_promotion():
    out = evaluate_gate(oos_input())
    assert out.promoted and out.to_status is Status.OUT_OF_SAMPLE


def test_sign_flip_out_of_sample_is_fatal():
    out = evaluate_gate(oos_input(oos_mean=-0.004))
    assert "oos_same_sign" in blocked(out)


def test_collapsed_effect_size_blocks():
    out = evaluate_gate(oos_input(oos_mean=0.003))   # 21% of in-sample
    assert "oos_effect_ratio" in blocked(out)


def test_holdout_can_only_be_spent_once():
    out = evaluate_gate(oos_input(holdout_already_used=True))
    assert "holdout_available" in blocked(out)


def test_unreproduced_result_blocks():
    assert "reproduced_on_fresh_snapshot" in blocked(evaluate_gate(oos_input(oos_reproduced=False)))


# ── SHADOW -> SHADOW_VERIFIED ────────────────────────────────────────────

def test_shadow_verified_needs_forward_evidence():
    out = evaluate_gate(GateInput("H-000044", 1, Status.SHADOW, "F", forward=None))
    assert not out.promoted
    assert "forward_events" in blocked(out)


def test_shadow_verified_bar_is_higher_than_paper():
    forward = ForwardMetrics(events=120, trading_days=45, net_mean=0.006,
                             net_tstat=2.2, realized_slippage_bps=18,
                             modelled_slippage_bps=15)
    out = evaluate_gate(GateInput("H-000044", 1, Status.SHADOW, "F",
                                  forward=forward, lord_rejected=True))
    # t=2.2 clears the paper bar (2.0) but not the shadow bar (2.5).
    assert "forward_tstat" in blocked(out)
    assert TH.shadow_min_t > TH.paper_min_t


def test_shadow_verified_promotion():
    forward = ForwardMetrics(events=120, trading_days=45, net_mean=0.006,
                             net_tstat=2.8, realized_slippage_bps=18,
                             modelled_slippage_bps=15)
    out = evaluate_gate(GateInput("H-000044", 1, Status.SHADOW, "F",
                                  forward=forward, lord_rejected=True))
    assert out.promoted and out.to_status is Status.SHADOW_VERIFIED


# ── entering PAPER ───────────────────────────────────────────────────────

def paper_entry(**kw) -> GateInput:
    base = dict(
        hypothesis_id="H-000031", version=1, from_status=Status.OUT_OF_SAMPLE, data_tier="H",
        tradable=True, concurrent_paper=2, family_paper_count=0, rate_card_verified=True,
    )
    base.update(kw)
    return GateInput(**base)


def test_paper_entry_promotion():
    assert evaluate_gate(paper_entry()).promoted


def test_unverified_cost_rates_cannot_reach_paper():
    """NSE's levies page was unreachable, so the rate cards ship unverified."""
    out = evaluate_gate(paper_entry(rate_card_verified=False))
    assert "rate_card_verified" in blocked(out)


def test_capacity_caps_are_enforced():
    assert "concurrent_paper" in blocked(evaluate_gate(paper_entry(concurrent_paper=6)))
    assert "family_paper_count" in blocked(evaluate_gate(paper_entry(family_paper_count=2)))


def test_untradable_strategy_blocked():
    assert "tradable" in blocked(evaluate_gate(paper_entry(tradable=False)))


# ── PAPER -> PAPER_VERIFIED ──────────────────────────────────────────────

def paper_verify(**kw) -> GateInput:
    forward = kw.pop("forward", ForwardMetrics(
        events=120, trading_days=48, net_mean=0.0061, net_tstat=2.1,
        realized_slippage_bps=22, modelled_slippage_bps=15,
        divergence_class="within_tolerance"))
    base = dict(
        hypothesis_id="H-000031", version=1, from_status=Status.PAPER, data_tier="H",
        forward=forward, rate_card_verified=True,
    )
    base.update(kw)
    return GateInput(**base)


def test_paper_verified_promotion():
    out = evaluate_gate(paper_verify())
    assert out.promoted and out.to_status is Status.PAPER_VERIFIED


def test_slippage_beyond_one_and_a_half_times_model_blocks():
    forward = ForwardMetrics(events=120, trading_days=48, net_mean=0.006, net_tstat=2.1,
                             realized_slippage_bps=30, modelled_slippage_bps=15,
                             divergence_class="within_tolerance")
    assert "slippage_ratio" in blocked(evaluate_gate(paper_verify(forward=forward)))


def test_research_error_divergence_blocks():
    forward = ForwardMetrics(events=120, trading_days=48, net_mean=0.006, net_tstat=2.1,
                             realized_slippage_bps=18, modelled_slippage_bps=15,
                             divergence_class="research_error")
    assert "divergence_class" in blocked(evaluate_gate(paper_verify(forward=forward)))


def test_orderwin_long_as_observed_would_be_refused():
    """The live case, with its verified numbers: 134 closed trades averaging
    -1.43% at t = -3.9. The gate must refuse it on sign, t-stat and class."""
    forward = ForwardMetrics(
        events=134, trading_days=45, net_mean=-0.0143, net_tstat=-3.9,
        realized_slippage_bps=10, modelled_slippage_bps=10,
        divergence_class="implementation_bug",
    )
    out = evaluate_gate(paper_verify(forward=forward))
    assert not out.promoted
    assert {"paper_tstat", "paper_net_positive", "divergence_class"} <= blocked(out)


# ── family kill rule ─────────────────────────────────────────────────────

def test_family_continues_below_the_failure_limit():
    paused, _ = family_pause_check(2)
    assert paused is False


def test_family_pauses_after_repeated_failures():
    paused, reason = family_pause_check(3)
    assert paused is True
    assert "no new feature version" in reason


def test_prose_alone_cannot_reopen_a_family():
    """Only a concrete new reference reopens it — an eloquent memo does not."""
    assert family_pause_check(5, new_mechanism_refs=[])[0] is True
    assert family_pause_check(5, new_mechanism_refs=["sue_revenue@2.2"])[0] is False


# ── ledger serialization ─────────────────────────────────────────────────

def test_outcome_serializes_every_rule_for_the_ledger():
    d = evaluate_gate(robust_input()).as_dict()
    assert d["from_status"] == "SIMULATED" and d["to_status"] == "ROBUST"
    assert d["gate_version"] == "gates_v1"
    names = {r["rule"] for r in d["rule_results"]}
    assert {"cohort_tstat", "deflated_sharpe", "lord_rejected"} <= names
    assert all({"rule", "value", "threshold", "pass"} <= set(r) for r in d["rule_results"])
