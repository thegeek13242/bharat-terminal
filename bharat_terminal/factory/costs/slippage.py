"""Liquidity-aware slippage.

A flat haircut is the standard way to make a microcap strategy look tradeable.
The live paper engine currently charges a flat 10 bps per side, which is
optimistic for the names most event strategies select. This model charges by
liquidity bucket and adds a participation term.

Buckets are seeded from judgement, not measurement [ASSUMPTION]. They are
recalibrated monthly from realized paper fills (`provenance.slippage_bps` and
`exit_slippage_bps`), which is the only honest source we will have.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


@dataclass(frozen=True)
class Bucket:
    min_median_traded_value_cr: float
    bps: float


# Ordered high liquidity -> low. `None` bps means untradeable.
ADV_BUCKETS_V1: Tuple[Bucket, ...] = (
    Bucket(50.0, 5.0),
    Bucket(10.0, 15.0),
    Bucket(2.0, 40.0),
    Bucket(1.0, 80.0),
)

UNTRADEABLE_BELOW_CR = 1.0

# Extra cost for taking a large share of a day's volume. Square-root impact is
# the conventional shape; the coefficient is a placeholder until measured.
IMPACT_COEFFICIENT_BPS = 10.0


def slippage_bps(
    median_traded_value_cr: float,
    order_value_inr: float = 0.0,
    buckets: Sequence[Bucket] = ADV_BUCKETS_V1,
    impact_coefficient_bps: float = IMPACT_COEFFICIENT_BPS,
) -> Optional[float]:
    """Per-side slippage in bps, or None when the name is untradeable.

    Returning None (rather than a large number) is deliberate: an untradeable
    name must be excluded from the sample, not charged a fee that a backtest
    can still profit through.
    """
    if median_traded_value_cr is None or median_traded_value_cr < UNTRADEABLE_BELOW_CR:
        return None

    base = buckets[-1].bps
    for b in buckets:
        if median_traded_value_cr >= b.min_median_traded_value_cr:
            base = b.bps
            break

    if order_value_inr > 0 and median_traded_value_cr > 0:
        adv_inr = median_traded_value_cr * 1e7  # Rs crore -> rupees
        participation = order_value_inr / adv_inr
        base += impact_coefficient_bps * math.sqrt(max(participation, 0.0))

    return base


def participation_ok(
    order_value_inr: float, median_traded_value_cr: float, max_pct: float = 2.0
) -> bool:
    """Cap on our share of the day's traded value (spec §7.4)."""
    if median_traded_value_cr <= 0:
        return False
    adv_inr = median_traded_value_cr * 1e7
    return (100.0 * order_value_inr / adv_inr) <= max_pct
