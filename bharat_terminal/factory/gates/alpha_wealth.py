"""Online FDR control: the thing that stops a tireless generator from winning by volume.

An agent that can propose all night will eventually produce a t-statistic that
looks good. LORD++ makes that expensive: every test debits alpha wealth, and
only a rejection credits any back. A productive research line finances itself;
a barren one starves. Without this wired, an unsupervised loop is not a
research factory, it is a p-hacking machine that runs while you sleep.

THE LEDGER IS THE STATE
`LordPP` holds its wealth in memory, which is worthless across restarts and
worse than worthless across processes -- two orchestrators would each think
they had full wealth. So state is rebuilt by REPLAYING
`factory.alpha_wealth_ledger` in entry order before every test. The ledger is
append-only and `factory_gate` is the only role that may write it, so the
replay is authoritative.

REPLAY IS ALSO AN INTEGRITY CHECK
Replaying recomputes each historical decision from its stored p-value. If a
recomputed rejection disagrees with what the ledger recorded, the algorithm and
the record have diverged and `AlphaWealthCorrupt` is raised rather than
quietly continuing on a wealth figure nobody can reproduce. A silent
disagreement here would let promotions run on a fabricated error budget.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from bharat_terminal.factory.stats.fdr import LordPP

Executor = Callable[[str, Dict[str, Any]], Awaitable[List[Dict[str, Any]]]]

# Matches the spec's global target. Changing it invalidates every prior
# decision in the ledger, so it is a constant here rather than a parameter.
ALPHA = 0.05
W0_FRACTION = 0.5


class AlphaWealthCorrupt(RuntimeError):
    """The ledger and the algorithm disagree about a past decision."""


@dataclass(frozen=True)
class WealthDecision:
    hypothesis_id: str
    version: int
    p_value: float
    alpha_tested: float
    rejected: bool
    wealth_before: float
    wealth_after: float
    n_tested: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id, "version": self.version,
            "p_value": self.p_value, "alpha_tested": self.alpha_tested,
            "rejected": self.rejected, "wealth_before": self.wealth_before,
            "wealth_after": self.wealth_after, "n_tested": self.n_tested,
        }


def two_sided_p(t: Optional[float], n_cohorts: Optional[int] = None) -> Optional[float]:
    """Two-sided p from a cohort t-statistic, normal approximation.

    Deliberately stdlib-only (math.erfc) rather than pulling scipy into the
    stats package. With the cohort counts this factory works at -- 147 weekly
    cohorts in the PEAD baseline -- the normal and t distributions are
    indistinguishable at the third decimal. Below ~30 cohorts the
    approximation flatters the result, so the caller gets None and the gate
    treats missing evidence as a failed rule rather than a pass.
    """
    if t is None:
        return None
    if n_cohorts is not None and n_cohorts < 30:
        return None
    return math.erfc(abs(float(t)) / math.sqrt(2.0))


async def replay(ex: Executor) -> LordPP:
    """Rebuild LORD++ state from the ledger, verifying each past decision."""
    rows = await ex(
        """SELECT entry_id, hypothesis_id, p_value, rejected
             FROM factory.alpha_wealth_ledger ORDER BY entry_id""", {})
    lord = LordPP(alpha=ALPHA, w0_fraction=W0_FRACTION)
    for r in rows:
        p = r.get("p_value")
        if p is None:
            raise AlphaWealthCorrupt(
                f"entry {r.get('entry_id')} has no p_value; state cannot be replayed")
        recomputed, _, _ = lord.test(float(p))
        if bool(recomputed) != bool(r.get("rejected")):
            raise AlphaWealthCorrupt(
                f"entry {r.get('entry_id')} ({r.get('hypothesis_id')}): ledger says "
                f"rejected={r.get('rejected')} but replay gives {recomputed}. The "
                f"alpha-wealth record and the algorithm have diverged; refusing to "
                f"spend an error budget nobody can reproduce.")
    return lord


async def spend_and_record(
    ex_gate: Executor, hypothesis_id: str, version: int, p_value: float,
) -> WealthDecision:
    """Spend alpha on one hypothesis and append the decision.

    `ex_gate` MUST be a factory_gate executor: alpha_wealth_ledger is one of
    the three tables factory_writer is revoked on, so an agent path physically
    cannot reach this.
    """
    lord = await replay(ex_gate)
    before = lord.state.wealth
    rejected, alpha_t, after = lord.test(float(p_value))
    await ex_gate(
        """INSERT INTO factory.alpha_wealth_ledger
             (hypothesis_id, version, p_value, alpha_tested, rejected,
              wealth_before, wealth_after)
           VALUES (:hid, :ver, :p, :a, :rej, :wb, :wa)""",
        {"hid": hypothesis_id, "ver": version, "p": float(p_value),
         "a": alpha_t, "rej": bool(rejected), "wb": before, "wa": after})
    return WealthDecision(hypothesis_id, version, float(p_value), alpha_t,
                          bool(rejected), before, after, lord.state.n_tested)


async def remaining(ex: Executor) -> Dict[str, Any]:
    """Wealth left, and what the next test could spend. Read-only."""
    lord = await replay(ex)
    return {
        "alpha": ALPHA,
        "wealth_remaining": lord.state.wealth,
        "n_tested": lord.state.n_tested,
        "n_rejections": len(lord.state.rejections),
        "alpha_for_next": lord.alpha_for_next(),
        "exhausted": lord.alpha_for_next() <= 0.0,
    }
