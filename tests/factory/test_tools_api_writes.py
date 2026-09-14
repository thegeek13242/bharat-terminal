"""submit_hypotheses: validation, repair, dedup and idempotency.

The load-bearing tests are the ones that prove an agent CANNOT do something:
cannot exceed the batch cap, cannot bypass the compiler, cannot submit two
identical specs in one payload, and cannot turn a retry into a duplicate row.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from bharat_terminal.factory.tools_api import writes
from bharat_terminal.factory.tools_api.writes import (
    MAX_PER_CALL,
    MAX_REPAIR_ATTEMPTS,
    submit_hypotheses,
)
from tests.factory.conftest import BASE_SPEC, make_ctx


class FakeExecutor:
    """Records every statement; RETURNING yields a row unless told otherwise."""

    def __init__(self, conflict: bool = False):
        self.calls: List[Dict[str, Any]] = []
        self.conflict = conflict

    async def __call__(self, sql: str, params: Dict[str, Any]):
        self.calls.append({"sql": sql, "params": params})
        if "RETURNING" in sql and not self.conflict:
            return [{"hypothesis_id": params.get("hid")}]
        return []

    def sql_for(self, fragment: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if fragment in c["sql"]]


def item(**spec_overrides: Any) -> Dict[str, Any]:
    spec = dict(BASE_SPEC)
    spec.update(spec_overrides)
    return {"spec": spec, "parents": spec.get("parents") or [],
            "mechanism": "anchoring on the headline number"}


def ctx_with_counts(**kw):
    """A context whose event counts clear the compiler's minimums."""
    from bharat_terminal.factory.dsl.compiler import EventCounts
    kw.setdefault("event_counter",
                  lambda _s: EventCounts(events=900, cohorts=40, symbols=250))
    return make_ctx(**kw)


# ── the cap ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batch_cap_is_enforced_and_stated():
    ex = FakeExecutor()
    items = [item(id=f"H-{i:06d}") for i in range(MAX_PER_CALL + 3)]
    out = await submit_hypotheses(ex, items, ctx=ctx_with_counts(),
                                  agent_version_id="scientist@v1")
    considered = len(out["data"]["accepted"]) + len(out["data"]["rejected"])
    assert considered == MAX_PER_CALL
    assert "cap is" in out["note"]


@pytest.mark.asyncio
async def test_empty_submission_is_not_an_error():
    out = await submit_hypotheses(FakeExecutor(), [], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    assert out["data"]["accepted"] == [] and out["data"]["rejected"] == []


# ── the compiler is the only validator ───────────────────────────────────

@pytest.mark.asyncio
async def test_a_clean_spec_is_accepted_and_written():
    ex = FakeExecutor()
    out = await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                                  agent_version_id="scientist@v1")
    assert len(out["data"]["accepted"]) == 1
    acc = out["data"]["accepted"][0]
    assert acc["hypothesis_id"] == BASE_SPEC["id"]
    assert acc["spec_hash"] and acc["inserted"] is True
    assert ex.sql_for("INSERT INTO factory.hypotheses")


@pytest.mark.asyncio
async def test_malformed_spec_is_rejected_without_touching_the_database():
    ex = FakeExecutor()
    out = await submit_hypotheses(ex, [{"mechanism": "no spec key"}],
                                  ctx=ctx_with_counts(), agent_version_id="a@v1")
    assert len(out["data"]["rejected"]) == 1
    assert out["data"]["rejected"][0]["errors"][0]["code"] == "E000"
    assert ex.calls == [], "nothing may be written for a rejected item"


