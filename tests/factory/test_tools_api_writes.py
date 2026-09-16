"""submit_hypotheses: validation, repair, dedup and idempotency.

The load-bearing tests are the ones that prove an agent CANNOT do something:
cannot exceed the batch cap, cannot bypass the compiler, cannot submit two
identical specs in one payload, and cannot turn a retry into a duplicate row.
"""
from __future__ import annotations

from datetime import date
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


# ══ submit_attack_plan ═══════════════════════════════════════════════════

class AttackExecutor:
    def __init__(self, run_ids=(7, 8)):
        self.run_ids = list(run_ids)
        self.calls: list[dict] = []

    async def __call__(self, sql: str, params: dict):
        self.calls.append({"sql": sql, "params": params})
        if "experiment_runs" in sql:
            return [{"run_id": r} for r in self.run_ids]
        return []

    def inserts(self):
        return [c for c in self.calls if "INSERT INTO factory.robustness_tests" in c["sql"]]


MEMO = "Run 7 shows the effect concentrated in two names; drop_top5 should kill it."


@pytest.mark.asyncio
async def test_attack_plan_writes_requested_rows_with_catalogue_criticality():
    ex = AttackExecutor()
    out = await writes.submit_attack_plan(
        ex, "EXP-000001",
        [{"test_id": "drop_top5"}, {"test_id": "season_split"}],
        MEMO, agent_version_id="adversary@v1")
    d = out["data"]
    assert [a["test_id"] for a in d["accepted"]] == ["drop_top5", "season_split"]
    crit = {a["test_id"]: a["critical"] for a in d["accepted"]}
    assert crit["drop_top5"] is True and crit["season_split"] is False
    assert all(c["params"]["verdict"] == "requested" for c in ex.inserts())


@pytest.mark.asyncio
async def test_an_attack_outside_the_catalogue_is_refused():
    ex = AttackExecutor()
    out = await writes.submit_attack_plan(
        ex, "EXP-000001", [{"test_id": "delete_the_ledger"}], MEMO,
        agent_version_id="adversary@v1")
    assert out["data"]["accepted"] == []
    assert out["data"]["refused"][0]["reason"] == "not in the battery catalogue"
    assert ex.inserts() == []


@pytest.mark.asyncio
async def test_a_memo_citing_no_real_run_is_rejected_not_stored():
    """A kill argument that references nothing is rhetoric."""
    ex = AttackExecutor(run_ids=[7])
    out = await writes.submit_attack_plan(
        ex, "EXP-000001", [{"test_id": "cost_x2"}],
        "This result is obviously spurious.", agent_version_id="adversary@v1")
    assert out["data"] is None
    assert "cites no real run_id" in out["note"]
    assert ex.inserts() == []


@pytest.mark.asyncio
async def test_a_number_that_merely_contains_a_run_id_does_not_cite_it():
    """Substring matching would let 'we ran 15 tests' cite runs 1 and 5.

    The citation guard exists to refuse a memo that references nothing, and
    small run ids are exactly where a naive `rid in memo` check collapses.
    """
    ex = AttackExecutor(run_ids=[1, 5])
    out = await writes.submit_attack_plan(
        ex, "EXP-000001", [{"test_id": "cost_x2"}],
        "We ran 15 tests and the result looks fragile to me.",
        agent_version_id="adversary@v1")
    assert out["data"] is None
    assert "cites no real run_id" in out["note"]
    assert ex.inserts() == []


@pytest.mark.asyncio
async def test_a_memo_citing_a_run_by_number_is_accepted():
    ex = AttackExecutor(run_ids=[1, 5])
    out = await writes.submit_attack_plan(
        ex, "EXP-000001", [{"test_id": "cost_x2"}],
        "Run 5 concentrates the whole effect in two names.",
        agent_version_id="adversary@v1")
    assert out["data"]["cited_run_ids"] == ["5"]


