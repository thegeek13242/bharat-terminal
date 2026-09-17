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
        check_signal_supported("impact_magnitude@1.0")
    assert "impact_magnitude@1.0" in str(e.value)
    assert "snapshot" in str(e.value).lower()


def test_the_message_says_what_to_do_about_it():
    """A refusal a caller cannot act on just becomes a mystery in a log."""
    with pytest.raises(UnbacktestableSpec) as e:
        check_signal_supported("guidance_change@1.0")
    assert "snapshotter" in str(e.value)


@pytest.mark.parametrize("feature", sorted(SNAPSHOT_RANKED_FEATURES))
def test_every_declared_signal_passes_and_has_a_loader_mapping(feature):
    """The set and the loader must agree, or a 'supported' signal KeyErrors."""
    from bharat_terminal.factory.backtest.events import RAW_COLUMN
    check_signal_supported(feature)
    assert feature in RAW_COLUMN


def test_guidance_change_is_not_a_ranked_signal():
    """It is an enum split, not something a decile can be taken of."""
    assert "guidance_change@1.0" not in SNAPSHOT_RANKED_FEATURES
