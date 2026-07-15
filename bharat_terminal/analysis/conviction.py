"""Deterministic conviction v2 — pure function, frozen table (v1_2026_07).

Replaces LLM-vibes conviction with a code-computed label from measurable
features. The Jul 2-3 study showed LLM conviction/implied-move were
ANTI-calibrated (every 8%-implied signal realized <1.5%; the 2%-implied ones
delivered 3-6%). The LLM's label is preserved separately (llm_conviction)
so the two stay A/B-comparable; empirical recalibration (shrinkage, à la
research/calibrate_weights.py) is reserved for the post-freeze v2 review.
"""
from __future__ import annotations

from typing import Optional

from bharat_terminal.analysis.filing_policy import ALWAYS_ELIGIBLE, RATIO_REQUIRED

# Microcap cap: below this mcap the tails are where the anti-calibration
# lived — cap conviction at medium regardless of ratio.
MICROCAP_MCAP_CR = 500.0

_BUCKETS = ["low", "medium", "high"]


def compute_conviction(
    policy_action: str,
    ratio: Optional[float],
    priced_in_downgraded: bool = False,
    mcap_cr: Optional[float] = None,
) -> str:
    """Frozen v1 table:

    ratio ≥ 0.20                                  → base high
    0.10 ≤ ratio < 0.20                           → base medium
    ALWAYS_ELIGIBLE without an extractable number → base medium
    PASS_THROUGH                                  → base low
    priced-in downgrade fired on this symbol      → one bucket down
    mcap < 500 Cr or unknown                      → capped at medium
    """
    if ratio is not None and ratio >= 0.20:
        base = "high"
    elif ratio is not None and ratio >= 0.10:
        base = "medium"
    elif policy_action == ALWAYS_ELIGIBLE:
        base = "medium"
    elif policy_action == RATIO_REQUIRED:
        # Ratio-required with a sub-threshold ratio shouldn't normally reach
        # here (the gate fails it first); defensive floor.
        base = "low"
    else:  # PASS_THROUGH and anything unrecognized
        base = "low"

    idx = _BUCKETS.index(base)
    if priced_in_downgraded:
        idx = max(0, idx - 1)
    if mcap_cr is None or mcap_cr < MICROCAP_MCAP_CR:
        idx = min(idx, _BUCKETS.index("medium"))
    return _BUCKETS[idx]