@pytest.mark.asyncio
async def test_cited_runs_are_sorted_numerically_not_lexically():
    ex = AttackExecutor(run_ids=[2, 10])
    out = await writes.submit_attack_plan(
        ex, "EXP-000001", [{"test_id": "cost_x2"}],
        "Runs 10 and 2 both show the effect vanishing under cost.",
        agent_version_id="adversary@v1")
    assert out["data"]["cited_run_ids"] == ["2", "10"]


@pytest.mark.asyncio
async def test_no_runs_means_nothing_to_attack():
    ex = AttackExecutor(run_ids=[])
    out = await writes.submit_attack_plan(
        ex, "EXP-000001", [{"test_id": "cost_x2"}], MEMO,
        agent_version_id="adversary@v1")
    assert out["data"] is None and "nothing to attack" in out["note"]


@pytest.mark.asyncio
async def test_empty_memo_is_rejected():
    out = await writes.submit_attack_plan(
        AttackExecutor(), "EXP-000001", [{"test_id": "cost_x2"}], "   ",
        agent_version_id="a@v1")
    assert out["data"] is None and "kill memo is required" in out["note"]


@pytest.mark.asyncio
async def test_duplicate_attack_in_one_plan_is_refused_once():
    ex = AttackExecutor()
    out = await writes.submit_attack_plan(
        ex, "EXP-000001",
        [{"test_id": "cost_x2"}, {"test_id": "cost_x2"}], MEMO,
        agent_version_id="a@v1")
    assert len(out["data"]["accepted"]) == 1
    assert out["data"]["refused"][0]["reason"] == "already requested in this plan"


@pytest.mark.asyncio
async def test_attack_plan_is_capped():
    ex = AttackExecutor()
    many = [{"test_id": t.test_id} for t in __import__(
        "bharat_terminal.factory.tools_api.catalogue",
        fromlist=["BATTERY"]).BATTERY]
    out = await writes.submit_attack_plan(ex, "EXP-000001", many, MEMO,
                                          agent_version_id="a@v1")
    considered = len(out["data"]["accepted"]) + len(out["data"]["refused"])
    assert considered == writes.MAX_ATTACKS_PER_PLAN


@pytest.mark.asyncio
async def test_requested_by_is_truncated_but_the_full_id_survives_in_details():
    """robustness_tests.requested_by is varchar(16); truncation must not lose the id."""
    long_id = "adversary@2026-09-14"          # 20 chars
    ex = AttackExecutor()
    await writes.submit_attack_plan(ex, "EXP-000001", [{"test_id": "cost_x2"}],
                                    MEMO, agent_version_id=long_id)
    p = ex.inserts()[0]["params"]
    assert len(p["by"]) <= 16
    import json
    assert json.loads(p["details"])["requested_by_full"] == long_id


@pytest.mark.asyncio
async def test_plan_id_is_stable_across_processes():
    """Python's hash() is randomized per process; an audit id must not be."""
    ex1, ex2 = AttackExecutor(), AttackExecutor()
    a = await writes.submit_attack_plan(ex1, "EXP-000001", [{"test_id": "cost_x2"}],
                                        MEMO, agent_version_id="a@v1")
    b = await writes.submit_attack_plan(ex2, "EXP-000001", [{"test_id": "cost_x2"}],
                                        MEMO, agent_version_id="a@v1")
    assert a["data"]["plan_id"] == b["data"]["plan_id"]
    assert "hash" not in a["data"]["plan_id"]


@pytest.mark.asyncio
async def test_attack_plan_cannot_set_a_verdict():
    """The Adversary requests tests; it never decides them."""
    ex = AttackExecutor()
    await writes.submit_attack_plan(ex, "EXP-000001", [{"test_id": "year_loo"}],
                                    MEMO, agent_version_id="a@v1")
    verdicts = {c["params"]["verdict"] for c in ex.inserts()}
    assert verdicts == {"requested"}
    all_sql = " ".join(c["sql"] for c in ex.calls).lower()
    assert "gate_decisions" not in all_sql and "update" not in all_sql


# ══ submit_allocation ════════════════════════════════════════════════════

