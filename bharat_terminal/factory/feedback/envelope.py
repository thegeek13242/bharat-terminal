"""The expectation envelope.

Frozen onto every `strategy_version` the moment it is promoted, BEFORE any
live evidence exists. That ordering is the point: it is a pre-registration of
what the research predicts, so "we expected roughly this" can never be
rewritten after seeing the result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class Envelope:
    """What the research says this strategy should do per trade."""

    expected_mean: float              # net return per trade, decimal
    ci_low: float
    ci_high: float
    expected_hit_rate: Optional[float] = None
    modelled_slippage_bps: float = 10.0
    expected_holding_days: Optional[int] = None
    # Mean net return per regime cell, e.g. {"results_season": 0.01, "off": 0.004}
    regime_means: Dict[str, float] = field(default_factory=dict)
    # Expected absorption/decay profile, e.g. {"t5": 0.004, "d1": 0.006}
    decay: Dict[str, float] = field(default_factory=dict)
    source_run_id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.ci_low > self.ci_high:
            raise ValueError("ci_low must not exceed ci_high")


def envelope_breach(env: Envelope, realized_mean: float) -> bool:
    """Is the realized mean outside the pre-registered interval?"""
    return realized_mean < env.ci_low or realized_mean > env.ci_high


def slippage_ratio(env: Envelope, realized_slippage_bps: float) -> Optional[float]:
    if not env.modelled_slippage_bps:
        return None
    return realized_slippage_bps / env.modelled_slippage_bps


def regime_z(env: Envelope, cell: str, realized_mean: float, realized_sd: float,
             n: int) -> Optional[float]:
    """How far one regime cell has moved from its expected mean, in sigmas."""
    expected = env.regime_means.get(cell)
    if expected is None or realized_sd <= 0 or n <= 0:
        return None
    se = realized_sd / (n ** 0.5)
    if se == 0:
        return None
    return (realized_mean - expected) / se