@pytest.mark.asyncio
async def test_compiler_errors_are_returned_verbatim_for_repair():
    """A rejection must name the rule, not merely say no."""
    ex = FakeExecutor()
    # An intraday rule on announcement_date is blocked by E003.
    bad = item(entry={"rule": "same_session_close", "delay_minutes": 0})
    out = await submit_hypotheses(ex, [bad], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    rej = out["data"]["rejected"]
    assert rej, "an intraday rule on announcement_date must be refused"
    codes = {e["code"] for e in rej[0]["errors"]}
    assert codes, "errors must carry codes the agent can act on"
    for e in rej[0]["errors"]:
        assert set(e) >= {"code", "path", "message"}


@pytest.mark.asyncio
async def test_a_rejected_item_writes_nothing():
    ex = FakeExecutor()
    await submit_hypotheses(ex, [item(entry={"rule": "same_session_close",
                                             "delay_minutes": 0})],
                            ctx=ctx_with_counts(), agent_version_id="a@v1")
    assert not ex.sql_for("INSERT INTO factory.hypotheses")


# ── duplicates ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_of_a_known_experiment_is_surfaced():
    """E010 names the experiment whose mechanics already ran."""
    ex = FakeExecutor()
    first = await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                                    agent_version_id="a@v1")
    shash = first["data"]["accepted"][0]["spec_hash"]

    ctx = ctx_with_counts(known_spec_hashes={shash: "EXP-000042"})
    out = await submit_hypotheses(FakeExecutor(), [item(id="H-999999")],
                                  ctx=ctx, agent_version_id="a@v1")
    rej = out["data"]["rejected"]
    assert len(rej) == 1
    assert rej[0]["duplicate_of"] == "EXP-000042"
    assert any(e["code"] == "E010" for e in rej[0]["errors"])


@pytest.mark.asyncio
async def test_two_identical_specs_in_one_batch_do_not_both_land():
    """The compiler cannot see siblings in the same payload; this module can."""
    ex = FakeExecutor()
    out = await submit_hypotheses(
        ex, [item(id="H-000101"), item(id="H-000102")],
        ctx=ctx_with_counts(), agent_version_id="a@v1")
    assert len(out["data"]["accepted"]) == 1
    assert len(out["data"]["rejected"]) == 1
    assert out["data"]["rejected"][0]["duplicate_of"] == "H-000101"


# ── idempotency + lineage ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conflicting_insert_reports_not_inserted():
    """A replayed submission is a no-op, and says so rather than lying."""
    ex = FakeExecutor(conflict=True)
    out = await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    assert out["data"]["accepted"][0]["inserted"] is False


@pytest.mark.asyncio
async def test_inserts_use_on_conflict_do_nothing():
    ex = FakeExecutor()
    await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                            agent_version_id="a@v1")
    ins = ex.sql_for("INSERT INTO factory.hypotheses")[0]["sql"]
    assert "ON CONFLICT" in ins and "DO NOTHING" in ins
    assert "UPDATE" not in ins.upper().replace("DO NOTHING", "")


@pytest.mark.asyncio
async def test_lineage_edges_are_written_for_declared_parents():
    ex = FakeExecutor()
    await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                            agent_version_id="a@v1")
    edges = ex.sql_for("factory.hypothesis_lineage")
    assert edges, "BASE_SPEC declares a parent, so an edge must be recorded"
    assert edges[0]["params"]["pid"] == BASE_SPEC["parents"][0]["id"]
    assert "uq_lineage_edge" in edges[0]["sql"]


@pytest.mark.asyncio
async def test_jsonb_values_are_serialised_not_passed_as_dicts():
    """asyncpg has no codec for dict; the same class of bug as pgvector."""
    ex = FakeExecutor()
    await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                            agent_version_id="a@v1")
    p = ex.sql_for("INSERT INTO factory.hypotheses")[0]["params"]
    assert isinstance(p["claim"], str) and p["claim"].startswith("{")
    assert isinstance(p["spec_json"], str)


@pytest.mark.asyncio
async def test_created_by_records_the_agent_version():
    ex = FakeExecutor()
    await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                            agent_version_id="scientist@2026-09-14")
    p = ex.sql_for("INSERT INTO factory.hypotheses")[0]["params"]
    assert p["created_by"] == "scientist@2026-09-14"


