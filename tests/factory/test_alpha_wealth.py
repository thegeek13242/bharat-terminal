"""Online FDR wiring: replay, corruption detection, and depletion.

The load-bearing tests are that wealth actually runs out, that a rejection
finances the next test, and that a ledger disagreeing with the algorithm stops
the world rather than continuing on an unreproducible error budget.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from bharat_terminal.factory.gates.alpha_wealth import (
    ALPHA,
    AlphaWealthCorrupt,
    remaining,
    replay,
    spend_and_record,
    two_sided_p,
)


class LedgerExecutor:
    """A fake alpha_wealth_ledger that honours insert order."""

    def __init__(self, rows: List[Dict[str, Any]] | None = None):
        self.rows = list(rows or [])
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, sql: str, params: Dict[str, Any]):
        self.calls.append({"sql": sql, "params": params})
        if "INSERT INTO factory.alpha_wealth_ledger" in sql:
            self.rows.append({
                "entry_id": len(self.rows) + 1,
                "hypothesis_id": params["hid"], "p_value": params["p"],
                "rejected": params["rej"],
            })
            return []
        return [dict(r) for r in self.rows]


# ── the p-value bridge ───────────────────────────────────────────────────

def test_two_sided_p_matches_the_normal_tail():
    assert two_sided_p(1.96) == pytest.approx(0.05, abs=0.002)
    assert two_sided_p(2.58) == pytest.approx(0.01, abs=0.002)
    assert two_sided_p(0.0) == pytest.approx(1.0)


def test_sign_is_irrelevant():
    assert two_sided_p(-2.13) == two_sided_p(2.13)


def test_too_few_cohorts_yields_no_p_value():
    """Below ~30 cohorts the normal approximation flatters the result.

    Returning None matters: the gate treats missing evidence as a FAILED rule,
    never an assumed pass, so a thin study cannot buy a rejection cheaply.
    """
    assert two_sided_p(3.0, n_cohorts=12) is None
    assert two_sided_p(3.0, n_cohorts=147) is not None
    assert two_sided_p(None) is None


# ── replay ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_ledger_starts_with_full_wealth():
    lord = await replay(LedgerExecutor())
    assert lord.state.n_tested == 0
    assert lord.state.wealth == pytest.approx(ALPHA * 0.5)


@pytest.mark.asyncio
async def test_state_is_rebuilt_from_the_ledger_not_memory():
    """A restart must not hand the next test a fresh error budget."""
    ex = LedgerExecutor()
    for i in range(5):
        await spend_and_record(ex, f"H-{i:06d}", 1, 0.9)   # five non-rejections
    lord = await replay(ex)
    assert lord.state.n_tested == 5
    assert lord.state.wealth < ALPHA * 0.5, "five tests must have cost wealth"


@pytest.mark.asyncio
async def test_a_diverging_ledger_stops_the_world():
    ex = LedgerExecutor([{"entry_id": 1, "hypothesis_id": "H-1",
                          "p_value": 0.9, "rejected": True}])   # cannot be right
    with pytest.raises(AlphaWealthCorrupt) as e:
        await replay(ex)
    assert "diverged" in str(e.value)


@pytest.mark.asyncio
async def test_a_missing_p_value_stops_the_world():
    ex = LedgerExecutor([{"entry_id": 1, "hypothesis_id": "H-1",
                          "p_value": None, "rejected": False}])
    with pytest.raises(AlphaWealthCorrupt):
        await replay(ex)


# ── the control actually biting ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_wealth_depletes_under_a_stream_of_nulls():
    """The point of the whole mechanism: testing forever is not free."""
    ex = LedgerExecutor()
    first = await remaining(ex)
    for i in range(40):
        await spend_and_record(ex, f"H-{i:06d}", 1, 0.8)
    later = await remaining(ex)
    assert later["wealth_remaining"] < first["wealth_remaining"]
    assert later["alpha_for_next"] < first["alpha_for_next"]
    assert later["n_tested"] == 40


@pytest.mark.asyncio
async def test_a_rejection_credits_wealth_back():
    """A productive line finances itself; that asymmetry is the design."""
    ex_a, ex_b = LedgerExecutor(), LedgerExecutor()
    d_null = await spend_and_record(ex_a, "H-1", 1, 0.9)          # cannot reject
    d_hit = await spend_and_record(ex_b, "H-1", 1, 1e-12)         # certainly does
    assert d_null.rejected is False and d_hit.rejected is True
    assert d_hit.wealth_after > d_null.wealth_after


@pytest.mark.asyncio
async def test_the_decision_is_recorded_with_both_wealth_sides():
    ex = LedgerExecutor()
    d = await spend_and_record(ex, "H-000002", 1, 0.033)
    ins = [c for c in ex.calls if "INSERT" in c["sql"]][0]["params"]
    assert ins["hid"] == "H-000002" and ins["p"] == 0.033
    assert ins["wb"] == d.wealth_before and ins["wa"] == d.wealth_after
    assert isinstance(ins["rej"], bool)


@pytest.mark.asyncio
async def test_it_only_ever_touches_the_alpha_wealth_ledger():
    """factory_gate can write three tables; this must use exactly one."""
    ex = LedgerExecutor()
    await spend_and_record(ex, "H-1", 1, 0.5)
    sql = " ".join(c["sql"] for c in ex.calls).lower()
    assert "alpha_wealth_ledger" in sql
    for other in ("gate_decisions", "hypothesis_status", "hypotheses", "public."):
        assert other not in sql, other
