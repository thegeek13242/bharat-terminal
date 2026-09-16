"""The promotion ladder.

Two tracks, because the data has two shapes (RESEARCH_FACTORY_SPEC_v2 §5.2):

    Tier H  IDEA -> SIMULATED -> ROBUST -> OUT_OF_SAMPLE -> PAPER -> PAPER_VERIFIED -> PRODUCTION_CANDIDATE
    Tier F  IDEA -> SIMULATED -> SHADOW -> SHADOW_VERIFIED -> PAPER -> PAPER_VERIFIED -> PRODUCTION_CANDIDATE

Tier F exists because the event-pipeline features only start 2026-05-10: there
is no history to hold out, so forward evidence IS the out-of-sample test.

PRODUCTION is absent on purpose. No code path in this factory can reach it;
that transition is a human decision and is blocked by compliance (Phase A)
regardless.

Every rule returns a RuleResult carrying its value and threshold, and the whole
list is persisted on the decision row — so "why was this promoted" is answerable
without rerunning anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

from bharat_terminal.factory import GATE_VERSION


class Status(str, Enum):
    IDEA = "IDEA"
    SIMULATED = "SIMULATED"
    ROBUST = "ROBUST"
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    SHADOW = "SHADOW"
    SHADOW_VERIFIED = "SHADOW_VERIFIED"
    PAPER = "PAPER"
    PAPER_VERIFIED = "PAPER_VERIFIED"
    PRODUCTION_CANDIDATE = "PRODUCTION_CANDIDATE"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


_LADDER_H: Tuple[Status, ...] = (
    Status.IDEA, Status.SIMULATED, Status.ROBUST, Status.OUT_OF_SAMPLE,
    Status.PAPER, Status.PAPER_VERIFIED, Status.PRODUCTION_CANDIDATE,
)
_LADDER_F: Tuple[Status, ...] = (
    Status.IDEA, Status.SIMULATED, Status.SHADOW, Status.SHADOW_VERIFIED,
    Status.PAPER, Status.PAPER_VERIFIED, Status.PRODUCTION_CANDIDATE,
)


def next_status(current: Status, data_tier: str) -> Optional[Status]:
    """The only promotion this hypothesis could make, or None at the top."""
    ladder = _LADDER_H if data_tier.upper() == "H" else _LADDER_F
    if current not in ladder:
        return None
    i = ladder.index(current)
    return ladder[i + 1] if i + 1 < len(ladder) else None


@dataclass(frozen=True)
class GateThresholds:
    # SIMULATED -> ROBUST / SHADOW
    robust_min_t: float = 3.0
    robust_min_positive_year_share: float = 0.60
    robust_min_dsr: float = 0.95
    shadow_entry_min_events: int = 100

    # ROBUST -> OUT_OF_SAMPLE
    oos_min_t: float = 2.0
    oos_min_effect_ratio: float = 0.50

    # SHADOW -> SHADOW_VERIFIED (forward evidence carries a higher bar than a
    # historical fit, because it is the only out-of-sample Tier F ever gets)
    shadow_min_events: int = 100
    shadow_min_trading_days: int = 40
    shadow_min_t: float = 2.5

    # PAPER -> PAPER_VERIFIED
    paper_min_events: int = 100
    paper_min_trading_days: int = 40
    paper_min_t: float = 2.0
    paper_max_slippage_ratio: float = 1.5

    # Capacity
    max_concurrent_paper: int = 6
    max_paper_per_family: int = 2

    # Family kill rule
    max_consecutive_failures: int = 3


@dataclass(frozen=True)
class ForwardMetrics:
    """Observed forward record for a shadow or paper strategy."""

    events: int
    trading_days: int
    net_mean: float
    net_tstat: float
    realized_slippage_bps: float
    modelled_slippage_bps: float
    divergence_class: Optional[str] = None


@dataclass(frozen=True)
class GateInput:
    hypothesis_id: str
    version: int
    from_status: Status
    data_tier: str

    # In-sample evidence (SIMULATED gate)
    cohort_tstat: Optional[float] = None
    cohort_mean: Optional[float] = None
    positive_year_share: Optional[float] = None
    n_events: Optional[int] = None
    n_cohorts: Optional[int] = None
    dsr_probability: Optional[float] = None
    lord_rejected: Optional[bool] = None

    # Adversarial
    critical_battery_failures: Tuple[str, ...] = ()
    adversary_veto_upheld: bool = False

    # Out-of-sample (ROBUST gate)
    oos_tstat: Optional[float] = None
    oos_mean: Optional[float] = None
    oos_reproduced: Optional[bool] = None
    holdout_already_used: bool = False

    # Forward
    forward: Optional[ForwardMetrics] = None

    # Preconditions that apply everywhere
    integrity_ok: bool = True
    rate_card_verified: bool = False
    tradable: bool = True
    concurrent_paper: int = 0
    family_paper_count: int = 0


@dataclass(frozen=True)
class RuleResult:
    rule: str
    value: object
    threshold: object
    passed: bool

    def as_dict(self) -> Dict[str, object]:
        return {"rule": self.rule, "value": self.value,
                "threshold": self.threshold, "pass": self.passed}


@dataclass(frozen=True)
class GateOutcome:
    from_status: Status
    to_status: Status
    promoted: bool
    gate_version: str
    rule_results: Tuple[RuleResult, ...]
    blocking: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        return {
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "promoted": self.promoted,
            "gate_version": self.gate_version,
            "rule_results": [r.as_dict() for r in self.rule_results],
            "blocking": list(self.blocking),
        }


def _r(rule: str, value, threshold, passed: bool) -> RuleResult:
    return RuleResult(rule=rule, value=value, threshold=threshold, passed=passed)


def evaluate_gate(gi: GateInput, th: GateThresholds | None = None) -> GateOutcome:
    """Decide one transition. Never raises on missing evidence — absent
    evidence is a failed rule, never an assumed pass."""
    th = th or GateThresholds()
    rules: List[RuleResult] = []

    target = next_status(gi.from_status, gi.data_tier)
    if target is None:
        return GateOutcome(gi.from_status, gi.from_status, False, GATE_VERSION, (),
                           ("no further automated promotion from this status",))

    # Preconditions that apply to every transition.
    rules.append(_r("integrity_ok", gi.integrity_ok, True, bool(gi.integrity_ok)))

    if gi.from_status is Status.IDEA:
        # Compilation and minimum-data checks happen in the compiler; reaching
        # here means they passed.
        rules.append(_r("compiled", True, True, True))

    elif gi.from_status is Status.SIMULATED:
        if gi.data_tier.upper() == "H":
            rules.append(_r("cohort_tstat", gi.cohort_tstat, th.robust_min_t,
                            gi.cohort_tstat is not None and gi.cohort_tstat >= th.robust_min_t))
            rules.append(_r("positive_year_share", gi.positive_year_share,
                            th.robust_min_positive_year_share,
                            gi.positive_year_share is not None
                            and gi.positive_year_share >= th.robust_min_positive_year_share))
            rules.append(_r("deflated_sharpe", gi.dsr_probability, th.robust_min_dsr,
                            gi.dsr_probability is not None
                            and gi.dsr_probability >= th.robust_min_dsr))
            rules.append(_r("lord_rejected", gi.lord_rejected, True, gi.lord_rejected is True))
        else:
            # Tier F cannot be promoted on history; it only earns the right to
            # start accruing forward evidence.
            rules.append(_r("n_events", gi.n_events, th.shadow_entry_min_events,
                            gi.n_events is not None and gi.n_events >= th.shadow_entry_min_events))
        rules.append(_r("critical_battery_failures", list(gi.critical_battery_failures), [],
                        not gi.critical_battery_failures))
        rules.append(_r("adversary_veto_upheld", gi.adversary_veto_upheld, False,
                        not gi.adversary_veto_upheld))

    elif gi.from_status is Status.ROBUST:
        rules.append(_r("holdout_available", gi.holdout_already_used, False,
                        not gi.holdout_already_used))
        rules.append(_r("oos_tstat", gi.oos_tstat, th.oos_min_t,
                        gi.oos_tstat is not None and gi.oos_tstat >= th.oos_min_t))
        same_sign = (
            gi.oos_mean is not None and gi.cohort_mean is not None
            and gi.oos_mean * gi.cohort_mean > 0
        )
        rules.append(_r("oos_same_sign", same_sign, True, same_sign))
        ratio = None
        if gi.oos_mean is not None and gi.cohort_mean not in (None, 0):
            ratio = abs(gi.oos_mean) / abs(gi.cohort_mean)
        rules.append(_r("oos_effect_ratio", ratio, th.oos_min_effect_ratio,
                        ratio is not None and ratio >= th.oos_min_effect_ratio))
        rules.append(_r("reproduced_on_fresh_snapshot", gi.oos_reproduced, True,
                        gi.oos_reproduced is True))

    elif gi.from_status is Status.SHADOW:
        f = gi.forward
        rules.append(_r("forward_events", f.events if f else None, th.shadow_min_events,
                        f is not None and f.events >= th.shadow_min_events))
        rules.append(_r("forward_trading_days", f.trading_days if f else None,
                        th.shadow_min_trading_days,
                        f is not None and f.trading_days >= th.shadow_min_trading_days))
        rules.append(_r("forward_tstat", f.net_tstat if f else None, th.shadow_min_t,
                        f is not None and f.net_tstat >= th.shadow_min_t))
        rules.append(_r("forward_net_positive", f.net_mean if f else None, 0.0,
                        f is not None and f.net_mean > 0))
        rules.append(_r("lord_rejected", gi.lord_rejected, True, gi.lord_rejected is True))

    elif gi.from_status in (Status.OUT_OF_SAMPLE, Status.SHADOW_VERIFIED):
        rules.append(_r("tradable", gi.tradable, True, bool(gi.tradable)))
        rules.append(_r("concurrent_paper", gi.concurrent_paper, th.max_concurrent_paper,
                        gi.concurrent_paper < th.max_concurrent_paper))
        rules.append(_r("family_paper_count", gi.family_paper_count, th.max_paper_per_family,
                        gi.family_paper_count < th.max_paper_per_family))

    elif gi.from_status is Status.PAPER:
        f = gi.forward
        rules.append(_r("paper_events", f.events if f else None, th.paper_min_events,
                        f is not None and f.events >= th.paper_min_events))
        rules.append(_r("paper_trading_days", f.trading_days if f else None,
                        th.paper_min_trading_days,
                        f is not None and f.trading_days >= th.paper_min_trading_days))
        rules.append(_r("paper_tstat", f.net_tstat if f else None, th.paper_min_t,
                        f is not None and f.net_tstat >= th.paper_min_t))
        rules.append(_r("paper_net_positive", f.net_mean if f else None, 0.0,
                        f is not None and f.net_mean > 0))
        slip_ratio = None
        if f is not None and f.modelled_slippage_bps:
            slip_ratio = f.realized_slippage_bps / f.modelled_slippage_bps
        rules.append(_r("slippage_ratio", slip_ratio, th.paper_max_slippage_ratio,
                        slip_ratio is not None and slip_ratio <= th.paper_max_slippage_ratio))
        acceptable = {"within_tolerance", "regime_change", None}
        rules.append(_r("divergence_class", f.divergence_class if f else None,
                        sorted(c for c in acceptable if c),
                        f is not None and f.divergence_class in acceptable))

    elif gi.from_status is Status.PAPER_VERIFIED:
        # The last automated step. It flags a candidate for a human; it does
        # not authorize anything.
        rules.append(_r("human_review_required", True, True, True))

    # Money-touching transitions additionally require verified cost rates.
    if target in (Status.PAPER, Status.PAPER_VERIFIED, Status.PRODUCTION_CANDIDATE):
        rules.append(_r("rate_card_verified", gi.rate_card_verified, True,
                        bool(gi.rate_card_verified)))

    blocking = tuple(r.rule for r in rules if not r.passed)
    promoted = not blocking
    return GateOutcome(
        from_status=gi.from_status,
        to_status=target if promoted else gi.from_status,
        promoted=promoted,
        gate_version=GATE_VERSION,
        rule_results=tuple(rules),
        blocking=blocking,
    )


def family_pause_check(
    consecutive_failures: int,
    new_mechanism_refs: Sequence[str] = (),
    th: GateThresholds | None = None,
) -> Tuple[bool, str]:
    """Should this research family stop spending?

    A family that has failed repeatedly may only continue if the revisit cites
    evidence the failed children did NOT use — a feature version or data window
    reference. Prose alone never reopens a family, so an eloquent agent cannot
    talk its way past the rule.
    """
    th = th or GateThresholds()
    if consecutive_failures < th.max_consecutive_failures:
        return False, "under the consecutive-failure limit"
    if new_mechanism_refs:
        return False, f"revisit justified by new evidence: {list(new_mechanism_refs)}"
    return True, (
        f"{consecutive_failures} consecutive failures with no new feature version "
        f"or data window cited"
    )
