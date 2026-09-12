"""Divergence classification — ordered, deterministic, first match wins.

The order encodes what is cheapest to rule out and most damaging to miss:

  1. implementation_bug   the engine did not do what the spec said
  2. data_mismatch        the features at decision time were not the ones we backtested
  3. execution_error      we got worse prices than modelled
  4. model_drift          an upstream model/artifact changed under us
  5. regime_change        the world moved, within what the research anticipated
  6. signal_decay         the edge is fading while execution stays clean
  7. research_error       none of the above, and the record is outside the envelope
  8. within_tolerance     nothing to explain

Only after 1-6 are excluded may the finding itself be blamed. The live
`orderwin_long` case is exactly class 1: the D0T research specified a 5-day
hold with no stop, and the paper engine applied trailing stops to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from bharat_terminal.factory.feedback.envelope import (
    Envelope,
    envelope_breach,
    regime_z,
    slippage_ratio,
)


@dataclass(frozen=True)
class SpecReplay:
    """Result of replaying the frozen spec over the actual trade log."""

    trades_total: int
    exit_rule_violations: int = 0      # exits the spec does not allow (e.g. a stop)
    entry_rule_violations: int = 0     # entered outside the spec's entry rule
    stale_fill_count: int = 0          # filled at a price with no fresh print
    fill_timing_violations: int = 0    # filled outside the allowed window

    @property
    def violation_share(self) -> float:
        if self.trades_total <= 0:
            return 0.0
        bad = self.exit_rule_violations + self.entry_rule_violations + self.stale_fill_count
        return bad / self.trades_total


@dataclass(frozen=True)
class Realized:
    n_events: int
    trading_days: int
    mean: float
    sd: float = 0.0
    hit_rate: Optional[float] = None
    slippage_bps: float = 0.0
    feature_mismatch_share: float = 0.0
    upstream_artifact_changed: bool = False
    rolling_slope_t: Optional[float] = None
    regime_cell: Optional[str] = None
    regime_mean: Optional[float] = None
    regime_n: int = 0


@dataclass(frozen=True)
class DivergenceThresholds:
    max_violation_share: float = 0.05
    max_feature_mismatch_share: float = 0.05
    max_slippage_ratio: float = 1.5
    regime_z_abs: float = 2.0
    decay_slope_t: float = -2.0
    research_error_min_events: int = 60


@dataclass(frozen=True)
class DivergenceOutcome:
    classification: str
    action: str                        # none | followup | pause
    evidence: Tuple[Dict[str, object], ...] = ()
    detail: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "classification": self.classification,
            "action": self.action,
            "evidence": list(self.evidence),
            "detail": self.detail,
        }


# Classes that mean "stop trading this and look at it".
_PAUSE = {"implementation_bug", "data_mismatch", "research_error"}


def _ev(kind: str, value, threshold=None) -> Dict[str, object]:
    e: Dict[str, object] = {"kind": kind, "value": value}
    if threshold is not None:
        e["threshold"] = threshold
    return e


def classify_divergence(
    env: Envelope,
    realized: Realized,
    replay: SpecReplay,
    th: DivergenceThresholds | None = None,
) -> DivergenceOutcome:
    th = th or DivergenceThresholds()
    evidence: List[Dict[str, object]] = []

    # 1. Did the engine follow the spec?
    if replay.violation_share > th.max_violation_share:
        evidence.append(_ev("violation_share", round(replay.violation_share, 4),
                            th.max_violation_share))
        if replay.exit_rule_violations:
            evidence.append(_ev("exit_rule_violations", replay.exit_rule_violations))
        if replay.entry_rule_violations:
            evidence.append(_ev("entry_rule_violations", replay.entry_rule_violations))
        if replay.stale_fill_count:
            evidence.append(_ev("stale_fills", replay.stale_fill_count))
        return DivergenceOutcome(
            "implementation_bug", "pause", tuple(evidence),
            "the live engine did not execute the frozen specification",
        )

    # 2. Were the features the ones we backtested?
    if realized.feature_mismatch_share > th.max_feature_mismatch_share:
        evidence.append(_ev("feature_mismatch_share", round(realized.feature_mismatch_share, 4),
                            th.max_feature_mismatch_share))
        return DivergenceOutcome(
            "data_mismatch", "pause", tuple(evidence),
            "decision-time features differ from the snapshot-recomputed values",
        )

    # 3. Did we pay more than modelled?
    ratio = slippage_ratio(env, realized.slippage_bps)
    if ratio is not None and ratio > th.max_slippage_ratio:
        evidence.append(_ev("slippage_ratio", round(ratio, 3), th.max_slippage_ratio))
        evidence.append(_ev("realized_slippage_bps", realized.slippage_bps))
        return DivergenceOutcome(
            "execution_error", "followup", tuple(evidence),
            "realized slippage exceeds the modelled cost",
        )
    if replay.fill_timing_violations > 0:
        evidence.append(_ev("fill_timing_violations", replay.fill_timing_violations))
        return DivergenceOutcome(
            "execution_error", "followup", tuple(evidence),
            "fills landed outside the allowed timing window",
        )

    breached = envelope_breach(env, realized.mean)

    # 4. Did something upstream change under us?
    if realized.upstream_artifact_changed and breached:
        evidence.append(_ev("upstream_artifact_changed", True))
        evidence.append(_ev("realized_mean", realized.mean, (env.ci_low, env.ci_high)))
        return DivergenceOutcome(
            "model_drift", "followup", tuple(evidence),
            "an upstream model or artifact version changed during the window",
        )

    # 5. Is the shortfall concentrated in one regime the research described?
    if realized.regime_cell and realized.regime_mean is not None:
        z = regime_z(env, realized.regime_cell, realized.regime_mean,
                     realized.sd, realized.regime_n)
        if z is not None and abs(z) >= th.regime_z_abs:
            evidence.append(_ev("regime_cell", realized.regime_cell))
            evidence.append(_ev("regime_z", round(z, 2), th.regime_z_abs))
            return DivergenceOutcome(
                "regime_change", "followup", tuple(evidence),
                "divergence concentrated in one regime cell",
            )

    # 6. Is a real edge fading?
    if realized.rolling_slope_t is not None and realized.rolling_slope_t <= th.decay_slope_t:
        evidence.append(_ev("rolling_slope_t", realized.rolling_slope_t, th.decay_slope_t))
        return DivergenceOutcome(
            "signal_decay", "followup", tuple(evidence),
            "rolling performance is trending down with clean execution",
        )

    # 7. Only now may the research itself be wrong — and only with sample.
    if breached and realized.n_events >= th.research_error_min_events:
        evidence.append(_ev("realized_mean", realized.mean, (env.ci_low, env.ci_high)))
        evidence.append(_ev("n_events", realized.n_events, th.research_error_min_events))
        return DivergenceOutcome(
            "research_error", "pause", tuple(evidence),
            "outside the pre-registered interval with every mechanical explanation excluded",
        )

    if breached:
        evidence.append(_ev("n_events", realized.n_events, th.research_error_min_events))
        return DivergenceOutcome(
            "within_tolerance", "none", tuple(evidence),
            "outside the interval but too few events to conclude anything",
        )

    return DivergenceOutcome("within_tolerance", "none", (), "realized record matches the envelope")


# The counterfactual trap this project already walked into once: comparing
# stopped trades against unstopped SURVIVORS. Time exits only exist because the
# stop did not fire first, so the comparison is conditioned on the outcome.
COUNTERFACTUAL_RULE = (
    "recompute ALL entries under both exit rules from price data; never compare "
    "stopped trades against surviving time-exits"
)


def followup_question(
    strategy_id: str,
    version: int,
    outcome: DivergenceOutcome,
    realized: Realized,
) -> Optional[str]:
    """Deterministic text for the research question this divergence opens."""
    if outcome.action == "none":
        return None
    if outcome.classification == "implementation_bug":
        return (
            f"{strategy_id}@{version}: the live engine deviated from the frozen spec on "
            f"{realized.n_events} events. Does the edge survive under the specification's own "
            f"exit rule? {COUNTERFACTUAL_RULE}."
        )
    if outcome.classification == "data_mismatch":
        return (
            f"{strategy_id}@{version}: decision-time features diverged from snapshot values. "
            f"Which feature, and is the backtest or the live path wrong?"
        )
    if outcome.classification == "execution_error":
        return (
            f"{strategy_id}@{version}: realized slippage exceeded the model. Recalibrate the "
            f"liquidity buckets from these fills and re-test net expectancy."
        )
    if outcome.classification == "model_drift":
        return (
            f"{strategy_id}@{version}: an upstream artifact changed mid-window. Re-run the "
            f"frozen spec on both artifact versions and compare."
        )
    if outcome.classification == "regime_change":
        return (
            f"{strategy_id}@{version}: performance is concentrated by regime "
            f"({realized.regime_cell}). Should the strategy condition on that regime?"
        )
    if outcome.classification == "signal_decay":
        return (
            f"{strategy_id}@{version}: the edge is trending down with clean execution. Is decay "
            f"real, and does a shorter holding period recover it?"
        )
    return (
        f"{strategy_id}@{version}: realized record sits outside the pre-registered interval with "
        f"mechanical causes excluded. Treat the original finding as suspect and retest."
    )
