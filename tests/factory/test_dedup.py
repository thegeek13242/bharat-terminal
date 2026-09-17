"""The dedup index must see proposals, not only runs.

Regression for the 2026-09-17 observational pilot: it evaluates dry by design,
so factory.experiments was never written, the index stayed frozen at 2 entries
while hypotheses grew to 24, and 8 of those rows (33%) were duplicate
mechanics. The only hypothesis that ever cleared the gate had a twin.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from bharat_terminal.factory.dsl.dedup import known_mechanics
from bharat_terminal.factory.dsl.hashing import spec_hash

from tests.factory.conftest import BASE_SPEC, _deep_merge, make_spec


class LedgerExecutor:
    """Answers the two queries known_mechanics issues, and nothing else."""

    def __init__(self, hypotheses: List[Dict[str, Any]],
                 experiments: List[Dict[str, Any]] | None = None):
        self._h = hypotheses
        self._e = experiments or []

    async def __call__(self, sql: str, params: Dict[str, Any]):
        if "FROM factory.hypotheses" in sql:
            return self._h
        if "FROM factory.experiments" in sql:
            return self._e
        raise AssertionError(f"unexpected query: {sql}")


def _row(hid: str, **overrides: Any) -> Dict[str, Any]:
    spec = _deep_merge(BASE_SPEC, {"id": hid, **overrides})
    return {"hypothesis_id": hid, "spec_json": json.dumps(spec)}


@pytest.mark.asyncio
async def test_a_proposed_hypothesis_is_enough_to_be_a_duplicate():
    """The bug: only run experiments counted, so proposals recurred forever."""
    ex = LedgerExecutor([_row("H-000007")])
    km = await known_mechanics(ex)
    assert km[spec_hash(make_spec(id="H-000007"))] == "H-000007"


@pytest.mark.asyncio
async def test_identical_mechanics_under_two_ids_collapse_to_the_first():
    """H-000009 was a twin of H-000007; the citation must name the original."""
    ex = LedgerExecutor([_row("H-000007"), _row("H-000009")])
    km = await known_mechanics(ex)
    assert len(km) == 1
    assert list(km.values()) == ["H-000007"]


@pytest.mark.asyncio
async def test_an_experiment_outranks_a_hypothesis_on_the_same_hash():
    """"already run as EXP-000001" is a stronger citation than "proposed as"."""
    h = spec_hash(make_spec(id="H-000002"))
    ex = LedgerExecutor([_row("H-000002")],
                        [{"spec_hash": h, "experiment_id": "EXP-000001"}])
    km = await known_mechanics(ex)
    assert km[h] == "EXP-000001"


@pytest.mark.asyncio
async def test_differing_mechanics_stay_separate():
    """Dedup must not over-match, or real variants get refused as duplicates."""
    ex = LedgerExecutor([_row("H-000007"),
                         _row("H-000008", exit={**BASE_SPEC["exit"],
                                                "holding_days": [10]},
                              statistics={**BASE_SPEC["statistics"],
                                          "primary_holding_days": 10})])
    km = await known_mechanics(ex)
    assert len(km) == 2


@pytest.mark.asyncio
async def test_one_unreadable_spec_does_not_take_down_the_cycle():
    """Fail soft: skipping a bad row risks a duplicate, raising stops research."""
    ex = LedgerExecutor([{"hypothesis_id": "H-000099", "spec_json": '{"broken": true}'},
                         _row("H-000007")])
    km = await known_mechanics(ex)
    assert km[spec_hash(make_spec(id="H-000007"))] == "H-000007"
    assert "H-000099" not in km.values()


# ══ self-collision: the regression the first dedup fix caused ═════════════
#
# Adding hypotheses to the index fixed cross-cycle duplicates and broke
# re-evaluation in the same move. Observed live 2026-09-17: every stored
# hypothesis failed E010 as a duplicate of itself, killing the evaluate stage.

def test_a_hypothesis_does_not_collide_with_its_own_ledger_row():
    from bharat_terminal.factory.dsl.dedup import without_self
    known = {"aaa": "H-000026", "bbb": "H-000004"}
    assert without_self(known, "H-000026") == {"bbb": "H-000004"}


def test_a_genuine_duplicate_is_still_refused_after_excluding_self():
    """Only the self-reference goes. Another id's claim on the hash stays."""
    from bharat_terminal.factory.dsl.dedup import without_self
    known = {"aaa": "H-000007"}          # H-000009's mechanics, owned by H-000007
    assert without_self(known, "H-000009") == {"aaa": "H-000007"}


def test_excluding_self_removes_every_row_that_id_owns():
    from bharat_terminal.factory.dsl.dedup import without_self
    known = {"aaa": "H-000001", "bbb": "H-000001", "ccc": "EXP-000001"}
    assert without_self(known, "H-000001") == {"ccc": "EXP-000001"}