class AllocExecutor:
    def __init__(self, families=("pead", "guidance")):
        self.families = list(families)
        self.calls: list[dict] = []

    async def __call__(self, sql: str, params: dict):
        self.calls.append({"sql": sql, "params": params})
        if "research_families" in sql:
            return [{"family_id": f, "status": "active", "budget_cap_usd": 100}
                    for f in self.families]
        if "frontier_snapshots" in sql:
            return [{"snapshot_id": 11}]
        return []


@pytest.mark.asyncio
async def test_allocation_records_a_snapshot():
    ex = AllocExecutor()
    out = await writes.submit_allocation(
        ex, agent_version_id="chief@v1",
        allocations=[{"family_id": "pead", "budget_usd": 40}])
    assert out["data"]["snapshot_id"] == 11
    assert out["data"]["total_usd"] == 40


@pytest.mark.asyncio
async def test_a_kill_is_recommended_never_applied():
    """factory_writer has no UPDATE; a kill cannot change family status here."""
    ex = AllocExecutor()
    out = await writes.submit_allocation(
        ex, agent_version_id="chief@v1",
        kills=[{"family_id": "pead", "reason": "four failures, no mechanism"}])
    applied = [a for a in out["data"]["applied"] if a["item"] == "kill"]
    assert applied[0]["status"] == "recommended_not_applied"
    all_sql = " ".join(c["sql"] for c in ex.calls).upper()
    assert "UPDATE" not in all_sql


@pytest.mark.asyncio
async def test_a_kill_without_a_reason_is_refused():
    out = await writes.submit_allocation(
        AllocExecutor(), agent_version_id="c@v1",
        kills=[{"family_id": "pead", "reason": "  "}])
    assert out["data"]["refused"][0]["reason"] == "a kill must state a reason"


@pytest.mark.asyncio
async def test_exceeding_the_global_cap_records_nothing():
    ex = AllocExecutor()
    out = await writes.submit_allocation(
        ex, agent_version_id="c@v1", global_cap_usd=50,
        allocations=[{"family_id": "pead", "budget_usd": 40},
                     {"family_id": "guidance", "budget_usd": 30}])
    assert out["data"] is None and "global cap" in out["note"]
    assert not [c for c in ex.calls if "INSERT" in c["sql"]]


@pytest.mark.asyncio
async def test_unknown_family_and_negative_budget_are_refused():
    out = await writes.submit_allocation(
        AllocExecutor(), agent_version_id="c@v1",
        allocations=[{"family_id": "nope", "budget_usd": 10},
                     {"family_id": "pead", "budget_usd": -5}])
    reasons = {r["reason"] for r in out["data"]["refused"]}
    assert "unknown family_id" in reasons
    assert "budget must not be negative" in reasons


@pytest.mark.asyncio
async def test_prose_alone_cannot_reopen_a_paused_family():
    """Uses gates.engine.family_pause_check -- not a second implementation."""
    out = await writes.submit_allocation(
        AllocExecutor(), agent_version_id="c@v1",
        revisits=[{"family_id": "pead", "consecutive_failures": 99,
                   "evidence_refs": []}])
    assert out["data"]["applied"] == []
    assert "no new feature version" in out["data"]["refused"][0]["reason"]


@pytest.mark.asyncio
async def test_a_revisit_citing_new_evidence_is_allowed():
    out = await writes.submit_allocation(
        AllocExecutor(), agent_version_id="c@v1",
        revisits=[{"family_id": "pead", "consecutive_failures": 99,
                   "evidence_refs": ["sue_composite@2.2"]}])
    applied = [a for a in out["data"]["applied"] if a["item"] == "revisit"]
    assert applied and applied[0]["evidence_refs"] == ["sue_composite@2.2"]


# ══ submit_report_narrative ══════════════════════════════════════════════