# ── repair budget ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repair_is_offered_on_the_first_attempt_only():
    bad = [{"mechanism": "no spec"}]
    first = await submit_hypotheses(FakeExecutor(), bad, ctx=ctx_with_counts(),
                                    agent_version_id="a@v1", attempt=1)
    assert first["data"]["may_repair"] is True

    last = await submit_hypotheses(FakeExecutor(), bad, ctx=ctx_with_counts(),
                                   agent_version_id="a@v1",
                                   attempt=MAX_REPAIR_ATTEMPTS)
    assert last["data"]["may_repair"] is False


@pytest.mark.asyncio
async def test_no_repair_offered_when_nothing_was_rejected():
    out = await submit_hypotheses(FakeExecutor(), [item()], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    assert out["data"]["may_repair"] is False


# ── the negative space ───────────────────────────────────────────────────

def test_the_module_exposes_no_promotion_path():
    """Absence is the enforcement; assert it rather than assume it."""
    public = {n for n in dir(writes) if not n.startswith("_")}
    for forbidden in ("promote", "set_status", "update_status", "gate",
                      "approve", "enrol", "enroll"):
        assert not any(forbidden in n.lower() for n in public), forbidden


@pytest.mark.asyncio
async def test_no_statement_touches_a_forbidden_table():
    ex = FakeExecutor()
    await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                            agent_version_id="a@v1")
    all_sql = " ".join(c["sql"] for c in ex.calls).lower()
    for forbidden in ("gate_decisions", "hypothesis_status", "public.",
                      "alpha_wealth_ledger", "holdout_access_log"):
        assert forbidden not in all_sql, forbidden


# ══ report_data_gap ══════════════════════════════════════════════════════

class GapExecutor:
    """Routes by SQL content: family lookup, dedup probe, id allocation, insert."""

    def __init__(self, *, family_exists: bool = True,
                 duplicate_of: str | None = None,
                 max_n: int = 0, insert_conflicts: bool = False):
        self.family_exists = family_exists
        self.duplicate_of = duplicate_of
        self.max_n = max_n
        self.insert_conflicts = insert_conflicts
        self.calls: list[dict] = []

    async def __call__(self, sql: str, params: dict):
        self.calls.append({"sql": sql, "params": params})
        if "research_families" in sql:
            return [{"family_id": params["fid"]}] if self.family_exists else []
        if "next_n" in sql:
            return [{"next_n": self.max_n + 1}]
        if "SELECT question_id" in sql:
            return [{"question_id": self.duplicate_of}] if self.duplicate_of else []
        if "INSERT INTO factory.research_questions" in sql:
            return [] if self.insert_conflicts else [{"question_id": params["qid"]}]
        return []

    def sql_for(self, fragment: str):
        return [c for c in self.calls if fragment in c["sql"]]


GOOD_NEED = "I cannot see realized slippage per paper trade; get_paper_feedback "\
            "returns only aggregates."


@pytest.mark.asyncio
async def test_a_concrete_gap_is_recorded_and_allocated_an_id():
    ex = GapExecutor(max_n=6)
    out = await writes.report_data_gap(ex, GOOD_NEED, agent_version_id="sci@v1")
    assert out["data"]["created"] is True
    assert out["data"]["question_id"] == "RQ-000007"


@pytest.mark.asyncio
async def test_a_vague_need_is_refused_and_writes_nothing():
    ex = GapExecutor()
    out = await writes.report_data_gap(ex, "need data", agent_version_id="a@v1")
    assert out["data"] is None
    assert "concrete need" in out["note"]
    assert ex.calls == []


@pytest.mark.asyncio
async def test_empty_need_is_refused():
    out = await writes.report_data_gap(GapExecutor(), "   ", agent_version_id="a@v1")
    assert out["data"] is None


