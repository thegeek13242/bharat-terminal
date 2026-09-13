"""Immutable dataset snapshots.

A snapshot is the factory's unit of reproducibility: a manifest of tables with
row counts, max dates and sha256 hashes, pinned to a `dataset_version` that
every experiment run records. Re-running an experiment against the same
dataset_version must produce the same result_hash, or E6 fails loudly.

Snapshots also decouple research compute from the production database: the
backtester reads files, so a sweep cannot compete with the live pipeline for
Postgres connections — a real failure mode on this host, which has hit 72 of
100 before.
"""
from __future__ import annotations

from bharat_terminal.factory.snapshot.integrity import (
    IntegrityResult,
    IntegritySuite,
    LagStats,
    check_clock_lag,
    check_constant_offset,
    check_null_share,
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

__all__ = [
    "IntegrityResult",
    "IntegritySuite",
    "LagStats",
    "Manifest",
    "TableSnapshot",
    "check_clock_lag",
    "check_constant_offset",
    "check_null_share",
    "check_row_delta",
    "check_series_continuity",
    "check_snapshot_age",
    "check_source_freshness",
    "check_universe_survivorship",
    "clock_lag_stats",
    "dataset_version_for",
    "run_integrity_suite",
    "sha256_bytes",
    "sha256_file",
]
