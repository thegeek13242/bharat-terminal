"""Snapshot manifests and integrity gates.

The clock fixtures mirror the REAL measured distribution (2026-09-12, 502
matched pairs): median 0.91h, p25 0.09h, p75 5.81h, p90 7.83h, 3.4% negative,
bimodal because bse_xbrl lands in ~0.2h and nse_xbrl in ~6.2h.

The pair of clock tests encodes the correction that cost us a wrong claim: a
publication lag must NOT read as a timezone mislabel, and a real constant
offset must still be caught.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from bharat_terminal.factory.snapshot.integrity import (
    FAIL,
    PASS,
    WARN,
    check_clock_lag,
    check_constant_offset,
    check_row_delta,
    check_series_continuity,
    check_snapshot_age,
    check_source_freshness,
    check_universe_survivorship,
    clock_lag_stats,
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


def measured_gaps() -> list:
    """502 gaps shaped like production: two feeds, no constant offset."""
    return (
        [0.0] * 180 + [0.2] * 30 + [0.9] * 60 + [1.5] * 40 + [3.0] * 40
        + [5.7] * 76 + [6.5] * 40 + [7.8] * 30 + [12.0] * 6
    )


def mislabelled_gaps() -> list:
    """What a genuine IST-stored-as-UTC bug would look like: a delta at 5.5."""
    return [5.5] * 100 + [0.0] * 300 + [0.5] * 60 + [1.5] * 42


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
                                          "price_points.csv.gz", "2026-09-11"),
            "earnings_surprises": TableSnapshot("earnings_surprises", 43_082, "b" * 64,
                                                "earnings_surprises.csv.gz", "2026-09-11"),
        },
        code_commit="e438be3" + "0" * 33,
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
                                                            "price_points.csv.gz", "2026-09-11")})
    assert a.digest != b.digest


def test_manifest_age_hours():
    assert make_manifest().age_hours(NOW + timedelta(hours=3)) == pytest.approx(3.0)


def test_manifest_verify_detects_tampering(tmp_path):
    payload = b"col_a,col_b\n1,2\n"
    path = tmp_path / "price_points.csv.gz"
    path.write_bytes(payload)
    m = make_manifest(tables={
        "price_points": TableSnapshot("price_points", 1, sha256_file(str(path)),
                                      "price_points.csv.gz", "2026-09-11"),
    })
    assert m.verify(str(tmp_path)) == []

    path.write_bytes(payload + b"3,4\n")          # someone rewrote the snapshot
    problems = m.verify(str(tmp_path))
    assert len(problems) == 1 and "sha256 mismatch" in problems[0]


def test_manifest_verify_reports_missing_file(tmp_path):
    m = make_manifest(tables={
        "price_points": TableSnapshot("price_points", 1, sha256_bytes(b"x"),
                                      "missing.csv.gz", "2026-09-11"),
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
    assert check_source_freshness("price_points", date(2026, 9, 11),
                                  date(2026, 9, 11)).status == PASS


def test_freshness_counts_sessions_not_calendar_days():
    """A Monday snapshot must not read a long weekend as a stalled feed."""
    trading = [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 14)]
    assert check_source_freshness("price_points", date(2026, 9, 11), date(2026, 9, 14),
                                  trading_dates=trading).status == PASS
    assert check_source_freshness("price_points", date(2026, 9, 10), date(2026, 9, 14),
                                  trading_dates=trading).status == FAIL


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


# ── clock quality: lag vs constant offset ────────────────────────────────

def test_lag_stats_match_the_production_shape():
    st = clock_lag_stats(measured_gaps())
    assert st.n == 502
    assert st.median == pytest.approx(0.9, abs=0.2)
    assert st.p90 >= 6.0
    assert st.share_within_30min == pytest.approx(0.42, abs=0.05)


def test_lagging_clock_is_flagged():
    r = check_clock_lag(measured_gaps())
    assert r.flag == "clock_lags_dissemination"
    assert r.status == WARN            # a flag, not a block


def test_prompt_clock_raises_no_lag_flag():
    quick = [0.0] * 300 + [0.1] * 150 + [0.3] * 52
    assert check_clock_lag(quick).flag is None


def test_publication_lag_is_not_reported_as_a_timezone_bug():
    """The correction: the NSE XBRL hump peaks near 5.7h and must NOT fire."""
    r = check_constant_offset(measured_gaps())
    assert r.status == PASS and r.flag is None


def test_a_real_constant_offset_is_still_caught():
    r = check_constant_offset(mislabelled_gaps())
    assert r.flag == "clock_constant_offset"
    assert r.status == WARN


def test_constant_offset_needs_concentration_not_just_volume():
    """A broad hump centred exactly on 5.5 is still a lag, not an offset."""
    hump = ([5.3] * 60 + [5.4] * 60 + [5.5] * 70 + [5.6] * 60 + [5.7] * 60
            + [0.0] * 192)
    assert check_constant_offset(hump).flag is None


def test_no_overlapping_sightings_warns_rather_than_passing_silently():
    assert check_clock_lag([]).status == WARN
    assert check_constant_offset([]).status == WARN


# ── series continuity: the benchmark hole ────────────────────────────────

def market_sessions(n: int = 300, start: date = date(2024, 1, 1)) -> list:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_continuous_series_passes():
    sessions = market_sessions()
    assert check_series_continuity("NIFTYBEES", sessions, sessions).status == PASS


def test_holed_benchmark_fails():
    """The real case: NIFTYBEES stops 2025-05-08 and resumes 2026-06-04."""
    sessions = market_sessions()
    holed = sessions[:100] + sessions[200:]          # 100 sessions missing
    r = check_series_continuity("NIFTYBEES", holed, sessions)
    assert r.status == FAIL
    assert r.flag == "benchmark_gap"
    assert "largest gap 100" in r.detail


def test_short_gap_is_tolerated():
    sessions = market_sessions()
    minor = [d for i, d in enumerate(sessions) if i not in (50, 51)]
    assert check_series_continuity("NIFTYBEES", minor, sessions).status == PASS


def test_young_series_is_judged_only_over_its_own_life():
    """MID150BEES starts 2022-05; that is not a gap."""
    sessions = market_sessions()
    young = sessions[200:]
    assert check_series_continuity("MID150BEES", young, sessions).status == PASS


def test_empty_series_fails():
    assert check_series_continuity("GONE", [], market_sessions()).status == FAIL


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
    assert set(suite.flags) == {"clock_lags_dissemination", "survivorship"}
    d = suite.as_dict()
    # age + 2 freshness + row delta + null share + clock lag + constant offset
    # + survivorship
    assert d["ok"] is False and len(d["results"]) == 8


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
    assert "clock_lags_dissemination" in suite.flags
    assert "clock_constant_offset" not in suite.flags
