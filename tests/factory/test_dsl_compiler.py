"""Compiler rules — one test per error code.

These encode the project's verified data hazards. If one of these ever starts
failing, the factory has lost a guard rail that exists because of a specific
incident (see RESEARCH_FACTORY_SPEC_v2 §1.1).
"""
from __future__ import annotations

from datetime import date

import pytest

from bharat_terminal.factory.dsl.compiler import (
    Embargo,
    EventCounts,
    compile_spec,
)
from bharat_terminal.factory.dsl.hashing import spec_hash
from tests.factory.conftest import make_ctx, make_spec


def codes(result) -> set:
    return {e.code for e in result.errors}


# ── the happy path ───────────────────────────────────────────────────────

def test_baseline_spec_compiles(spec, ctx):
    result = compile_spec(spec, ctx)
    assert result.ok, [str(e) for e in result.errors]
    assert result.plan is not None
    assert result.plan.data_tier == "H"
    assert result.plan.holdout == "sealed_h_v1"
    assert result.plan.study_start == date(2020, 1, 1)
    assert result.plan.feature_versions == {"sue_composite@2.1": "2.1"}
    # Survivorship is unavoidable today and must always be recorded.
    assert "survivorship" in result.plan.bias_flags
    assert "exclude_ca_windows" in result.plan.constraints
    # E006 cannot be judged without data access.
    assert "E006" in result.checks_deferred


def test_clock_lag_flag_marks_entry_conservative(spec, ctx):
    result = compile_spec(spec, ctx)
    assert "ts_late" in result.plan.bias_flags
    assert "entry_timing_conservative" in result.plan.constraints


def test_no_integrity_flag_means_no_bias_marker(spec):
    result = compile_spec(spec, make_ctx(integrity_flags=frozenset()))
    assert result.ok
    assert "ts_late" not in result.plan.bias_flags


# ── E001..E015 ───────────────────────────────────────────────────────────

def test_e001_unversioned_feature_is_refused(ctx):
    result = compile_spec(make_spec(signal={"feature": "sue_composite", "direction": "long"}), ctx)
    assert "E001" in codes(result)


def test_e001_unknown_feature(ctx):
    result = compile_spec(make_spec(signal={"feature": "alpha_juice@9.9", "direction": "long"}), ctx)
    assert "E001" in codes(result)


def test_e002_feature_not_pit_for_clock(ctx):
    # impact_magnitude only exists once the pipeline has analysed the filing.
    result = compile_spec(
        make_spec(signal={"feature": "impact_magnitude@1.0", "direction": "long"}), ctx
    )
    assert "E002" in codes(result)


def test_e003_intraday_rule_on_date_clock(ctx):
    result = compile_spec(make_spec(entry={"rule": "t_plus_minutes", "delay_minutes": 30}), ctx)
    assert "E003" in codes(result)


def test_e003_delay_on_date_clock(ctx):
    result = compile_spec(make_spec(entry={"rule": "next_session_open", "delay_minutes": 15}), ctx)
    assert "E003" in codes(result)


def test_e004_same_session_entry_needs_timestamp_and_cutoff(ctx):
    result = compile_spec(
        make_spec(
            event={"clock": "exchange_first_seen"},
            entry={"rule": "same_session_close_if_before", "before_time": None},
        ),
        ctx,
    )
    assert "E004" in codes(result)


def test_e005_medium_horizon_needs_long_holding(ctx):
    result = compile_spec(
        make_spec(
            horizon="medium",
            exit={"holding_days": [5], "stop": "none"},
            statistics={"primary_holding_days": 5},
        ),
        ctx,
    )
    assert "E005" in codes(result)


def test_e005_intraday_research_has_no_history(ctx):
    result = compile_spec(
        make_spec(
            horizon="intraday",
            event={"clock": "exchange_first_seen"},
            entry={"rule": "t_plus_minutes", "delay_minutes": 15},
            exit={"holding_days": [1], "stop": "none", "squareoff_intraday": True},
            statistics={"primary_holding_days": 1},
        ),
        ctx,
    )
    assert "E005" in codes(result)


def test_e006_insufficient_events_and_cohorts():
    ctx = make_ctx(event_counter=lambda s: EventCounts(events=120, cohorts=5, symbols=40))
    result = compile_spec(make_spec(), ctx)
    assert "E006" in codes(result)
    assert len([e for e in result.errors if e.code == "E006"]) == 2


