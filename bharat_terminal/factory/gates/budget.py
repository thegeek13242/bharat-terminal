"""Spend caps: the difference between a research loop and an open tap.

Two supervised Scientist runs cost $0.67. A loop left running overnight has no
natural ceiling -- it does not get bored, and a family that keeps almost-passing
will keep attracting budget. This module is what makes "$3,000 over 90 days" a
property of the system rather than an intention in a document.

CHECK BEFORE SPENDING, NOT AFTER
`check_cap` is meant to be called BEFORE each model invocation. Recording spend
afterwards and noticing the breach on the next pass is how you end up $200 over
on a weekend: the overrun is already paid for by the time anyone looks.

WHO MAY WRITE THIS
`budget_ledger` is not one of the three tables revoked from factory_writer, so
an agent-facing process CAN record its own spend. That is deliberate -- the
alternative is spend that never gets recorded at all, which is worse. What an
agent cannot do is raise its own cap: the cap is passed in by the orchestrator,
never read from anything the agent can write.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

Executor = Callable[[str, Dict[str, Any]], Awaitable[List[Dict[str, Any]]]]

# Founder decision 2026-09-11: $3,000 of NEW spend over a 90-day runway.
DEFAULT_WINDOW_DAYS = 90
DEFAULT_CAP_USD = 3000.0
# Warn well before the wall so a run can finish rather than being cut mid-cycle.
WARN_FRACTION = 0.80


@dataclass(frozen=True)
class CapStatus:
    spent_usd: float
    cap_usd: float
    window_days: int
    remaining_usd: float
    fraction: float
    halted: bool
    warning: bool
    by_category: Dict[str, float]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "spent_usd": round(self.spent_usd, 4),
            "cap_usd": self.cap_usd,
            "window_days": self.window_days,
            "remaining_usd": round(self.remaining_usd, 4),
            "fraction_used": round(self.fraction, 4),
            "halted": self.halted,
            "warning": self.warning,
            "by_category": {k: round(v, 4) for k, v in self.by_category.items()},
        }

    def reason(self) -> Optional[str]:
        if self.halted:
            return (f"spend ${self.spent_usd:,.2f} has reached the "
                    f"${self.cap_usd:,.2f} cap over {self.window_days} days; "
                    f"no further model calls")
        if self.warning:
            return (f"spend ${self.spent_usd:,.2f} is {self.fraction:.0%} of the "
                    f"${self.cap_usd:,.2f} cap")
        return None


async def check_cap(
    ex: Executor,
    cap_usd: float = DEFAULT_CAP_USD,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> CapStatus:
    """What has been spent in the window, and whether to stop. Read-only."""
    rows = await ex(
        """SELECT category, COALESCE(sum(amount_usd), 0) AS usd
             FROM factory.budget_ledger
            WHERE date >= (CURRENT_DATE - make_interval(days => :days))
            GROUP BY category""",
        {"days": max(1, int(window_days))})
    by_cat = {r["category"]: float(r["usd"] or 0) for r in rows}
    spent = sum(by_cat.values())
    cap = float(cap_usd)
    frac = (spent / cap) if cap > 0 else 1.0
    return CapStatus(
        spent_usd=spent, cap_usd=cap, window_days=int(window_days),
        remaining_usd=max(cap - spent, 0.0), fraction=frac,
        halted=spent >= cap, warning=frac >= WARN_FRACTION and spent < cap,
        by_category=by_cat)


async def record_spend(
    ex: Executor,
    category: str,
    amount_usd: float,
    *,
    family_id: Optional[str] = None,
    source: str = "agent_run",
    ref: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one spend entry. Append-only; nothing here can lower a total.

    The truncations match the live schema, verified 2026-09-16: category is
    varchar(24) and source is varchar(16). An earlier version had them the
    wrong way round, which would have passed every test (the default source is
    9 characters) and thrown "value too long" on the first longer caller.
    """
    if amount_usd < 0:
        raise ValueError("spend cannot be negative; the ledger is append-only")
    await ex(
        """INSERT INTO factory.budget_ledger
             (date, category, family_id, amount_usd, source, ref)
           VALUES (CURRENT_DATE, :cat, :fam, :usd, :src, :ref)""",
        {"cat": category[:24], "fam": family_id, "usd": float(amount_usd),
         "src": source[:16], "ref": json.dumps(ref or {}, default=str)})


class BudgetExhausted(RuntimeError):
    """Raised instead of making a model call that would breach the cap."""


async def guard(ex: Executor, cap_usd: float = DEFAULT_CAP_USD,
                window_days: int = DEFAULT_WINDOW_DAYS) -> CapStatus:
    """check_cap, but raises when halted.

    Use this at the top of a loop iteration so the breach stops the NEXT call
    rather than being discovered after the money is gone.
    """
    st = await check_cap(ex, cap_usd, window_days)
    if st.halted:
        raise BudgetExhausted(st.reason() or "budget exhausted")
    return st
