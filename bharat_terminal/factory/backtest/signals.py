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

# Extended 2026-09-17 when the snapshotter began exporting these PIT-joined
# and backtest.events began ranking them. Adding a name here without doing both
# of those things re-opens the silent-substitution hole this module closes.
SNAPSHOT_RANKED_FEATURES = frozenset({
    "sue_composite@2.1",              # precomputed decile, production pipeline
    "expectation_surprise_gbm@1.0",   # raw value, ranked by trailing_deciles
})

# NOT supported, and the reason is worth keeping next to the set.
#
# shp_promoter_delta@1.0 is exported and ranked correctly, and yields exactly
# ZERO in-sample events. Measured 2026-09-17: 9,931 events carry a PIT-safe
# delta, 19 of them predate the seal, and 0 of those 19 pass the liquidity
# filter. shareholding_patterns holds 15,838 rows across 3,639 symbols -- about
# four quarters each -- so consecutive quarters before the holdout barely
# exist. The registry's 0.969 is a fill rate over that table and says nothing
# about usable history.
#
# The column stays in the snapshot: it is correct, and the feature becomes
# researchable on its own as history accumulates. It is the SUPPORTED claim
# that would be false, not the data.


class UnbacktestableSpec(RuntimeError):
    """The spec is valid but this engine cannot execute it faithfully."""


def check_signal_supported(feature: str) -> None:
    """Raise unless the snapshot carries a ranked column for `feature`."""
    if feature not in SNAPSHOT_RANKED_FEATURES:
        raise UnbacktestableSpec(
            f"signal {feature!r} has no ranked column in this snapshot "
            f"(it carries only {sorted(SNAPSHOT_RANKED_FEATURES)}). Export the "
            f"feature in the snapshotter and rank on it before researching this.")


# Controls the ENGINE can actually apply, as opposed to the ones the compiler
# will merely validate. Anything eligible but absent here still compiles and
# still carries the controls_not_applied bias flag, because a study that
# declares a control it does not apply is an uncontrolled study.
#
# sector_bse became real on 2026-09-18: backtest.sector builds an equal-weighted
# index per sector and run_event_study measures against it.
APPLIED_CONTROLS = frozenset({"sector_bse@1.0"})
