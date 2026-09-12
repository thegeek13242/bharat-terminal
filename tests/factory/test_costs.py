"""Worked examples for the India cost model and the slippage buckets.

The numbers here are arithmetic on the rate card, not claims about the real
world: the rates themselves are unverified (NSE's levies page timed out), and
`RateCard.verified` says so.
"""
from __future__ import annotations

from bharat_terminal.factory.costs.india_cash import (
    CASH_DELIVERY_V1,
    CASH_INTRADAY_V1,
    FUTURES_V1,
    round_trip_bps,
    side_cost_bps,
)
from bharat_terminal.factory.costs.slippage import (
    participation_ok,
    slippage_bps,
)

LAKH = 100_000.0


def test_rate_cards_are_marked_unverified():
    """Guards the promotion rule: unverified rates cannot reach production."""
    for card in (CASH_DELIVERY_V1, CASH_INTRADAY_V1, FUTURES_V1):
        assert card.verified is False
        assert card.source_note


def test_delivery_buy_side_worked_example():
    b = side_cost_bps(CASH_DELIVERY_V1, "buy", LAKH)
    assert b.stt_bps == 10.0          # 0.1%
    assert b.stamp_bps == 1.5         # 0.015%
    assert abs(b.exchange_bps - 0.297) < 1e-12
    assert abs(b.sebi_bps - 0.01) < 1e-12
    assert b.brokerage_bps == 2.0     # Rs 20 cap on a Rs 1L order
    assert abs(b.gst_bps - (2.0 + 0.297 + 0.01) * 0.18) < 1e-12
    assert abs(b.total_bps - 14.22226) < 1e-5


def test_delivery_sell_side_has_no_stamp_duty():
    s = side_cost_bps(CASH_DELIVERY_V1, "sell", LAKH)
    assert s.stamp_bps == 0.0
    assert s.stt_bps == 10.0


def test_delivery_round_trip_on_one_lakh():
    # ~0.27% round trip: the reason a 40 bps "edge" is not an edge.
    assert abs(round_trip_bps(CASH_DELIVERY_V1, LAKH) - 26.94452) < 1e-5


def test_brokerage_cap_makes_small_orders_relatively_dearer():
    small = side_cost_bps(CASH_DELIVERY_V1, "buy", 10_000.0).brokerage_bps
    large = side_cost_bps(CASH_DELIVERY_V1, "buy", 1_000_000.0).brokerage_bps
    assert small == 3.0    # 0.03%, below the Rs 20 cap
    assert large == 0.2    # Rs 20 spread over Rs 10L
    assert small > large


def test_intraday_is_cheaper_than_delivery():
    assert round_trip_bps(CASH_INTRADAY_V1, LAKH) < round_trip_bps(CASH_DELIVERY_V1, LAKH)
    assert side_cost_bps(CASH_INTRADAY_V1, "buy", LAKH).stt_bps == 0.0
    assert side_cost_bps(CASH_INTRADAY_V1, "sell", LAKH).stt_bps == 2.5


def test_futures_stt_applies_on_the_sell_side_only():
    assert side_cost_bps(FUTURES_V1, "buy", LAKH).stt_bps == 0.0
    assert side_cost_bps(FUTURES_V1, "sell", LAKH).stt_bps == 5.0   # 0.05%


def test_zero_turnover_is_free_not_a_division_error():
    assert side_cost_bps(CASH_DELIVERY_V1, "buy", 0.0).total_bps == 0.0


def test_slippage_buckets():
    assert slippage_bps(60.0) == 5.0
    assert slippage_bps(20.0) == 15.0
    assert slippage_bps(5.0) == 40.0
    assert slippage_bps(1.5) == 80.0


def test_illiquid_names_are_untradeable_not_merely_expensive():
    """Returning None forces exclusion; a big number would still be profitable
    to a backtest that ignores reality."""
    assert slippage_bps(0.5) is None
    assert slippage_bps(0.0) is None


def test_participation_adds_impact():
    base = slippage_bps(10.0)
    with_size = slippage_bps(10.0, order_value_inr=2_000_000.0)
    assert with_size > base


def test_participation_cap():
    # Rs 1L against Rs 5 Cr/day is 0.2% of volume: fine.
    assert participation_ok(LAKH, 5.0) is True
    # Rs 1 Cr against Rs 1 Cr/day is the whole day: not fine.
    assert participation_ok(10_000_000.0, 1.0) is False
    assert participation_ok(LAKH, 0.0) is False
