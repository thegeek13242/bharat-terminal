"""Per-family facts for the Chief Scientist. Deterministic, no LLM.

The division of labour the spec insists on is that an LLM never computes a
statistic. So every number the Chief Scientist reasons over is produced here,
by SQL and arithmetic, and the agent's job is to argue about what they MEAN.

WHAT IS DELIBERATELY NOT HERE
The plan sketches a priority score of EIG x P(success) x value x relevance /
cost. Expected information gain and P(success) are not estimable from 24
hypotheses against a single question -- any number this module produced for
them would be invention dressed as arithmetic, which is the exact failure mode
this repository has an audit trail about. What IS computable is cost
effectiveness: dollars per distinct mechanic and dollars per promotion. Those
are reported; the speculative factors are left to the memo, where they read as
judgement rather than measurement.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from bharat_terminal.factory.dsl.hashing import spec_hash
from bharat_terminal.factory.dsl.models import HypothesisSpec

Executor = Callable[[str, Dict[str, Any]], Awaitable[Any]]

# A family that has produced this many hypotheses without a single promotion is
# reported as stalled. It is NOT a kill -- the gate owns that -- but it is the
# number the Chief Scientist is expected to explain rather than ignore.
STALL_AFTER_HYPOTHESES = 8


@dataclass(frozen=True)
class FamilyFacts:
    family_id: str
    title: str
    horizon: str
    data_tier: str
    status: str
    budget_cap_usd: float
    spend_usd: float
    budget_remaining_usd: float
    questions_open: int
    n_hypotheses: int
    n_distinct_mechanics: int
    duplicate_rate: float
    n_promoted: int
    ladder: Dict[str, int] = field(default_factory=dict)
    usd_per_distinct_mechanic: Optional[float] = None
    usd_per_promotion: Optional[float] = None
    stalled: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


async def family_facts(ex: Executor) -> List[Dict[str, Any]]:
    """One row of hard facts per family, ordered by family_id."""
    fams = await ex(
        """SELECT family_id, title, horizon, data_tier, status, budget_cap_usd
             FROM factory.research_families ORDER BY family_id""", {})

    spend = {r["family_id"]: float(r["s"] or 0) for r in await ex(
        """SELECT family_id, sum(amount_usd) AS s FROM factory.budget_ledger
            WHERE family_id IS NOT NULL GROUP BY family_id""", {})}

    qopen = {r["family_id"]: int(r["n"]) for r in await ex(
        """SELECT family_id, count(*) AS n FROM factory.research_questions
            WHERE status = 'open' AND family_id IS NOT NULL GROUP BY family_id""", {})}

    # Mechanics are hashed rather than counted, so three rewordings of one idea
    # register as one mechanic. That distinction is the whole point: the pilot's
    # 24 hypotheses were 16 mechanics.
    hyp = await ex(
        "SELECT family_id, hypothesis_id, spec_json FROM factory.hypotheses", {})
    counts: Dict[str, int] = {}
    mechanics: Dict[str, set] = {}
    for h in hyp:
        fid = h["family_id"] or "(none)"
        counts[fid] = counts.get(fid, 0) + 1
        raw = h["spec_json"]
        try:
            stored = json.loads(raw) if isinstance(raw, str) else raw
            mechanics.setdefault(fid, set()).add(
                spec_hash(HypothesisSpec.model_validate(stored)))
        except Exception:
            # Unreadable spec: counted as a hypothesis, not as a mechanic.
            # Silently dropping it would overstate the duplicate rate.
            continue

    ladder: Dict[str, Dict[str, int]] = {}
    for r in await ex(
        """SELECT h.family_id, s.status, count(*) AS n
             FROM factory.hypotheses h
             JOIN factory.hypothesis_status s
               ON s.hypothesis_id = h.hypothesis_id AND s.version = h.version
            GROUP BY h.family_id, s.status""", {}):
        ladder.setdefault(r["family_id"] or "(none)", {})[r["status"]] = int(r["n"])

    promoted = {r["family_id"]: int(r["n"]) for r in await ex(
        """SELECT h.family_id, count(*) AS n
             FROM factory.gate_decisions d
             JOIN factory.hypotheses h
               ON h.hypothesis_id = d.hypothesis_id AND h.version = d.version
            WHERE d.to_status <> d.from_status
            GROUP BY h.family_id""", {})}

    out: List[Dict[str, Any]] = []
    for f in fams:
        fid = f["family_id"]
        n = counts.get(fid, 0)
        distinct = len(mechanics.get(fid, ()))
        sp = spend.get(fid, 0.0)
        cap = float(f["budget_cap_usd"] or 0)
        promo = promoted.get(fid, 0)
        out.append(FamilyFacts(
            family_id=fid, title=f["title"], horizon=f["horizon"],
            data_tier=f["data_tier"], status=f["status"],
            budget_cap_usd=cap, spend_usd=round(sp, 4),
            budget_remaining_usd=round(cap - sp, 4),
            questions_open=qopen.get(fid, 0),
            n_hypotheses=n, n_distinct_mechanics=distinct,
            duplicate_rate=round((n - distinct) / n, 4) if n else 0.0,
            n_promoted=promo,
            ladder=ladder.get(fid, {}),
            usd_per_distinct_mechanic=round(sp / distinct, 4) if distinct else None,
            usd_per_promotion=round(sp / promo, 4) if promo else None,
            stalled=(n >= STALL_AFTER_HYPOTHESES and promo == 0),
        ).as_dict())
    return out
