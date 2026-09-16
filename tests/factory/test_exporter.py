"""Snapshot exporter.

No database here: these cover the parts that must be right for a snapshot to
mean anything — determinism of the bytes, honest row counts, read-only SQL, and
a manifest that verifies against what was actually written.
"""
from __future__ import annotations

import gzip
import os
from datetime import date


from bharat_terminal.factory.snapshot.exporter import (
    SPECS_V1,
    TableSpec,
    copy_sql,
    psql_argv,
    write_csv_gz,
)
from bharat_terminal.factory.snapshot.manifest import Manifest, TableSnapshot

ROWS = [("ABC", "2026-09-11", 100.5, 101.0), ("XYZ", "2026-09-11", 55.0, 54.25)]
HEADER = ("symbol", "date", "open", "close")


# ── determinism: the whole reproducibility story rests on this ───────────

def test_same_data_produces_the_same_hash(tmp_path):
    a = write_csv_gz(str(tmp_path / "a.csv.gz"), HEADER, ROWS)
    b = write_csv_gz(str(tmp_path / "b.csv.gz"), HEADER, ROWS)
    assert a.sha256 == b.sha256


def test_changed_data_changes_the_hash(tmp_path):
    a = write_csv_gz(str(tmp_path / "a.csv.gz"), HEADER, ROWS)
    changed = [ROWS[0], ("XYZ", "2026-09-11", 55.0, 54.26)]
    b = write_csv_gz(str(tmp_path / "b.csv.gz"), HEADER, changed)
    assert a.sha256 != b.sha256


def test_gzip_header_carries_no_timestamp(tmp_path):
    """A gzip mtime would make every export differ from the last one."""
    path = tmp_path / "a.csv.gz"
    write_csv_gz(str(path), HEADER, ROWS)
    raw = path.read_bytes()
    assert raw[4:8] == b"\x00\x00\x00\x00"


# ── row counting and content ─────────────────────────────────────────────

def test_row_count_excludes_the_header(tmp_path):
    result = write_csv_gz(str(tmp_path / "a.csv.gz"), HEADER, ROWS)
    assert result.rows == 2


def test_empty_table_is_zero_rows_not_one(tmp_path):
    result = write_csv_gz(str(tmp_path / "empty.csv.gz"), HEADER, [])
    assert result.rows == 0


def test_content_round_trips(tmp_path):
    path = tmp_path / "a.csv.gz"
    write_csv_gz(str(path), HEADER, ROWS)
    with gzip.open(path, "rt") as fh:
        lines = fh.read().splitlines()
    assert lines[0] == "symbol,date,open,close"
    assert lines[1] == "ABC,2026-09-11,100.5,101.0"
    assert len(lines) == 3


def test_nulls_render_as_empty_fields(tmp_path):
    path = tmp_path / "n.csv.gz"
    write_csv_gz(str(path), HEADER, [("ABC", "2026-09-11", None, 101.0)])
    with gzip.open(path, "rt") as fh:
        assert fh.read().splitlines()[1] == "ABC,2026-09-11,,101.0"


def test_reported_size_matches_the_file(tmp_path):
    path = tmp_path / "a.csv.gz"
    result = write_csv_gz(str(path), HEADER, ROWS)
    assert result.bytes == os.path.getsize(path)


# ── SQL is read-only by construction ─────────────────────────────────────

def test_copy_sql_is_a_read():
    sql = copy_sql(SPECS_V1[0])
    assert sql.startswith("COPY (SELECT ")
    assert "TO STDOUT" in sql
    lowered = sql.lower()
    assert not any(w in lowered for w in
                   (" insert ", " update ", " delete ", " drop ", " alter ", " truncate "))


def test_every_shipped_spec_is_a_select_with_deterministic_order():
    for spec in SPECS_V1:
        assert spec.sql.lstrip().upper().startswith("SELECT")
        if spec.name != "companies":
            assert "ORDER BY" in spec.sql.upper(), spec.name


def test_specs_cover_the_day_one_experiment():
    names = {s.name for s in SPECS_V1}
    assert {"price_points", "earnings_surprises", "corporate_actions"} <= names


def test_psql_argv_targets_the_documented_container():
    argv = psql_argv("SELECT 1")
    assert argv[:4] == ["docker", "exec", "-i", "bharat-postgres"]
    assert "-U" in argv and "bharat" in argv
    assert "ON_ERROR_STOP=1" in argv


def test_tuples_only_flag():
    assert "-At" in psql_argv("SELECT 1", tuples_only=True)
    assert "-At" not in psql_argv("SELECT 1")


def test_max_date_sql_only_when_a_clock_exists():
    assert "max(date)" in SPECS_V1[0].max_date_sql()
    assert TableSpec(name="t", sql="SELECT 1").max_date_sql() is None


# ── manifest assembled from real exported files ──────────────────────────

def test_manifest_verifies_against_exported_files(tmp_path):
    result = write_csv_gz(str(tmp_path / "price_points.csv.gz"), HEADER, ROWS)
    manifest = Manifest(
        dataset_version="DS-20260912-01",
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        sealed_from=date(2025, 9, 12),
        tables={"price_points": TableSnapshot(
            "price_points", result.rows, result.sha256, "price_points.csv.gz", "2026-09-11",
            result.bytes)},
    )
    assert manifest.verify(str(tmp_path)) == []

    # Re-export different data into the same filename: verification must fail.
    write_csv_gz(str(tmp_path / "price_points.csv.gz"), HEADER,
                 [("ABC", "2026-09-11", 100.5, 999.0)])
    assert len(manifest.verify(str(tmp_path))) == 1
