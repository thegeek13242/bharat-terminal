"""E7 — known-answer tests for the statistics that decide promotions.

If these drift, every gate decision in the ledger becomes unreliable, so a
failure here freezes the gates (spec §14).
"""
from __future__ import annotations

import math

from bharat_terminal.factory.stats.bootstrap import block_bootstrap_ci, default_block_length
from bharat_terminal.factory.stats.cohort import (
    cohort_stats,
    lag_for_holding,
    newey_west_tstat,
)
from bharat_terminal.factory.stats.dsr import (
    _norm_cdf,
    _norm_ppf,
    deflated_sharpe,
    expected_max_sharpe,
    moments,
)
from bharat_terminal.factory.stats.fdr import LordPP, benjamini_hochberg, lord_gamma
from bharat_terminal.factory.stats.placebo import placebo_percentile


# ── cohort inference ─────────────────────────────────────────────────────

def test_newey_west_lag0_is_the_plain_standard_error():
    # [1..5]: mean 3, population variance 2, se = sqrt(2/5), t = 3/se.
    mean, se, t = newey_west_tstat([1, 2, 3, 4, 5], lag=0)
    assert mean == 3.0
    assert se == math.sqrt(2.0 / 5.0)
    assert t == 3.0 / math.sqrt(2.0 / 5.0)


def test_positive_autocorrelation_widens_the_standard_error():
    """The whole point of the correction: overlapping windows inflate t."""
    series = [1, 2, 3, 4, 5, 6, 7, 8]
    _, se0, t0 = newey_west_tstat(series, lag=0)
    _, se1, t1 = newey_west_tstat(series, lag=1)
    assert se1 > se0
    assert abs(t1) < abs(t0)


def test_constant_series_cannot_manufacture_significance():
    mean, se, t = newey_west_tstat([2.0, 2.0, 2.0], lag=1)
    assert mean == 2.0
    assert se == 0.0
    assert t == 0.0


def test_empty_and_single_observation_series():
    assert newey_west_tstat([]) == (0.0, 0.0, 0.0)
    assert newey_west_tstat([5.0]) == (5.0, 0.0, 0.0)


def test_lag_scales_with_holding_period():
    assert lag_for_holding(20, cohort_days=5) == 4
    assert lag_for_holding(5, cohort_days=5) == 1
    assert lag_for_holding(1, cohort_days=5) == 1


def test_cohort_stats_aggregates_events_and_years():
    stats = cohort_stats(
        [("2024-01-01", 10, 0.01), ("2024-01-08", 12, 0.02), ("2025-01-06", 8, -0.01)],
        holding_days=20,
    )
    assert stats.n_cohorts == 3
    assert stats.n_events == 30
    assert stats.positive_cohort_share == 2 / 3
    assert stats.by_year[2024] == 0.015
    assert stats.by_year[2025] == -0.01
    assert stats.lag == 4


def test_cohort_stats_on_no_data():
    stats = cohort_stats([], holding_days=5)
    assert stats.n_cohorts == 0 and stats.tstat == 0.0


# ── bootstrap ────────────────────────────────────────────────────────────

def test_bootstrap_is_reproducible_with_the_factory_seed():
    series = [0.01, -0.02, 0.03, 0.00, 0.015, -0.005, 0.02, 0.01]
    a = block_bootstrap_ci(series, n_resamples=500)
    b = block_bootstrap_ci(series, n_resamples=500)
    assert (a.lo, a.hi) == (b.lo, b.hi)
    assert a.seed == 42


def test_bootstrap_interval_brackets_the_mean():
    series = [0.01, 0.012, 0.009, 0.011, 0.013, 0.008, 0.010, 0.012]
    ci = block_bootstrap_ci(series, n_resamples=1000)
    assert ci.lo <= ci.mean <= ci.hi
    assert ci.share_above_zero > 0.9


def test_bootstrap_block_length_rule_and_degenerate_inputs():
    assert default_block_length(27) == 3
    assert block_bootstrap_ci([], n_resamples=10).n_resamples == 0
    single = block_bootstrap_ci([0.02], n_resamples=10)
    assert single.lo == single.hi == 0.02


