"""Placebo and permutation helpers.

A placebo run repeats the entire pipeline on pseudo-event dates drawn from the
same symbols' non-event sessions. If the "edge" survives on dates where
nothing happened, the edge is an artifact of the universe, the calendar or the
return construction — not of the event.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PlaceboResult:
    observed: float
    n_draws: int
    percentile: float
    passed: bool
    threshold_percentile: float


def placebo_percentile(
    observed: float,
    placebo_effects: Sequence[float],
    threshold_percentile: float = 97.5,
) -> PlaceboResult:
    """Where the real effect sits in the placebo distribution.

    Uses the conservative (r + 1) / (n + 1) convention, so a draw count of
    zero can never manufacture a pass.
    """
    draws = [float(v) for v in placebo_effects]
    n = len(draws)
    if n == 0:
        return PlaceboResult(float(observed), 0, 0.0, False, threshold_percentile)
    below = sum(1 for v in draws if v < observed)
    pct = 100.0 * (below + 1) / (n + 1)
    return PlaceboResult(
        observed=float(observed),
        n_draws=n,
        percentile=pct,
        passed=pct >= threshold_percentile,
        threshold_percentile=threshold_percentile,
    )
