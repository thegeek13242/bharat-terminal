"""Immutable dataset snapshots.

A snapshot is the factory's unit of reproducibility: a manifest of tables with
row counts, max dates and sha256 hashes, pinned to a `dataset_version` that
every experiment run records. Re-running an experiment against the same
dataset_version must produce the same result_hash, or E6 fails loudly.

Snapshots also decouple research compute from the production database: the
backtester reads files, so a sweep cannot compete with the live pipeline for
Postgres connections — a real failure mode on this host, which has hit 72 of
100 connections before.
"""
from __future__ import annotations

from bharat_terminal.factory.snapshot.integrity import (
    IntegrityResult,
    IntegritySuite,
    check_null_share,
    check_row_delta,
    check_snapshot_age,
    check_source_freshness,
    check_timestamp_ist_as_utc,
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
    "Manifest",
    "TableSnapshot",
    "check_null_share",
    "check_row_delta",
    "check_snapshot_age",
    "check_source_freshness",
    "check_timestamp_ist_as_utc",
    "dataset_version_for",
    "sha256_bytes",
    "sha256_file",
]
