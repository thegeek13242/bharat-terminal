"""Deflated Sharpe ratio (Bailey & López de Prado).

A Sharpe ratio selected as the best of N trials is biased upward. The factory
knows N exactly — every sweep point of every experiment is a ledger row — so
the deflation uses a counted number of trials rather than a guess.

`deflated_sharpe` returns the probability that the true Sharpe exceeds the
benchmark implied by N trials. The ROBUST gate requires >= 0.95.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

EULER_MASCHERONI = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """Inverse normal CDF (Acklam's rational approximation, |err| < 1.15e-9)."""
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


@dataclass(frozen=True)
class DSRResult:
    sharpe: float
    expected_max_sharpe: float
    probability: float
    n_trials: int
    n_obs: int
    skew: float
    kurtosis: float


def expected_max_sharpe(n_trials: int, sharpe_variance: float = 1.0) -> float:
    """E[max Sharpe] over n independent trials of zero-skill strategies."""
    n = max(2, int(n_trials))
    sd = math.sqrt(max(sharpe_variance, 0.0))
    a = _norm_ppf(1.0 - 1.0 / n)
    b = _norm_ppf(1.0 - 1.0 / (n * math.e))
    return sd * ((1.0 - EULER_MASCHERONI) * a + EULER_MASCHERONI * b)


def moments(series: Sequence[float]) -> tuple[float, float, float, float]:
    """(mean, std, skew, kurtosis) — population moments, kurtosis not excess."""
    x = [float(v) for v in series]
    n = len(x)
    if n < 2:
        return (x[0] if x else 0.0), 0.0, 0.0, 3.0
    mean = sum(x) / n
    m2 = sum((v - mean) ** 2 for v in x) / n
    if m2 <= 0:
        return mean, 0.0, 0.0, 3.0
    m3 = sum((v - mean) ** 3 for v in x) / n
    m4 = sum((v - mean) ** 4 for v in x) / n
    sd = math.sqrt(m2)
    return mean, sd, m3 / (sd ** 3), m4 / (m2 ** 2)


def deflated_sharpe(
    series: Sequence[float],
    n_trials: int,
    benchmark_sharpe: float | None = None,
) -> DSRResult:
    """Probability that observed skill survives selection over `n_trials`."""
    x = [float(v) for v in series]
    n = len(x)
    mean, sd, skew, kurt = moments(x)
    if n < 3 or sd == 0.0:
        return DSRResult(0.0, 0.0, 0.0, int(n_trials), n, skew, kurt)

    sharpe = mean / sd
    sr0 = benchmark_sharpe
    if sr0 is None:
        # Variance of the Sharpe estimate across trials, approximated by the
        # sampling variance of a zero-skill Sharpe: 1/(n-1).
        sr0 = expected_max_sharpe(n_trials, sharpe_variance=1.0 / max(n - 1, 1))

    denom = 1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * sharpe ** 2
    if denom <= 0:
        return DSRResult(sharpe, sr0, 0.0, int(n_trials), n, skew, kurt)

    z = (sharpe - sr0) * math.sqrt(n - 1) / math.sqrt(denom)
    return DSRResult(sharpe, sr0, _norm_cdf(z), int(n_trials), n, skew, kurt)
