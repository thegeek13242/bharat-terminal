"""The 24-hour report: facts, reconciliation, rendering, inert delivery.

The load-bearing tests here are the negative ones — a quiet day must say so,
an unbacked number must not ship, and ACTION REQUIRED must be computed from
ledger state rather than written by an agent.
"""
from __future__ import annotations

from datetime import date

import pytest

from bharat_terminal.factory.report.deliver import (
    DeliveryPayload,
    build_payload,
    dead_man_alarm_spec,
    deliver,
)
from bharat_terminal.factory.report.facts import (
    LOW_ACTIVITY_THRESHOLD,
    ReportFacts,
    Scorecard,
    build_facts,
    compute_action_required,
)
from bharat_terminal.factory.report.reconcile import (
    reconcile_narrative,
    split_sentences,
)
from bharat_terminal.factory.report.render import render_text, subject

TODAY = date(2026, 9, 13)


def cycles(n: int = 5, promoted: int = 2) -> list:
    out = []
    for i in range(n):
        out.append({
            "hypothesis_id": f"H-{i:06d}",
            "experiment_id": f"EXP-{i:06d}",
            "promoted": i < promoted,
            "to_status": "ROBUST" if i < promoted else "SIMULATED",
        })
    return out


# ── facts ────────────────────────────────────────────────────────────────

def test_scorecard_counts_from_cycles():
    f = build_facts(TODAY, cycles=cycles(5, 2), llm_usd=12.5, eval_usd=2.0)
    assert f.scorecard.experiments_completed == 5
    assert f.scorecard.hypotheses_tested == 5
    assert f.scorecard.promoted == 2
    assert f.scorecard.llm_usd == 12.5


def test_quiet_day_is_flagged_as_low_activity():
    f = build_facts(TODAY, cycles=cycles(1))
    assert f.activity_low is True
    assert LOW_ACTIVITY_THRESHOLD == 3


def test_busy_day_is_not_low_activity():
    assert build_facts(TODAY, cycles=cycles(9)).activity_low is False


def test_no_activity_at_all_still_produces_facts():
    f = build_facts(TODAY)
    assert f.activity_low is True
    assert f.scorecard.experiments_completed == 0


# ── ACTION REQUIRED is computed, never authored ──────────────────────────

def test_clean_day_requires_no_action():
    f = build_facts(TODAY, cycles=cycles(5))
    assert f.action_required is False
    assert f.actions == []


def test_production_candidate_demands_attention():
    c = cycles(3)
    c[0]["to_status"] = "PRODUCTION_CANDIDATE"
    f = build_facts(TODAY, cycles=c)
    assert f.action_required
    assert any("PRODUCTION CANDIDATE" in a for a in f.actions)


def test_integrity_failure_demands_attention():
    f = build_facts(TODAY, cycles=cycles(4), integrity_failures=["freshness:price_points"])
    assert any("integrity FAILURE" in a for a in f.actions)


def test_stale_snapshot_demands_attention():
    f = build_facts(TODAY, cycles=cycles(4), snapshot_age_hours=51.0)
    assert any("stale" in a for a in f.actions)


def test_failed_previous_delivery_demands_attention():
    f = build_facts(TODAY, cycles=cycles(4), previous_delivery_failed=True)
    assert any("failed to deliver" in a for a in f.actions)


def test_exhausted_alpha_wealth_freezes_promotions():
    f = build_facts(TODAY, cycles=cycles(4), alpha_wealth_remaining=0.0)
    assert any("Alpha wealth exhausted" in a for a in f.actions)


def test_budget_threshold_triggers_at_eighty_percent():
    f = build_facts(TODAY, cycles=cycles(4), llm_usd=800.0, budget_cap_usd=1000.0)
    assert any("cap" in a for a in f.actions)
    quiet = build_facts(TODAY, cycles=cycles(4), llm_usd=500.0, budget_cap_usd=1000.0)
    assert not any("cap" in a for a in quiet.actions)


def test_failing_quality_gate_demands_attention():
    f = build_facts(TODAY, cycles=cycles(4), agent_quality={"e7_failing": True})
    assert any("e7_failing" in a for a in f.actions)


def test_actions_are_order_stable():
    c = cycles(2)
    c[0]["to_status"] = "PRODUCTION_CANDIDATE"
    kw = dict(cycles=c, pending_approvals=["deploy executor"],
              integrity_failures=["freshness:x"])
    assert build_facts(TODAY, **kw).actions == build_facts(TODAY, **kw).actions


# ── reconciliation ───────────────────────────────────────────────────────

def test_sentence_splitter_survives_abbreviations():
    s = split_sentences("D10 beat D1 vs. the benchmark. Costs were Rs. 90 per leg. Done.")
    assert len(s) == 3
    assert "vs." in s[0]


def test_backed_numbers_are_kept():
    facts = build_facts(TODAY, cycles=cycles(5, 2))
    r = reconcile_narrative("We completed 5 experiments and promoted 2.",
                            facts.numbers(), facts.identifiers())
    assert r.clean and r.kept


