"""Which signals this backtester can actually rank on.

Found live 2026-09-17: the cycle runner read spec.signal.direction but never
spec.signal.feature, then filtered on the snapshot's `decile` column, which is
built from composite SUE. A hypothesis signalled on expectation_surprise_gbm
therefore ran a SUE study and reported real-looking numbers for it. H-000026
and H-000027 came back byte-identical -- 1,751 events, t=1.00, mean +0.466% --
despite carrying different signals.

Silent substitution that reports success is the failure mode this repository
keeps an audit trail about. The mismatch is fatal now. Refusing costs a
hypothesis that stays un-evaluated, which is true; substituting costs a
fabricated result that looks like evidence.

TO ADD A SIGNAL: export it as a ranked column in the snapshotter, teach the
backtest to rank on it, THEN add it here. Adding it here first re-opens exactly
the hole this module exists to close.
"""
from __future__ import annotations

SNAPSHOT_RANKED_FEATURES = frozenset({"sue_composite@2.1"})


class UnbacktestableSpec(RuntimeError):
    """The spec is valid but this engine cannot execute it faithfully."""


def check_signal_supported(feature: str) -> None:
    """Raise unless the snapshot carries a ranked column for `feature`."""
    if feature not in SNAPSHOT_RANKED_FEATURES:
        raise UnbacktestableSpec(
            f"signal {feature!r} has no ranked column in this snapshot "
            f"(it carries only {sorted(SNAPSHOT_RANKED_FEATURES)}). Export the "
            f"feature in the snapshotter and rank on it before researching this.")
