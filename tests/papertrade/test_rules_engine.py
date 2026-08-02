"""RulesEngine.evaluate_report — the gate cascade that turns an ImpactReport
into auditable intents. Pool and fno_supplier are constructor-injected; the
event-type bucketing is patched per-test so these tests isolate the CASCADE
(the bucketing itself belongs to the analysis suite).

The materiality artifact is forced UNLOADED (gate._art = None) so the paper
fail-open contract is what's under test — scoring against a real artifact
needs a small synthetic joblib fixture (future work, design doc §2.3).
"""
from datetime import datetime, timedelta, timezone

import pytest

from bharat_terminal.papertrade import rules as rules_mod
from bharat_terminal.papertrade.rules import RulesEngine


def make_rules(pool, fno=frozenset()):
    async def supplier():
        return set(fno)

    re = RulesEngine(pool, supplier)
    re.gate._art = None            # deterministic fail-open regardless of env
    return re


@pytest.fixture
def etype(monkeypatch):
    """Patch the event-type bucketing to a fixed value."""
    def _set(value):
        monkeypatch.setattr(rules_mod, "_event_type", lambda headline: value)
    return _set


# ── report-level gates ───────────────────────────────────────────────────

async def test_irrelevant_report_yields_nothing(pool, make_report, etype):
    etype("other")
    out = await make_rules(pool).evaluate_report(make_report(relevant=False))
    assert out == []


async def test_non_filing_source_gated(pool, make_report, etype):
    etype("other")
    out = await make_rules(pool).evaluate_report(
        make_report(source="ECONOMIC_TIMES"))
    assert out == []


async def test_hop1_impacts_skipped(pool, make_report, etype):
    etype("other")
    out = await make_rules(pool).evaluate_report(make_report(hop=1))
    assert out == []


# ── headwind_short ───────────────────────────────────────────────────────

async def test_headwind_signal_on_fresh_negative_mag4(pool, make_report,
                                                      etype):
    etype("other")
    (intent,) = await make_rules(pool).evaluate_report(
        make_report(sentiment="negative", magnitude=4))
    assert intent.strategy == "headwind_short"
    assert intent.side == "SHORT"
    assert intent.instrument == "CASH"             # not in F&O
    assert intent.rejected_reason is None
    # artifact unavailable -> FAIL-OPEN with provenance (paper stance)
    assert intent.provenance["gate_fallback"] is True
    assert intent.provenance["materiality_p"] is None
    assert intent.provenance["gate_flags"]["fresh"] is True


async def test_headwind_requires_min_magnitude(pool, make_report, etype):
    # S0 correction: shorts take magnitude >= HEADWIND_MIN_MAG (4) only
    etype("other")
    out = await make_rules(pool).evaluate_report(
        make_report(sentiment="negative", magnitude=rules_mod.HEADWIND_MIN_MAG - 1))
    assert out == []


async def test_headwind_stale_event_is_auditable_reject(pool, make_report,
                                                        etype):
    etype("other")
    (intent,) = await make_rules(pool).evaluate_report(
        make_report(sentiment="negative", magnitude=4,
                    age_s=rules_mod.PAPER_MAX_EVENT_AGE_S + 300))
    assert intent.rejected_reason == "stale_event"
    assert intent.provenance["gate_flags"]["fresh"] is False


async def test_headwind_fno_member_gets_futures(pool, make_report, etype):
    etype("other")
    (intent,) = await make_rules(pool, fno={"TEST"}).evaluate_report(
        make_report(sentiment="negative", magnitude=5))
    assert intent.instrument == "FUT"
    assert intent.provenance["gate_flags"]["fno"] is True


async def test_headwind_duplicate_position_rejected(pool, make_report, etype):
    # Concentration guard: VSTIND filed two negatives in one evening —
    # the second must become an auditable REJECTED intent, not stack exposure.
    etype("other")
    pool.live_pos.add(("TEST", "headwind_short"))
    (intent,) = await make_rules(pool).evaluate_report(
        make_report(sentiment="negative", magnitude=4))
    assert intent.rejected_reason == "duplicate_position"
    assert intent.provenance["gate_flags"]["duplicate_position"] is True