@pytest.mark.asyncio
async def test_unknown_family_is_a_repairable_message_not_an_fk_explosion():
    """family_id is a FOREIGN KEY; an unchecked bad value raises through the Gateway."""
    ex = GapExecutor(family_exists=False)
    out = await writes.report_data_gap(ex, GOOD_NEED, agent_version_id="a@v1",
                                       family_id="no_such_family")
    assert out["data"] is None
    assert "unknown family_id" in out["note"]
    assert not ex.sql_for("INSERT INTO factory.research_questions")


@pytest.mark.asyncio
async def test_an_identical_open_gap_is_not_duplicated():
    """A retrying agent must not turn the signal queue into spam."""
    ex = GapExecutor(duplicate_of="RQ-000003")
    out = await writes.report_data_gap(ex, GOOD_NEED, agent_version_id="a@v1")
    assert out["data"] == {"question_id": "RQ-000003", "created": False}
    assert not ex.sql_for("INSERT INTO factory.research_questions")


@pytest.mark.asyncio
async def test_dedup_ignores_whitespace_and_case():
    ex = GapExecutor(duplicate_of="RQ-000003")
    noisy = "  " + GOOD_NEED.upper().replace(" ", "   ") + "\n"
    await writes.report_data_gap(ex, noisy, agent_version_id="a@v1")
    fp = ex.sql_for("SELECT question_id")[0]["params"]["fp"]
    assert fp == GOOD_NEED.lower().replace("  ", " ")


@pytest.mark.asyncio
async def test_source_is_agent_gap_and_fits_the_column():
    ex = GapExecutor()
    await writes.report_data_gap(ex, GOOD_NEED, agent_version_id="a@v1")
    p = ex.sql_for("INSERT INTO factory.research_questions")[0]["params"]
    assert p["source"] == "agent_gap"
    assert len(p["source"]) <= 16, "research_questions.source is String(16)"
    assert p["status"] == "open"


@pytest.mark.asyncio
async def test_evidence_refs_are_serialised_json_not_a_dict():
    ex = GapExecutor()
    await writes.report_data_gap(ex, GOOD_NEED, agent_version_id="sci@v1",
                                 blocked_tool="get_paper_feedback",
                                 evidence_refs=["FAIL-1", "EXP-2"])
    p = ex.sql_for("INSERT INTO factory.research_questions")[0]["params"]
    assert isinstance(p["refs"], str)
    import json
    refs = json.loads(p["refs"])
    assert refs["reported_by"] == "sci@v1"
    assert refs["refs"] == ["FAIL-1", "EXP-2"]
    assert refs["blocked_tool_known"] is True


@pytest.mark.asyncio
async def test_an_invented_blocked_tool_is_flagged_not_rejected():
    """Naming a tool that does not exist is itself signal."""
    ex = GapExecutor()
    out = await writes.report_data_gap(ex, GOOD_NEED, agent_version_id="a@v1",
                                       blocked_tool="get_tomorrows_prices")
    assert out["data"]["created"] is True
    import json
    refs = json.loads(ex.sql_for("INSERT INTO")[0]["params"]["refs"])
    assert refs["blocked_tool_known"] is False


@pytest.mark.asyncio
async def test_id_allocation_retries_on_conflict_then_gives_up_cleanly():
    ex = GapExecutor(insert_conflicts=True)
    out = await writes.report_data_gap(ex, GOOD_NEED, agent_version_id="a@v1")
    assert out["data"] is None
    assert len(ex.sql_for("INSERT INTO")) == writes.MAX_ID_ALLOCATION_RETRIES


@pytest.mark.asyncio
async def test_gap_report_touches_no_forbidden_table():
    ex = GapExecutor()
    await writes.report_data_gap(ex, GOOD_NEED, agent_version_id="a@v1")
    all_sql = " ".join(c["sql"] for c in ex.calls).lower()
    for forbidden in ("gate_decisions", "hypothesis_status", "public.",
                      "alpha_wealth_ledger", "holdout_access_log"):
        assert forbidden not in all_sql, forbidden