class ReportExecutor:
    def __init__(self, conflict: bool = False):
        self.conflict = conflict
        self.calls: list[dict] = []

    async def __call__(self, sql: str, params: dict):
        self.calls.append({"sql": sql, "params": params})
        if "gate_decisions" in sql:
            return [{"hypothesis_id": "H-000001", "version": 1,
                     "to_status": "ROBUST", "promoted": True}]
        if "factory.failures" in sql:
            return []
        if "dataset_versions" in sql:
            return [{"dataset_version": "DS-20260913-02", "integrity": {}}]
        if "budget_ledger" in sql:
            return [{"category": "llm", "usd": 3.0}]
        if "daily_research_reports" in sql:
            return [] if self.conflict else [{"report_date": params["d"]}]
        return []

    def insert(self):
        return [c for c in self.calls
                if "INSERT INTO factory.daily_research_reports" in c["sql"]]


@pytest.mark.asyncio
async def test_facts_are_rederived_not_taken_from_the_agent():
    """An agent supplying its own facts could reconcile against fabrications."""
    ex = ReportExecutor()
    await writes.submit_report_narrative(
        ex, date(2026, 9, 14), {"summary": "One experiment completed."},
        agent_version_id="reporter@v1")
    assert any("gate_decisions" in c["sql"] for c in ex.calls), \
        "facts must be read from the ledger"


@pytest.mark.asyncio
async def test_an_unbacked_number_is_dropped_and_the_report_still_ships():
    ex = ReportExecutor()
    out = await writes.submit_report_narrative(
        ex, date(2026, 9, 14),
        {"summary": "The edge was 7.3% net and is risk-free."},
        agent_version_id="reporter@v1")
    assert out["data"]["dropped_count"] >= 1
    assert out["data"]["written"] is True, "a partial report still ships"


@pytest.mark.asyncio
async def test_a_second_report_for_the_same_date_is_a_noop():
    ex = ReportExecutor(conflict=True)
    out = await writes.submit_report_narrative(
        ex, date(2026, 9, 14), {"summary": "Anything."},
        agent_version_id="reporter@v1")
    assert out["data"]["written"] is False
    assert "already exists" in out["note"]


@pytest.mark.asyncio
async def test_claim_check_and_facts_are_serialised_json():
    ex = ReportExecutor()
    await writes.submit_report_narrative(
        ex, date(2026, 9, 14), {"summary": "One experiment completed."},
        agent_version_id="reporter@v1")
    p = ex.insert()[0]["params"]
    assert isinstance(p["facts"], str) and isinstance(p["claim_check"], str)


@pytest.mark.asyncio
async def test_report_write_touches_no_forbidden_table():
    ex = ReportExecutor()
    await writes.submit_report_narrative(
        ex, date(2026, 9, 14), {"summary": "x"}, agent_version_id="r@v1")
    inserts = " ".join(c["sql"] for c in ex.calls if "INSERT" in c["sql"]).lower()
    for forbidden in ("gate_decisions", "hypothesis_status", "public.",
                      "alpha_wealth_ledger"):
        assert forbidden not in inserts, forbidden


# ══ E016: an id may not be reused for different work ═════════════════════
#
# Found live on 2026-09-16. ON CONFLICT DO NOTHING is right for a replayed
# identical payload and actively wrong for a different payload under the same
# id: the write vanished, the tool said "accepted", and the agent's own summary
# then described a hypothesis the ledger did not hold.

class ClashExecutor(FakeExecutor):
    """A ledger that already contains one hypothesis."""

    def __init__(self, stored_spec):
        super().__init__()
        self.stored_spec = stored_spec

    async def __call__(self, sql: str, params: dict):
        self.calls.append({"sql": sql, "params": params})
        if "SELECT spec_json FROM factory.hypotheses" in sql:
            import json as _j
            return [{"spec_json": _j.dumps(self.stored_spec)}]
        if "RETURNING" in sql:
            return [{"hypothesis_id": params.get("hid")}]
        return []