def test_e006_passes_with_enough_data():
    ctx = make_ctx(event_counter=lambda s: EventCounts(events=1800, cohorts=250, symbols=600))
    result = compile_spec(make_spec(), ctx)
    assert result.ok, [str(e) for e in result.errors]
    assert result.plan.counts.events == 1800


def test_e007_low_coverage_control_refused(ctx):
    # sector_nse is 18.8% filled (verified) — below the 80% floor.
    result = compile_spec(make_spec(controls=["sector_nse"]), ctx)
    assert "E007" in codes(result)


def test_e007_unmeasured_coverage_control_refused(ctx, monkeypatch):
    """The rule, not the registry's current contents.

    This used to point at sector_bse because it happened to be unmeasured. On
    2026-09-17 it was measured at 85.0% and started passing, so the test went
    green while testing nothing. An unmeasured control is now synthesised, so
    the rule stays covered no matter what the registry says.
    """
    import dataclasses
    from bharat_terminal.factory.registry import features as reg

    unmeasured = dataclasses.replace(reg.REGISTRY["sector_bse@1.0"], coverage=None)
    monkeypatch.setitem(reg.REGISTRY, "sector_bse@1.0", unmeasured)

    result = compile_spec(make_spec(controls=["sector_bse"]), ctx)
    assert "E007" in codes(result)
    assert any("UNMEASURED" in e.message for e in result.errors)


def test_sector_bse_is_now_an_accepted_control(ctx):
    """Measured at 85.0% on 2026-09-17, so it clears the 80% floor.

    sector_nse (18.8%) remains refused, which is the whole reason this one was
    worth measuring: it is the only sector control the compiler will accept.
    """
    result = compile_spec(make_spec(controls=["sector_bse"]), ctx)
    assert "E007" not in codes(result)


def test_e007_non_control_feature_refused(ctx):
    result = compile_spec(make_spec(controls=["sue_composite"]), ctx)
    assert "E007" in codes(result)


def test_e008_numeric_operator_on_enum_feature(ctx):
    result = compile_spec(
        make_spec(
            event={
                "clock": "exchange_first_seen",
                "filters": [
                    {
                        "feature": "event_type_v2@1.0",
                        "op": "decile_ge",
                        "value": 10,
                        "reference": "trailing_365d",
                    }
                ],
            }
        ),
        ctx,
    )
    assert "E008" in codes(result)


def test_e008_decile_out_of_range_and_missing_reference(ctx):
    result = compile_spec(
        make_spec(
            event={
                "filters": [
                    {"feature": "sue_composite@2.1", "op": "decile_ge", "value": 42}
                ]
            }
        ),
        ctx,
    )
    msgs = [e.message for e in result.errors if e.code == "E008"]
    assert any("1..10" in m for m in msgs)
    assert any("reference" in m for m in msgs)


def test_e009_filter_reaching_into_sealed_holdout(ctx):
    result = compile_spec(
        make_spec(
            event={
                "filters": [
                    {"feature": "sue_composite@2.1", "op": "ge", "value": "2026-01-15"}
                ]
            }
        ),
        ctx,
    )
    assert "E009" in codes(result)


def test_embargo_becomes_a_constraint_not_an_error():
    ctx = make_ctx(
        embargoes=(
            Embargo(
                start=date(2026, 10, 13),
                end=date(2026, 11, 14),
                reason="E-series prereg Q2 window",
                sources=("earnings_surprises",),
            ),
        )
    )
    result = compile_spec(make_spec(), ctx)
    assert result.ok
    assert any(c.startswith("exclude_window:2026-10-13") for c in result.plan.constraints)
    assert any("embargo active" in w for w in result.warnings)


def test_e010_duplicate_spec_hash():
    ctx = make_ctx(known_spec_hashes={spec_hash(make_spec()): "EXP-000001"})
    result = compile_spec(make_spec(), ctx)
    assert "E010" in codes(result)
    assert result.duplicate_of == "EXP-000001"


def test_e011_overnight_short_needs_fno_universe(ctx):
    result = compile_spec(make_spec(signal={"feature": "sue_composite@2.1", "direction": "short"}), ctx)
    assert "E011" in codes(result)


def test_e011_intraday_short_is_allowed_with_squareoff(ctx):
    result = compile_spec(
        make_spec(
            signal={"feature": "sue_composite@2.1", "direction": "short"},
            exit={"holding_days": [1], "stop": "none", "squareoff_intraday": True},
            statistics={"primary_holding_days": 1},
        ),
        ctx,
    )
    assert "E011" not in codes(result)