def test_unbacked_number_drops_the_sentence():
    facts = build_facts(TODAY, cycles=cycles(5, 2))
    r = reconcile_narrative("We completed 5 experiments. The edge was 7.3% net.",
                            facts.numbers(), facts.identifiers())
    assert r.dropped_count == 1
    assert "unbacked_number:7.3" in r.dropped[0][1]
    assert "7.3%" not in r.text


def test_unknown_identifier_drops_the_sentence():
    facts = build_facts(TODAY, cycles=cycles(3))
    r = reconcile_narrative("H-999999 looks promising.", facts.numbers(), facts.identifiers())
    assert r.dropped_count == 1
    assert "unknown_id" in r.dropped[0][1]


def test_known_identifier_is_kept():
    facts = build_facts(TODAY, cycles=cycles(3))
    r = reconcile_narrative("H-000000 reached its gate.", facts.numbers(), facts.identifiers())
    assert r.clean


def test_overclaims_are_refused():
    facts = build_facts(TODAY, cycles=cycles(5))
    r = reconcile_narrative("This proves the strategy is risk-free.",
                            facts.numbers(), facts.identifiers())
    assert r.dropped_count == 1
    assert "overclaim" in r.dropped[0][1]


def test_precision_aware_tolerance():
    """12.2% must not pass against a stored 12.63%."""
    assert reconcile_narrative("It moved 12.2%.", [12.63]).dropped_count == 1
    assert reconcile_narrative("It moved 12.6%.", [12.63]).clean


def test_percentage_of_a_stored_fraction_is_accepted():
    assert reconcile_narrative("Net was 1.91%.", [0.0191]).clean


def test_reconcile_result_serializes():
    d = reconcile_narrative("Bogus 999 figure.", [1.0]).as_dict()
    assert d["clean"] is False and d["dropped"][0]["reason"].startswith("unbacked")


# ── rendering ────────────────────────────────────────────────────────────

def test_render_states_low_activity_plainly():
    text = render_text(build_facts(TODAY, cycles=cycles(1)))
    assert "Research activity was LOW" in text


def test_render_says_no_action_when_clean():
    text = render_text(build_facts(TODAY, cycles=cycles(5)))
    assert "NO ACTION REQUIRED" in text
    assert "ACTION REQUIRED\n" not in text.split("NO ACTION REQUIRED")[0]


def test_render_lists_actions_when_required():
    text = render_text(build_facts(TODAY, cycles=cycles(4), integrity_failures=["x"]))
    assert "ACTION REQUIRED" in text
    assert "integrity FAILURE" in text


def test_render_survives_a_missing_narrative():
    text = render_text(build_facts(TODAY, cycles=cycles(6)), narrative=None)
    assert "narrative unavailable" in text


def test_subject_carries_the_date():
    assert subject(build_facts(TODAY)).endswith("2026-09-13")


# ── delivery stays inert ─────────────────────────────────────────────────

def test_delivery_is_inert_without_a_transport():
    payload = build_payload(build_facts(TODAY, cycles=cycles(3)), "founder@example.com")
    r = deliver(payload)
    assert r.sent is False
    assert r.inert_reason == "no transport configured"


def test_successful_send_requires_ok_true():
    class R:
        status_code = 200
        def json(self): return {"ok": True}

    seen = {}
    r = deliver(DeliveryPayload("a@b.c", "s", "t"),
                transport=lambda body: (seen.update(body) or R()),
                metric_sink=lambda ns, name, v: None)
    assert r.sent and r.attempts == 1 and r.metric_pushed
    assert seen["to"] == "a@b.c" and "<pre>" in seen["html"]


def test_two_hundred_without_ok_is_not_delivery():
    class R:
        status_code = 200
        def json(self): return {}

    r = deliver(DeliveryPayload("a@b.c", "s", "t"), transport=lambda body: R())
    assert r.sent is False and r.attempts == 3


def test_transport_errors_are_retried_then_reported():
    calls = {"n": 0}

    def boom(body):
        calls["n"] += 1
        raise RuntimeError("flow down")

    r = deliver(DeliveryPayload("a@b.c", "s", "t"), transport=boom)
    assert r.sent is False and calls["n"] == 3 and "flow down" in r.error


def test_metric_failure_does_not_void_a_real_delivery():
    class R:
        status_code = 200
        def json(self): return {"ok": True}

    def bad_metric(ns, name, v):
        raise RuntimeError("no cloudwatch permission")

    r = deliver(DeliveryPayload("a@b.c", "s", "t"), transport=lambda b: R(),
                metric_sink=bad_metric)
    assert r.sent is True and r.metric_pushed is False
    assert "metric push failed" in r.error


def test_dead_man_alarm_treats_silence_as_breach():
    spec = dead_man_alarm_spec()
    assert spec["TreatMissingData"] == "breaching"
    assert spec["Period"] == 86400
    assert spec["ComparisonOperator"] == "LessThanThreshold"