@pytest.mark.asyncio
async def test_reusing_an_id_for_different_mechanics_is_refused():
    stored = dict(BASE_SPEC)
    stored["statistics"] = {**BASE_SPEC["statistics"], "primary_holding_days": 20}
    proposed = item()
    proposed["spec"] = dict(BASE_SPEC)
    proposed["spec"]["statistics"] = {**BASE_SPEC["statistics"],
                                      "primary_holding_days": 10}
    ex = ClashExecutor(stored)
    out = await submit_hypotheses(ex, [proposed], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    assert out["data"]["accepted"] == []
    err = out["data"]["rejected"][0]["errors"][0]
    assert err["code"] == "E016"
    assert "DIFFERENT" in err["message"]
    assert not ex.sql_for("INSERT INTO factory.hypotheses")


class ClashExecutorWithLedger(ClashExecutor):
    """A clashing ledger that can also answer "what is the highest id?".

    ClashExecutor returns [] for that query, which sends _suggest_free_id down
    its fallback branch -- so without this subclass the useful half of the
    error message is never exercised.
    """

    async def __call__(self, sql: str, params: dict):
        if "MAX(CAST(SUBSTRING(hypothesis_id" in sql:
            self.calls.append({"sql": sql, "params": params})
            return [{"mx": 7}]
        return await super().__call__(sql, params)


@pytest.mark.asyncio
async def test_e016_names_a_concrete_id_to_retry_with():
    """"Choose an unused id" is not actionable by a caller that cannot list them.

    The Scientist cannot see the id space, so on 2026-09-17 it guessed
    H-000002/4/5 -- all taken -- and burned 3 of 6 proposals on E016. The error
    now names the id to use, so the one permitted repair can actually succeed.
    """
    stored = dict(BASE_SPEC)
    stored["statistics"] = {**BASE_SPEC["statistics"], "primary_holding_days": 20}
    proposed = item()
    proposed["spec"] = dict(BASE_SPEC)
    proposed["spec"]["statistics"] = {**BASE_SPEC["statistics"],
                                      "primary_holding_days": 10}
    ex = ClashExecutorWithLedger(stored)
    out = await submit_hypotheses(ex, [proposed], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    err = out["data"]["rejected"][0]["errors"][0]
    assert err["code"] == "E016"
    assert "H-000008" in err["message"], err["message"]
    assert not ex.sql_for("INSERT INTO factory.hypotheses")


@pytest.mark.asyncio
async def test_e016_still_refuses_when_the_next_id_cannot_be_computed():
    """A broken suggestion query must not turn a refusal into an acceptance."""
    stored = dict(BASE_SPEC)
    stored["statistics"] = {**BASE_SPEC["statistics"], "primary_holding_days": 20}
    proposed = item()
    proposed["spec"] = dict(BASE_SPEC)
    proposed["spec"]["statistics"] = {**BASE_SPEC["statistics"],
                                      "primary_holding_days": 10}
    ex = ClashExecutor(stored)          # returns [] for the MAX query
    out = await submit_hypotheses(ex, [proposed], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    assert out["data"]["accepted"] == []
    assert out["data"]["rejected"][0]["errors"][0]["code"] == "E016"
    assert not ex.sql_for("INSERT INTO factory.hypotheses")


@pytest.mark.asyncio
async def test_replaying_identical_mechanics_is_still_a_no_op_not_an_error():
    """A Gateway retry must stay idempotent, not become a failure."""
    ex = ClashExecutor(dict(BASE_SPEC))
    out = await submit_hypotheses(ex, [item()], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    assert out["data"]["rejected"] == []
    assert len(out["data"]["accepted"]) == 1


@pytest.mark.asyncio
async def test_a_reworded_claim_over_identical_mechanics_is_a_replay():
    """spec_hash covers mechanics only, so prose changes must not collide."""
    stored = dict(BASE_SPEC)
    proposed = item()
    proposed["spec"] = dict(BASE_SPEC)
    proposed["spec"]["claim"] = {**BASE_SPEC["claim"],
                                 "description": "Completely different wording here."}
    ex = ClashExecutor(stored)
    out = await submit_hypotheses(ex, [proposed], ctx=ctx_with_counts(),
                                  agent_version_id="a@v1")
    assert out["data"]["rejected"] == [], "prose is not mechanics"
