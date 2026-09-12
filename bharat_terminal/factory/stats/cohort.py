"""Cohort inference.

Event studies violate independence twice over: several events share a week,
and overlapping holding windows correlate successive weeks. The project's own
validated PEAD work already handles the first by aggregating into calendar
cohorts; this module adds the second with a Newey-West (Bartlett) correction
on the cohort series.

The unit of inference is the COHORT MEAN, never the individual trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence


@dataclass(frozen=True)
class CohortStats:
    n_cohorts: int
    n_events: int
    mean: float
    se: float
    tstat: float
    lag: int
    positive_cohort_share: float
    by_year: Dict[int, float]


def newey_west_tstat(series: Sequence[float], lag: int = 0) -> tuple[float, float, float]:
    """t-statistic for the mean of an autocorrelated series.

    Returns (mean, standard error, t). With ``lag=0`` this is the ordinary
    standard error of the mean using the population variance (ddof=0).

    Variance of the mean uses the Bartlett-weighted long-run variance:

        v = g0 + 2 * sum_{l=1..L} (1 - l/(L+1)) * gl
        se = sqrt(v / n)

    where ``gl`` is the lag-l autocovariance. A degenerate series (n < 2 or
    zero variance) returns t = 0.0 rather than infinity, so a gate can never
    be passed by a constant.
    """
    x = [float(v) for v in series]
    n = len(x)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = sum(x) / n
    if n < 2:
        return mean, 0.0, 0.0

    dev = [v - mean for v in x]

    def autocov(l: int) -> float:
        return sum(dev[t] * dev[t - l] for t in range(l, n)) / n

    v = autocov(0)
    L = max(0, int(lag))
    for l in range(1, min(L, n - 1) + 1):
        weight = 1.0 - l / (L + 1.0)
        v += 2.0 * weight * autocov(l)

    if v <= 0.0:
        # Negative long-run variance estimates are possible with Bartlett
        # weights on short series; fall back to the uncorrected variance.
        v = autocov(0)
    if v <= 0.0:
        return mean, 0.0, 0.0

    se = math.sqrt(v / n)
    if se == 0.0:
        return mean, 0.0, 0.0
    return mean, se, mean / se


def lag_for_holding(holding_days: int, cohort_days: int = 5) -> int:
    """Bartlett lag for overlapping windows: ceil(holding / cohort length)."""
    return max(0, math.ceil(holding_days / float(cohort_days)) - 0)


def cohort_stats(
    cohorts: Sequence[tuple[str, int, float]],
    holding_days: int,
    cohort_days: int = 5,
) -> CohortStats:
    """Aggregate (cohort_key, n_events, mean_return) rows into one verdict.

    `cohort_key` is an ISO date string for the cohort start, so the year
    breakdown the gates need ("positive in >= 60% of years") is derivable
    without a second pass over the trades.
    """
    if not cohorts:
        return CohortStats(0, 0, 0.0, 0.0, 0.0, 0, 0.0, {})

    means = [float(c[2]) for c in cohorts]
    lag = lag_for_holding(holding_days, cohort_days)
    mean, se, t = newey_west_tstat(means, lag=lag)

    by_year_sums: Dict[int, List[float]] = {}
    for key, _n, m in cohorts:
        year = int(str(key)[:4])
        by_year_sums.setdefault(year, []).append(float(m))
    by_year = {y: sum(v) / len(v) for y, v in by_year_sums.items()}

    return CohortStats(
        n_cohorts=len(cohorts),
        n_events=sum(int(c[1]) for c in cohorts),
        mean=mean,
        se=se,
        tstat=t,
        lag=lag,
        positive_cohort_share=sum(1 for m in means if m > 0) / len(means),
        by_year=by_year,
    )