# ── multiple testing ─────────────────────────────────────────────────────

def test_benjamini_hochberg_known_answer():
    rejected, adjusted = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05], alpha=0.05)
    assert all(rejected)
    assert all(abs(a - 0.05) < 1e-12 for a in adjusted)


def test_benjamini_hochberg_rejects_only_the_strong_one():
    rejected, adjusted = benjamini_hochberg([0.001, 0.9], alpha=0.05)
    assert rejected == [True, False]
    assert abs(adjusted[0] - 0.002) < 1e-12


def test_benjamini_hochberg_preserves_input_order():
    rejected, adjusted = benjamini_hochberg([0.9, 0.001], alpha=0.05)
    assert rejected == [False, True]
    assert adjusted[1] < adjusted[0]


def test_benjamini_hochberg_empty():
    assert benjamini_hochberg([]) == ([], [])


def test_lord_gamma_decays():
    assert lord_gamma(1) > lord_gamma(10) > lord_gamma(100)


def test_lord_spends_wealth_and_earns_it_back():
    lord = LordPP(alpha=0.05)
    start = lord.state.wealth
    rejected, alpha_t, wealth = lord.test(1.0)      # a clear non-discovery
    assert rejected is False
    assert 0 < alpha_t <= start
    assert wealth == start - alpha_t

    before = lord.state.wealth
    rejected, alpha_t, wealth = lord.test(1e-9)     # a discovery
    assert rejected is True
    assert wealth == before - alpha_t + lord.b0
    assert wealth > before


def test_lord_never_offers_more_alpha_than_wealth():
    lord = LordPP(alpha=0.05)
    for _ in range(50):
        assert lord.alpha_for_next() <= max(lord.state.wealth, 0.0) + 1e-15
        lord.test(1.0)
    assert lord.exhausted or lord.state.wealth >= 0


def test_lord_controls_false_discoveries_on_a_null_stream():
    """1,000 pure-null p-values must not produce a pile of promotions."""
    import numpy as np

    rng = np.random.default_rng(42)
    lord = LordPP(alpha=0.05)
    rejections = sum(1 for p in rng.uniform(0, 1, 1000) if lord.test(float(p))[0])
    assert rejections <= 20


# ── deflated Sharpe ──────────────────────────────────────────────────────

def test_norm_helpers_match_reference_values():
    assert abs(_norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(_norm_cdf(1.959963985) - 0.975) < 1e-6
    assert abs(_norm_ppf(0.975) - 1.959963985) < 1e-6
    assert abs(_norm_ppf(0.5)) < 1e-9


def test_expected_max_sharpe_grows_with_trials():
    assert expected_max_sharpe(1000) > expected_max_sharpe(10) > 0


def test_more_trials_deflate_the_same_track_record():
    series = [0.012, 0.008, 0.015, -0.002, 0.011, 0.009, 0.013, 0.006,
              0.010, 0.014, -0.001, 0.012, 0.007, 0.011, 0.009, 0.010]
    few = deflated_sharpe(series, n_trials=1)
    many = deflated_sharpe(series, n_trials=5000)
    assert few.sharpe == many.sharpe
    assert many.probability < few.probability


def test_deflated_sharpe_degenerate_inputs():
    assert deflated_sharpe([0.01, 0.01, 0.01], n_trials=10).probability == 0.0
    assert deflated_sharpe([0.01], n_trials=10).probability == 0.0


def test_moments_of_a_symmetric_series():
    mean, sd, skew, kurt = moments([-2, -1, 0, 1, 2])
    assert mean == 0.0
    assert abs(skew) < 1e-12
    assert sd == math.sqrt(2.0)
    assert kurt < 3.0  # platykurtic, as a uniform-ish sample should be


# ── placebo ──────────────────────────────────────────────────────────────

def test_placebo_pass_and_fail():
    draws = [i / 1000.0 for i in range(1000)]
    assert placebo_percentile(5.0, draws).passed           # beats every draw
    assert not placebo_percentile(0.5, draws).passed       # sits at the median


def test_placebo_without_draws_cannot_pass():
    result = placebo_percentile(1.0, [])
    assert result.passed is False and result.n_draws == 0
