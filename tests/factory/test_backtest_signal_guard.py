"""The backtester must refuse a signal it cannot run, never substitute one.

Found live 2026-09-17. The cycle runner read spec.signal.direction but never
spec.signal.feature, then filtered on the snapshot's `decile` column, which is
composite SUE. H-000026 (expectation_surprise_gbm) and H-000027 (sue_composite)
came back byte-identical -- 1,751 events, t=1.00, mean +0.466% -- because both
ran the same SUE study. The spec said one thing, the engine did another, and it
reported success.
"""
from __future__ import annotations

import pytest

from bharat_terminal.factory.backtest.signals import (
    SNAPSHOT_RANKED_FEATURES, UnbacktestableSpec, check_signal_supported)


def test_an_unrankable_signal_is_refused():
    with pytest.raises(UnbacktestableSpec) as e:
        check_signal_supported("expectation_surprise_gbm@1.0")
    assert "expectation_surprise_gbm@1.0" in str(e.value)
    assert "snapshot" in str(e.value).lower()


def test_the_message_says_what_to_do_about_it():
    """A refusal a caller cannot act on just becomes a mystery in a log."""
    with pytest.raises(UnbacktestableSpec) as e:
        check_signal_supported("shp_promoter_delta@1.0")
    assert "snapshotter" in str(e.value)


def test_the_supported_signal_passes():
    check_signal_supported("sue_composite@2.1")


def test_the_supported_set_is_explicit_and_minimal():
    """Adding a feature here before the snapshotter exports it reopens the hole."""
    assert SNAPSHOT_RANKED_FEATURES == frozenset({"sue_composite@2.1"})
