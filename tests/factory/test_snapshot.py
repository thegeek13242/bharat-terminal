"""Snapshot manifests and integrity gates.

The timestamp test is built from the REAL measured distribution: 99 of 503
gaps sitting at +5.5h, the rest spread across a normal publishing lag.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from bharat_terminal.factory.snapshot.integrity import (
    FAIL,
    PASS,
    WARN,
    check_null_share,
    check_row_delta,
    check_snapshot_age,
    check_source_freshness,
    check_timestamp_ist_as_utc,
    check_universe_survivorship,
    run_integrity_suite,
)
from bharat_terminal.factory.snapshot.manifest import (
    Manifest,
    TableSnapshot,
    dataset_version_for,
    sha256_bytes,
    sha256_file,
)

NOW = datetime(2026, 9, 12, 2, 0, tzinfo=timezone.utc)


# ── manifest ─────────────────────────────────────────────────────────────

def test_dataset_version_format_and_bounds():
    assert dataset_version_for(date(2026, 9, 12), 1) == "DS-20260912-01"
    assert dataset_version_for(date(2026, 9, 12), 12) == "DS-20260912-12"
    with pytest.raises(ValueError):
        dataset_version_for(date(2026, 9, 12), 0)


def make_manifest(**kw) -> Manifest:
    base = dict(
        dataset_version="DS-20260912-01",
        created_at=NOW,
        sealed_from=date(2025, 9, 12),
        tables={
            "price_points": TableSnapshot("price_points", 3_672_681, "a" * 64,
                                          "price_points.parquet", "2026-09-11"),
            "earnings_surprises": TableSnapshot("earnings_surprises", 43_082, "b" * 64,
                                                "earnings_surprises.parquet", "2026-09-11"),
        },
        code_commit="7e72cd0" + "0" * 33,
    )
    base.update(kw)
    return Manifest(**base)


def test_manifest_round_trips_through_json():
    m = make_manifest()
    again = Manifest.from_dict(json.loads(m.to_json()))
    assert again.dataset_version == m.dataset_version
    assert again.sealed_from == m.sealed_from
    assert again.rows("price_points") == 3_672_681
    assert again.max_date("earnings_surprises") == date(2026, 9, 11)
    assert again.digest == m.digest


def test_manifest_digest_changes_with_content():
    a = make_manifest()
    b = make_manifest(tables={**a.tables,
                              "price_points": TableSnapshot("price_points", 3_672_680, "a" * 64,
                                                            "price_points.parquet", "2026-09-11")})
    assert a.digest != b.digest


def test_manifest_age_hours():
    m = make_manifest()
    assert m.age_hours(NOW + timedelta(hours=3)) == pytest.approx(3.0)


def test_manifest_verify_detects_tampering(tmp_path):
    payload = b"col_a,col_b\n1,2\n"
    path = tmp_path / "price_points.parquet"
    path.write_bytes(payload)
    m = make_manifest(tables={
        "price_points": TableSnapshot("price_points", 1, sha256_file(str(path)),
                                      "price_points.parquet", "2026-09-11"),
    })
    assert m.verify(str(tmp_path)) == []

    path.write_bytes(payload + b"3,4\n")          # someone rewrote the snapshot
    problems = m.verify(str(tmp_path))
    assert len(problems) == 1 and "sha256 mismatch" in problems[0]


def test_manifest_verify_reports_missing_file(tmp_path):
    m = make_manifest(tables={
        "price_points": TableSnapshot("price_points", 1, sha256_bytes(b"x"),
                                      "missing.parquet", "2026-09-11"),
    })
    assert "file missing" in m.verify(str(tmp_path))[0]


def test_unknown_table_lookups_are_none():
    m = make_manifest()
    assert m.rows("nope") is None and m.max_date("nope") is None


# ── age and freshness ────────────────────────────────────────────────────

def test_snapshot_age_fails_closed_when_stale():
    assert check_snapshot_age(NOW, NOW + timedelta(hours=3)).status == PASS
    assert check_snapshot_age(NOW, NOW + timedelta(hours=48)).status == FAIL


def test_freshness_passes_when_current():
    r = check_source_freshness("price_points", date(2026, 9, 11), date(2026, 9, 11))
    assert r.status == PASS


def test_freshness_counts_sessions_not_calendar_days():
    """A Monday snapshot must not read a long weekend as a stalled feed."""
    trading = [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14)]
    r = check_source_freshness("price_points", date(2026, 9, 11), date(2026, 9, 14),
                               trading_dates=trading)
    assert r.status == PASS       # one session behind
    r2 = check_source_freshness("price_points", date(2026, 9, 10), date(2026, 9, 14),
                                trading_dates=trading)
    assert r2.status == FAIL      # two sessions behind


def test_freshness_fails_on_empty_source():
    assert check_source_freshness("result_events", None, date(2026, 9, 11)).status == FAIL


# ── row deltas ───────────────────────────────────────────────────────────

def test_row_delta_fails_on_truncation():
    r = check_row_delta("price_points", 3_000_000, 3_672_681)
    assert r.status == FAIL and r.value < -5


def test_row_delta_passes_on_normal_growth():
    assert check_row_delta("price_points", 3_676_421, 3_672_681).status == PASS


def test_row_delta_warns_on_implausible_growth():
    assert check_row_delta("earnings_surprises", 90_000, 43_082).status == WARN


def test_row_delta_without_history_is_not_a_failure():
    assert check_row_delta("factory_new_table", 10, None).status == PASS


# ── the live timestamp finding ───────────────────────────────────────────

def measured_gaps() -> list:
    """503 gaps: 99 piled at +5.5h, the rest a plausible publishing lag."""
    spike = [5.5] * 99
    lag = [0.0] * 250 + [0.5] * 60 + [1.0] * 40 + [2.0] * 30 + [3.5] * 14 + [8.0] * 10
    return spike + lag


def test_ist_as_utc_spike_is_detected():
    r = check_timestamp_ist_as_utc(measured_gaps())
    assert r.flag == "ts_ist_as_utc"
    assert r.status == WARN                 # a flag, not a block
    assert r.value == pytest.approx(99 / 503, abs=1e-3)


def test_clean_publishing_lag_raises_no_flag():
    clean = [0.0] * 300 + [0.5] * 100 + [1.5] * 60 + [3.0] * 43
    r = check_timestamp_ist_as_utc(clean)
    assert r.flag is None and r.status == PASS


def test_tolerance_does_not_catch_a_genuine_five_hour_lag():
    r = check_timestamp_ist_as_utc([5.0] * 200 + [0.0] * 300)
    assert r.flag is None


def test_no_overlapping_sightings_warns_rather_than_passing_silently():
    assert check_timestamp_ist_as_utc([]).status == WARN


# ── survivorship ─────────────────────────────────────────────────────────

def test_survivorship_flag_on_todays_universe():
    r = check_universe_survivorship(25, 3923)
    assert r.flag == "survivorship" and r.status == WARN


def test_survivorship_clear_when_delistings_are_normal():
    assert check_universe_survivorship(120, 3923).flag is None


# ── the suite ────────────────────────────────────────────────────────────

def test_suite_collects_failures_and_flags():
    suite = run_integrity_suite(
        created_at=NOW,
        now=NOW + timedelta(hours=2),
        freshness=[("price_points", date(2026, 9, 11), date(2026, 9, 11)),
                   ("result_events", None, date(2026, 9, 11))],
        row_counts=[("price_points", 3_672_681, 3_670_000)],
        null_shares=[("companies", "sector_nse", 0.812)],
        timestamp_gaps_hours=measured_gaps(),
        survivorship=(25, 3923),
    )
    assert suite.ok is False
    assert "freshness:result_events" in suite.failures
    assert set(suite.flags) == {"ts_ist_as_utc", "survivorship"}
    d = suite.as_dict()
    # age + 2 freshness + row delta + null share + timestamp + survivorship
    assert d["ok"] is False and len(d["results"]) == 7


def test_healthy_snapshot_passes_with_flags_only():
    suite = run_integrity_suite(
        created_at=NOW,
        now=NOW + timedelta(hours=1),
        freshness=[("price_points", date(2026, 9, 11), date(2026, 9, 11))],
        row_counts=[("price_points", 3_672_681, 3_670_000)],
        timestamp_gaps_hours=measured_gaps(),
        survivorship=(25, 3923),
    )
    assert suite.ok is True
    assert suite.failures == ()
    assert "ts_ist_as_utc" in suite.flags
