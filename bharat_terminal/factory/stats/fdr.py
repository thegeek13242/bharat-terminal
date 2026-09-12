"""Multiple-testing control.

Two different problems, two different tools:

* `benjamini_hochberg` — a BATCH of tests decided together (the sweep points
  inside one experiment).
* `LordPP` — an ONLINE stream of hypotheses arriving one at a time forever,
  which is what a research factory actually is. LORD++ (Ramdas et al., 2017)
  keeps FDR control over that stream by spending an alpha-wealth budget:
  each test may only spend what past discoveries have earned.

The wealth is persisted in `factory.alpha_wealth_ledger`, so the control
survives restarts. When wealth runs out, the gate stops promoting — that is
the intended behaviour, not a bug to route around.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple


def benjamini_hochberg(
    pvalues: Sequence[float], alpha: float = 0.05
) -> Tuple[List[bool], List[float]]:
    """Step-up BH. Returns (rejected flags, adjusted p-values) in input order."""
    p = [float(v) for v in pvalues]
    n = len(p)
    if n == 0:
        return [], []

    order = sorted(range(n), key=lambda i: p[i])
    adjusted = [0.0] * n
    prev = 1.0
    # Walk from the largest p downward so the adjusted values stay monotone.
    for rank in range(n, 0, -1):
        i = order[rank - 1]
        val = min(prev, p[i] * n / rank)
        adjusted[i] = val
        prev = val

    rejected = [adjusted[i] <= alpha for i in range(n)]
    return rejected, adjusted


def lord_gamma(j: int) -> float:
    """The standard LORD++ gamma sequence (unnormalized), j >= 1.

        gamma_j ∝ log(max(j, 2)) / (j * exp(sqrt(log j)))
    """
    j = max(1, int(j))
    log_j = math.log(j) if j > 1 else 0.0
    return math.log(max(j, 2)) / (j * math.exp(math.sqrt(log_j)))


@dataclass
class LordPPState:
    wealth: float
    n_tested: int
    rejections: List[int]


class LordPP:
    """Online FDR control over the hypothesis stream.

    alpha_t = gamma_t * W0  +  (alpha - W0) * sum_{j: tau_j < t} gamma_(t - tau_j)

    where tau_j are the indices of previous rejections. Every test also debits
    alpha_t from wealth and a rejection credits back (alpha - W0), which is
    what makes a productive research line self-financing and a barren one
    starve.
    """

    def __init__(self, alpha: float = 0.05, w0_fraction: float = 0.5):
        if not 0 < alpha < 1:
            raise ValueError("alpha must be in (0, 1)")
        self.alpha = float(alpha)
        self.w0 = self.alpha * float(w0_fraction)
        self.b0 = self.alpha - self.w0
        self._gamma_norm = None  # lazily normalized
        self.state = LordPPState(wealth=self.w0, n_tested=0, rejections=[])

    def _gamma(self, j: int) -> float:
        if self._gamma_norm is None:
            # Normalize over a long horizon so sum(gamma) ~ 1.
            self._gamma_norm = sum(lord_gamma(k) for k in range(1, 10_001))
        return lord_gamma(j) / self._gamma_norm

    def alpha_for_next(self) -> float:
        t = self.state.n_tested + 1
        a = self._gamma(t) * self.w0
        for tau in self.state.rejections:
            if tau < t:
                a += self.b0 * self._gamma(t - tau)
        return min(a, max(self.state.wealth, 0.0))

    def test(self, p_value: float) -> Tuple[bool, float, float]:
        """Test one hypothesis. Returns (rejected, alpha_t, wealth_after)."""
        alpha_t = self.alpha_for_next()
        self.state.n_tested += 1
        t = self.state.n_tested
        rejected = float(p_value) <= alpha_t and alpha_t > 0.0
        self.state.wealth -= alpha_t
        if rejected:
            self.state.rejections.append(t)
            self.state.wealth += self.b0
        return rejected, alpha_t, self.state.wealth

    @property
    def exhausted(self) -> bool:
        """True when no further promotion is affordable."""
        return self.state.wealth <= 1e-12
