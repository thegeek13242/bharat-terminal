"""ReportFacts — the single source of every number in the daily email.

Built deterministically from ledger rows. The renderer formats it, the agent
may describe it, and neither may add to it.

ACTION REQUIRED is computed here, not written by an agent, because "does this
need the founder tonight" is exactly the judgement an LLM should not be making
unsupervised.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

# Fewer completed experiments than this and the report says so plainly.
LOW_ACTIVITY_THRESHOLD = 3

# Spend fraction of the monthly cap that demands attention.
BUDGET_ATTENTION_FRACTION = 0.80


@dataclass
class Scorecard:
    hypotheses_tested: int = 0
    experiments_completed: int = 0
    rejected: int = 0
    promoted: int = 0
    entered_shadow: int = 0
    entered_paper: int = 0
    llm_usd: float = 0.0
    eval_usd: float = 0.0
    compute_hours: float = 0.0
    budget_remaining_usd: Optional[float] = None
    runway_days: Optional[int] = None


@dataclass
class ReportFacts:
    report_date: date
    dataset_version: Optional[str] = None
    snapshot_age_hours: Optional[float] = None
    scorecard: Scorecard = field(default_factory=Scorecard)
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    survivors: List[Dict[str, Any]] = field(default_factory=list)
    paper: List[Dict[str, Any]] = field(default_factory=list)
    frontier: List[Dict[str, Any]] = field(default_factory=list)
    integrity_flags: List[str] = field(default_factory=list)
    integrity_failures: List[str] = field(default_factory=list)
    agent_quality: Dict[str, Any] = field(default_factory=dict)
    health: Dict[str, Any] = field(default_factory=dict)
    pending_approvals: List[str] = field(default_factory=list)
    production_candidates: List[str] = field(default_factory=list)
    alpha_wealth_remaining: Optional[float] = None
    previous_delivery_failed: bool = False
    actions: List[str] = field(default_factory=list)

    @property
    def activity_low(self) -> bool:
        return self.scorecard.experiments_completed < LOW_ACTIVITY_THRESHOLD

    @property
    def action_required(self) -> bool:
        return bool(self.actions)

    def numbers(self) -> List[float]:
        """Every numeric value a narrative is allowed to cite."""
        out: List[float] = []

        def walk(v: Any) -> None:
            if isinstance(v, bool):
                return
            if isinstance(v, (int, float)):
                out.append(float(v))
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)

        walk(asdict(self.scorecard))
        for block in (self.hypotheses, self.failures, self.survivors,
                      self.paper, self.frontier):
            walk(block)
        walk(self.alpha_wealth_remaining)
        walk(self.snapshot_age_hours)
        return out

    def identifiers(self) -> List[str]:
        """Every ID a narrative is allowed to cite."""
        ids: List[str] = []
        for block in (self.hypotheses, self.failures, self.survivors, self.paper):
            for row in block:
                for key in ("hypothesis_id", "experiment_id", "failure_id",
                            "strategy_id", "run_id", "dataset_version"):
                    v = row.get(key)
                    if isinstance(v, str):
                        ids.append(v)
        if self.dataset_version:
            ids.append(self.dataset_version)
        ids.extend(self.production_candidates)
        return ids


def compute_action_required(facts: ReportFacts,
                            budget_cap_usd: Optional[float] = None) -> List[str]:
    """Deterministic ACTION REQUIRED. Order is stable so reports diff cleanly."""
    actions: List[str] = []

    for candidate in facts.production_candidates:
        actions.append(f"PRODUCTION CANDIDATE awaiting review: {candidate}")
    for approval in facts.pending_approvals:
        actions.append(f"Approval pending: {approval}")
    for failure in facts.integrity_failures:
        actions.append(f"Snapshot integrity FAILURE: {failure}")
    if facts.previous_delivery_failed:
        actions.append("Yesterday's report failed to deliver")
    if facts.snapshot_age_hours is not None and facts.snapshot_age_hours > 26:
        actions.append(
            f"Snapshot is stale ({facts.snapshot_age_hours:.1f}h) — no new experiments ran")
    if (facts.alpha_wealth_remaining is not None
            and facts.alpha_wealth_remaining <= 0):
        actions.append("Alpha wealth exhausted — promotions are frozen until reviewed")
    if budget_cap_usd:
        spent = facts.scorecard.llm_usd + facts.scorecard.eval_usd
        if spent >= BUDGET_ATTENTION_FRACTION * budget_cap_usd:
            actions.append(
                f"Spend at {spent / budget_cap_usd:.0%} of the ${budget_cap_usd:.0f} cap")
    for key in ("e1_failing", "e7_failing", "repro_mismatch"):
        if facts.agent_quality.get(key):
            actions.append(f"Quality gate failing: {key}")
    return actions


def build_facts(
    report_date: date,
    *,
    cycles: Sequence[Dict[str, Any]] = (),
    failures: Sequence[Dict[str, Any]] = (),
    paper: Sequence[Dict[str, Any]] = (),
    frontier: Sequence[Dict[str, Any]] = (),
    dataset_version: Optional[str] = None,
    snapshot_age_hours: Optional[float] = None,
    integrity_flags: Sequence[str] = (),
    integrity_failures: Sequence[str] = (),
    agent_quality: Optional[Dict[str, Any]] = None,
    health: Optional[Dict[str, Any]] = None,
    pending_approvals: Sequence[str] = (),
    alpha_wealth_remaining: Optional[float] = None,
    previous_delivery_failed: bool = False,
    llm_usd: float = 0.0,
    eval_usd: float = 0.0,
    compute_hours: float = 0.0,
    budget_cap_usd: Optional[float] = None,
    budget_remaining_usd: Optional[float] = None,
) -> ReportFacts:
    """Assemble the day's facts from completed cycle records."""
    promoted = [c for c in cycles if c.get("promoted")]
    rejected = [c for c in cycles if c.get("to_status") in ("REJECTED", "QUARANTINED")]
    shadow = [c for c in promoted if c.get("to_status") == "SHADOW"]
    to_paper = [c for c in promoted if c.get("to_status") == "PAPER"]

    scorecard = Scorecard(
        hypotheses_tested=len({c.get("hypothesis_id") for c in cycles if c.get("hypothesis_id")}),
        experiments_completed=len(cycles),
        rejected=len(rejected),
        promoted=len(promoted),
        entered_shadow=len(shadow),
        entered_paper=len(to_paper),
        llm_usd=round(llm_usd, 2),
        eval_usd=round(eval_usd, 2),
        compute_hours=round(compute_hours, 2),
        budget_remaining_usd=budget_remaining_usd,
    )

    facts = ReportFacts(
        report_date=report_date,
        dataset_version=dataset_version,
        snapshot_age_hours=snapshot_age_hours,
        scorecard=scorecard,
        hypotheses=list(cycles),
        failures=list(failures),
        survivors=list(promoted),
        paper=list(paper),
        frontier=list(frontier),
        integrity_flags=list(integrity_flags),
        integrity_failures=list(integrity_failures),
        agent_quality=agent_quality or {},
        health=health or {},
        pending_approvals=list(pending_approvals),
        production_candidates=[c["hypothesis_id"] for c in cycles
                               if c.get("to_status") == "PRODUCTION_CANDIDATE"
                               and c.get("hypothesis_id")],
        alpha_wealth_remaining=alpha_wealth_remaining,
        previous_delivery_failed=previous_delivery_failed,
    )
    facts.actions = compute_action_required(facts, budget_cap_usd)
    return facts