async def test_naive_timestamp_treated_as_utc(pool, make_report, etype):
    # Serialized timestamps sometimes round-trip tz-naive; _aware_utc must
    # read them as UTC, keeping a 60s-old event fresh.
    etype("other")
    report = make_report(sentiment="negative", magnitude=4)
    naive_utc = (datetime.now(timezone.utc) - timedelta(seconds=60)
                 ).replace(tzinfo=None)
    ni = report.news_item.model_copy(update={"timestamp_utc": naive_utc})
    report = report.model_copy(update={"news_item": ni})
    (intent,) = await make_rules(pool).evaluate_report(report)
    assert intent.rejected_reason is None


# ── pead_long ────────────────────────────────────────────────────────────

async def test_pead_signal_when_sue_clears_threshold(pool, make_report,
                                                     etype):
    etype("results")
    pool.pead_sue["TEST"] = 2.5
    pool.sue_threshold = 1.5
    (intent,) = await make_rules(pool).evaluate_report(
        make_report(sentiment="positive", magnitude=3))
    assert intent.strategy == "pead_long"
    assert intent.side == "LONG" and intent.instrument == "CASH"
    assert intent.provenance["sue_composite"] == 2.5
    assert intent.provenance["gate_flags"]["sue_pass"] is True


async def test_pead_requires_results_event(pool, make_report, etype):
    etype("order_win")
    pool.pead_sue["TEST"] = 2.5
    pool.sue_threshold = 1.5
    out = await make_rules(pool).evaluate_report(
        make_report(sentiment="neutral", magnitude=3))
    assert out == []


async def test_pead_gated_without_surprise_row(pool, make_report, etype):
    etype("results")
    pool.sue_threshold = 1.5                       # threshold exists, no SUE
    out = await make_rules(pool).evaluate_report(
        make_report(sentiment="positive", magnitude=3))
    assert out == []


async def test_pead_gated_when_threshold_window_empty(pool, make_report,
                                                      etype):
    etype("results")
    pool.pead_sue["TEST"] = 2.5
    pool.sue_threshold = None
    out = await make_rules(pool).evaluate_report(
        make_report(sentiment="positive", magnitude=3))
    assert out == []


async def test_pead_gated_below_threshold(pool, make_report, etype):
    etype("results")
    pool.pead_sue["TEST"] = 1.0
    pool.sue_threshold = 1.5
    out = await make_rules(pool).evaluate_report(
        make_report(sentiment="positive", magnitude=3))
    assert out == []


async def test_sue_threshold_cached_for_an_hour(pool, make_report, etype):
    etype("results")
    pool.pead_sue["TEST"] = 2.5
    pool.sue_threshold = 1.5
    re = make_rules(pool)
    await re.evaluate_report(make_report(sentiment="positive", magnitude=3))
    await re.evaluate_report(make_report(sentiment="positive", magnitude=3,
                                         symbol="OTHER"))
    assert pool.sue_threshold_queries == 1


# ── orderwin_long ────────────────────────────────────────────────────────

async def test_orderwin_signal_ungated_by_materiality(pool, make_report,
                                                      etype):
    # D0T: unconditional on magnitude/materiality — p recorded, never gated
    etype("order_win")
    (intent,) = await make_rules(pool).evaluate_report(
        make_report(sentiment="positive", magnitude=1))
    assert intent.strategy == "orderwin_long"
    assert intent.instrument == "CASH"             # cash-only by design
    assert intent.rejected_reason is None
    assert intent.provenance["gate_flags"]["materiality_p"] is None


async def test_orderwin_requires_positive_sentiment(pool, make_report, etype):
    etype("order_win")
    out = await make_rules(pool).evaluate_report(
        make_report(sentiment="neutral", magnitude=3))
    assert out == []


# ── tailwind_long (default OFF) ──────────────────────────────────────────

async def test_tailwind_disabled_by_default(pool, make_report, etype):
    etype("other")
    out = await make_rules(pool).evaluate_report(
        make_report(sentiment="positive", magnitude=5))
    assert out == []
    assert rules_mod.ENABLE_TAILWIND_LONG is False
