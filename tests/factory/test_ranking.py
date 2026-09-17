"""Trailing deciles must not see the future.

A decile is the one place a new feature can silently acquire look-ahead bias:
rank against the whole sample and every feature looks predictive. These tests
pin the property, not just the arithmetic.
"""
from __future__ import annotations

from datetime import date, timedelta

from bharat_terminal.factory.backtest.ranking import (
    DEFAULT_MIN_REFERENCE, Ranked, trailing_deciles)

D0 = date(2024, 1, 1)


def _series(n, value_fn, start=D0, step=1):
    return [(start + timedelta(days=i * step), value_fn(i)) for i in range(n)]


def test_the_future_cannot_change_the_past():
    """The property that matters. Append later events; earlier ranks must hold."""
    base = _series(400, lambda i: float(i % 50))
    first = trailing_deciles(base, min_reference=50)

    extended = base + _series(200, lambda i: 1e6, start=D0 + timedelta(days=400))
    second = trailing_deciles(extended, min_reference=50)

    assert first == second[:400]


def test_same_day_events_do_not_rank_each_other():
    """A day's worth of results must not be its own reference set."""
    obs = _series(DEFAULT_MIN_REFERENCE + 10, lambda i: float(i))
    day = obs[-1][0] + timedelta(days=1)
    obs += [(day, 0.0), (day, 1e9)]

    out = trailing_deciles(obs)
    # Both same-day events see an identical reference set of the same size.
    assert out[-1].reference_n == out[-2].reference_n
    # The enormous one ranks top, the tiny one bottom -- against the PAST only.
    assert out[-1].decile == 10
    assert out[-2].decile == 1


def test_under_referenced_returns_none_not_a_guess():
    out = trailing_deciles(_series(20, lambda i: float(i)), min_reference=100)
    assert all(r.decile is None and r.percentile is None for r in out)
    assert out[0].reference_n == 0
    assert out[-1].reference_n == 19


def test_values_outside_the_lookback_are_evicted():
    """An old regime must stop counting once it ages out."""
    old = _series(200, lambda i: 0.0)
    recent = _series(200, lambda i: 100.0, start=D0 + timedelta(days=2000))
    out = trailing_deciles(old + recent, lookback_days=365, min_reference=50)
    # The last recent event sees only recent values, not the 200 old zeros.
    assert out[-1].reference_n <= 200


def test_ties_take_the_midrank_not_the_top():
    """Otherwise a repetitive feature dumps every tied row into decile 10."""
    obs = _series(300, lambda i: 7.0)
    out = trailing_deciles(obs, min_reference=50)
    ranked = [r for r in out if r.decile is not None]
    assert ranked, "expected some ranked rows"
    assert all(r.percentile == 0.5 for r in ranked)
    assert all(r.decile == 6 for r in ranked)


def test_results_come_back_in_input_order():
    """Callers zip this onto their rows; a re-ordered result corrupts the join."""
    obs = [(D0 + timedelta(days=5), 5.0),
           (D0, 1.0),
           (D0 + timedelta(days=2), 2.0)]
    out = trailing_deciles(obs, min_reference=1)
    assert len(out) == 3
    assert out[1].reference_n == 0          # the earliest date, listed second
    assert out[2].reference_n == 1          # saw only the earliest
    assert out[0].reference_n == 2          # saw both


def test_a_higher_value_never_ranks_below_a_lower_one_on_the_same_day():
    obs = _series(200, lambda i: float(i % 30))
    day = obs[-1][0] + timedelta(days=1)
    obs += [(day, 1.0), (day, 29.0)]
    out = trailing_deciles(obs, min_reference=50)
    assert out[-1].percentile >= out[-2].percentile