def test_e012_sweep_over_family_trial_budget():
    ctx = make_ctx(family_trials_remaining=2)
    result = compile_spec(
        make_spec(sweep={"universe.min_median_traded_value_cr": [1, 2, 5],
                         "statistics.primary_holding_days": [10, 20]}),
        ctx,
    )
    assert "E012" in codes(result)


def test_e013_universe_must_exclude_fund_schemes(ctx):
    result = compile_spec(make_spec(universe={"exclude": []}), ctx)
    assert "E013" in codes(result)


def test_e014_benchmark_history_gap(ctx):
    result = compile_spec(make_spec(benchmark="mid150bees"), ctx)
    assert "E014" in codes(result)


def test_e015_stale_snapshot_fails_closed():
    result = compile_spec(make_spec(), make_ctx(snapshot_age_hours=48.0))
    assert "E015" in codes(result)


def test_e015_integrity_failure_blocks_everything():
    result = compile_spec(make_spec(), make_ctx(integrity_failures=frozenset({"price_row_drop"})))
    assert "E015" in codes(result)


# ── tier F normalization ─────────────────────────────────────────────────

def test_tier_f_forces_forward_validation(ctx):
    result = compile_spec(
        make_spec(
            event={"source": "impact_events", "clock": "analysis_ready",
                   "filters": [{"feature": "impact_magnitude@1.0", "op": "ge", "value": 4}]},
            signal={"feature": "impact_magnitude@1.0", "direction": "long"},
            exit={"holding_days": [5], "stop": "none"},
            statistics={"primary_holding_days": 5},
            controls=[],
        ),
        ctx,
    )
    assert result.ok, [str(e) for e in result.errors]
    assert result.plan.data_tier == "F"
    assert result.plan.holdout == "forward_only"
    assert "shadow_required_before_paper" in result.plan.constraints
    assert "no_historical_llm_replay" in result.plan.constraints
    assert any("SHADOW" in w for w in result.warnings)


def test_tier_f_minimums_are_lower_but_still_enforced():
    ctx = make_ctx(event_counter=lambda s: EventCounts(events=40, cohorts=9, symbols=20))
    result = compile_spec(
        make_spec(
            event={"source": "impact_events", "clock": "analysis_ready",
                   "filters": [{"feature": "impact_magnitude@1.0", "op": "ge", "value": 4}]},
            signal={"feature": "impact_magnitude@1.0", "direction": "long"},
            exit={"holding_days": [5], "stop": "none"},
            statistics={"primary_holding_days": 5},
            controls=[],
        ),
        ctx,
    )
    # 40 events is below the Tier-F floor of 100, but 9 cohorts clears the
    # Tier-F cohort floor of 4.
    assert "E006" in codes(result)
    assert len([e for e in result.errors if e.code == "E006"]) == 1


# ── model-level validation (pydantic, before the compiler) ───────────────

def test_primary_horizon_must_be_tested():
    with pytest.raises(ValueError):
        make_spec(statistics={"primary_holding_days": 40})


def test_holding_days_outside_the_allowed_grid_are_refused():
    with pytest.raises(ValueError):
        make_spec(exit={"holding_days": [7], "stop": "none"})


def test_stop_requires_its_parameter():
    with pytest.raises(ValueError):
        make_spec(exit={"holding_days": [5], "stop": "vol_trail"})


def test_sweep_cannot_exceed_the_point_cap():
    with pytest.raises(ValueError):
        make_spec(sweep={"a": list(range(5)), "b": list(range(6))})  # 30 > 24


def test_unknown_field_is_refused():
    with pytest.raises(ValueError):
        make_spec(hyperparameters={"lr": 0.1})


def test_declared_controls_are_flagged_as_not_applied(ctx):
    """Controls pass E007 and never reach the engine, so the result is biased.

    Verified 2026-09-17: StudyConfig has no notion of controls, so a spec
    declaring them gets an uncontrolled study. Refusing would halt every
    existing line of research including the validated incumbent, so the
    divergence is recorded on the result rather than hidden.
    """
    result = compile_spec(make_spec(controls=["sector_bse"]), ctx)
    assert result.ok, [e.message for e in result.errors]
    assert "controls_not_applied" in result.plan.bias_flags


def test_a_spec_without_controls_carries_no_such_flag(ctx):
    result = compile_spec(make_spec(controls=[]), ctx)
    assert "controls_not_applied" not in result.plan.bias_flags
