"""Facts the Chief Scientist reasons over must be arithmetic, not vibes.

The 2026-09-17 pilot produced 24 hypotheses that were really 16 ideas. A
per-family count that cannot tell those apart would tell the Chief Scientist a
family is productive when it is repeating itself, so the duplicate-rate and
stalled calculations are pinned here.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from bharat_terminal.factory.frontier.metrics import STALL_AFTER_HYPOTHESES, family_facts

from tests.factory.conftest import BASE_SPEC, _deep_merge


class FactsExecutor:
    """Answers exactly the queries family_facts issues."""

    def __init__(self, families, hypotheses=(), spend=(), ladder=(), promoted=(), questions=()):
        self._f, self._h, self._s = families, list(hypotheses), list(spend)
        self._l, self._p, self._q = list(ladder), list(promoted), list(questions)

    async def __call__(self, sql: str, params: Dict[str, Any]):
        if "FROM factory.research_families" in sql:
            return self._f
        if "FROM factory.budget_ledger" in sql:
            return self._s
        if "FROM factory.research_questions" in sql:
            return self._q
        if "FROM factory.hypotheses" in sql and "spec_json" in sql:
            return self._h
        if "FROM factory.hypothesis_status" in sql or "JOIN factory.hypothesis_status" in sql:
            return self._l
        if "FROM factory.gate_decisions" in sql:
            return self._p
        raise AssertionError(f"unexpected query: {sql}")


def fam(fid="f1", cap=200.0, status="active"):
    return [{"family_id": fid, "title": "T", "horizon": "swing",
             "data_tier": "H", "status": status, "budget_cap_usd": cap}]


def hyp(fid, hid, hold=20):
    spec = _deep_merge(BASE_SPEC, {
        "id": hid,
        "exit": {**BASE_SPEC["exit"], "holding_days": [hold]},
        "statistics": {**BASE_SPEC["statistics"], "primary_holding_days": hold}})
    return {"family_id": fid, "hypothesis_id": hid, "spec_json": json.dumps(spec)}


@pytest.mark.asyncio
async def test_duplicate_rate_counts_mechanics_not_rows():
    """Three rows, one idea -> duplicate_rate 2/3, not 0."""
    ex = FactsExecutor(fam(), [hyp("f1", "H-000001"), hyp("f1", "H-000002"),
                               hyp("f1", "H-000003")])
    f = (await family_facts(ex))[0]
    assert f["n_hypotheses"] == 3
    assert f["n_distinct_mechanics"] == 1
    assert f["duplicate_rate"] == pytest.approx(2 / 3, abs=1e-4)


@pytest.mark.asyncio
async def test_distinct_mechanics_are_actually_distinguished():
    """Different holding periods are different mechanics, not duplicates."""
    ex = FactsExecutor(fam(), [hyp("f1", "H-000001", hold=10),
                               hyp("f1", "H-000002", hold=40)])
    f = (await family_facts(ex))[0]
    assert f["n_distinct_mechanics"] == 2
    assert f["duplicate_rate"] == 0.0


@pytest.mark.asyncio
async def test_stalled_needs_volume_and_zero_promotions():
    """Many hypotheses and no promotion is the signal worth surfacing."""
    many = [hyp("f1", f"H-{i:06d}", hold=[1, 2, 3, 5, 10, 20, 40, 60][i % 8])
            for i in range(STALL_AFTER_HYPOTHESES)]
    ex = FactsExecutor(fam(), many)
    assert (await family_facts(ex))[0]["stalled"] is True

    ex = FactsExecutor(fam(), many,
                       promoted=[{"family_id": "f1", "n": 1}])
    assert (await family_facts(ex))[0]["stalled"] is False


@pytest.mark.asyncio
async def test_a_young_family_is_untested_not_stalled():
    """Absence of results is not evidence of a dead end."""
    ex = FactsExecutor(fam(), [hyp("f1", "H-000001")])
    assert (await family_facts(ex))[0]["stalled"] is False


@pytest.mark.asyncio
async def test_unreadable_spec_counts_as_hypothesis_but_not_mechanic():
    """Dropping it entirely would understate spend per idea."""
    ex = FactsExecutor(fam(), [hyp("f1", "H-000001"),
                               {"family_id": "f1", "hypothesis_id": "H-000099",
                                "spec_json": '{"broken": true}'}])
    f = (await family_facts(ex))[0]
    assert f["n_hypotheses"] == 2
    assert f["n_distinct_mechanics"] == 1


@pytest.mark.asyncio
async def test_cost_ratios_are_none_rather_than_a_divide_by_zero():
    ex = FactsExecutor(fam(), [], spend=[{"family_id": "f1", "s": 10.0}])
    f = (await family_facts(ex))[0]
    assert f["usd_per_distinct_mechanic"] is None
    assert f["usd_per_promotion"] is None
    assert f["spend_usd"] == 10.0
    assert f["budget_remaining_usd"] == 190.0
