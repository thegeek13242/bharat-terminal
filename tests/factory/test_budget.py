"""Spend caps. The load-bearing test is that the cap actually stops a call."""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from bharat_terminal.factory.gates.budget import (
    DEFAULT_CAP_USD,
    BudgetExhausted,
    check_cap,
    guard,
    record_spend,
)


class BudgetExecutor:
    def __init__(self, rows: List[Dict[str, Any]] | None = None):
        self.rows = list(rows or [])
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, sql: str, params: Dict[str, Any]):
        self.calls.append({"sql": sql, "params": params})
        if "INSERT INTO factory.budget_ledger" in sql:
            self.rows.append({"category": params["cat"], "usd": params["usd"]})
            return []
        agg: Dict[str, float] = {}
        for r in self.rows:
            agg[r["category"]] = agg.get(r["category"], 0.0) + float(r["usd"])
        return [{"category": k, "usd": v} for k, v in agg.items()]


@pytest.mark.asyncio
async def test_empty_ledger_is_not_halted():
    st = await check_cap(BudgetExecutor())
    assert st.spent_usd == 0.0 and st.halted is False and st.warning is False
    assert st.remaining_usd == DEFAULT_CAP_USD


@pytest.mark.asyncio
async def test_spend_accumulates_across_categories():
    ex = BudgetExecutor()
    await record_spend(ex, "llm", 10.0)
    await record_spend(ex, "eval", 2.5)
    st = await check_cap(ex, cap_usd=100.0)
    assert st.spent_usd == pytest.approx(12.5)
    assert st.by_category == {"llm": 10.0, "eval": 2.5}


@pytest.mark.asyncio
async def test_the_cap_actually_stops_the_next_call():
    ex = BudgetExecutor()
    await record_spend(ex, "llm", 100.0)
    with pytest.raises(BudgetExhausted) as e:
        await guard(ex, cap_usd=100.0)
    assert "reached the" in str(e.value)


@pytest.mark.asyncio
async def test_guard_passes_under_the_cap_and_returns_status():
    ex = BudgetExecutor()
    await record_spend(ex, "llm", 10.0)
    st = await guard(ex, cap_usd=100.0)
    assert st.halted is False and st.remaining_usd == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_warning_fires_before_the_wall_not_at_it():
    """A run should be able to finish rather than be cut mid-cycle."""
    ex = BudgetExecutor()
    await record_spend(ex, "llm", 85.0)
    st = await check_cap(ex, cap_usd=100.0)
    assert st.warning is True and st.halted is False
    assert "85%" in (st.reason() or "")


@pytest.mark.asyncio
async def test_negative_spend_is_refused():
    """Append-only: nothing may lower a running total."""
    with pytest.raises(ValueError):
        await record_spend(BudgetExecutor(), "llm", -5.0)


@pytest.mark.asyncio
async def test_values_fit_the_live_column_limits():
    """category is varchar(24), source is varchar(16) - verified 2026-09-16."""
    ex = BudgetExecutor()
    await record_spend(ex, "x" * 50, 1.0, source="y" * 50)
    p = [c for c in ex.calls if "INSERT" in c["sql"]][0]["params"]
    assert len(p["cat"]) <= 24
    assert len(p["src"]) <= 16


@pytest.mark.asyncio
async def test_ref_is_serialised_json_not_a_dict():
    ex = BudgetExecutor()
    await record_spend(ex, "llm", 1.0, ref={"agent_run_id": 7})
    p = [c for c in ex.calls if "INSERT" in c["sql"]][0]["params"]
    assert isinstance(p["ref"], str) and json.loads(p["ref"])["agent_run_id"] == 7


@pytest.mark.asyncio
async def test_budget_touches_no_forbidden_table():
    ex = BudgetExecutor()
    await record_spend(ex, "llm", 1.0)
    await check_cap(ex)
    sql = " ".join(c["sql"] for c in ex.calls).lower()
    for forbidden in ("gate_decisions", "hypothesis_status", "public.",
                      "alpha_wealth_ledger"):
        assert forbidden not in sql, forbidden
