"""Indian equity transaction costs.

RATE PROVENANCE — READ BEFORE TRUSTING A NET NUMBER
---------------------------------------------------
These rates were collected on 2026-09-12 from SECONDARY sources (broker and
tax explainers). NSE's own levies page timed out twice when fetched. They are
therefore ASSUMPTIONS, and `RateCard.verified` is False for every card below.

The gate engine refuses to promote a hypothesis whose rate card is unverified
past the SIMULATED stage, so an unverified card can never quietly become a
production candidate. Verify against NSE/BSE/SEBI circulars, flip `verified`,
and re-run the affected experiments — the ledger keeps the rate card version
on every run, so which results need re-running is a query, not a guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class RateCard:
    name: str
    stt_buy_pct: float
    stt_sell_pct: float
    stamp_buy_pct: float
    exchange_txn_pct: float
    sebi_fee_pct: float
    brokerage_pct: float
    brokerage_cap_inr: float
    gst_pct: float
    verified: bool
    source_note: str


# STT: delivery 0.1% both sides; intraday 0.025% sell only.
# Stamp duty (buy only): 0.015% delivery, 0.003% intraday.
# NSE transaction charge 0.00297%; SEBI fee Rs 10 per crore = 0.0001%.
# GST 18% on brokerage + exchange + SEBI charges.
CASH_DELIVERY_V1 = RateCard(
    name="india_cash_delivery_v1",
    stt_buy_pct=0.100, stt_sell_pct=0.100,
    stamp_buy_pct=0.015,
    exchange_txn_pct=0.00297, sebi_fee_pct=0.0001,
    brokerage_pct=0.03, brokerage_cap_inr=20.0,
    gst_pct=18.0,
    verified=False,
    source_note="secondary sources 2026-09-12; NSE levies page unreachable",
)

CASH_INTRADAY_V1 = RateCard(
    name="india_cash_intraday_v1",
    stt_buy_pct=0.0, stt_sell_pct=0.025,
    stamp_buy_pct=0.003,
    exchange_txn_pct=0.00297, sebi_fee_pct=0.0001,
    brokerage_pct=0.03, brokerage_cap_inr=20.0,
    gst_pct=18.0,
    verified=False,
    source_note="secondary sources 2026-09-12",
)

# Futures STT rose to 0.05% on the sell side effective 2026-04-01.
FUTURES_V1 = RateCard(
    name="india_futures_v1",
    stt_buy_pct=0.0, stt_sell_pct=0.05,
    stamp_buy_pct=0.002,
    exchange_txn_pct=0.0019, sebi_fee_pct=0.0001,
    brokerage_pct=0.03, brokerage_cap_inr=20.0,
    gst_pct=18.0,
    verified=False,
    source_note="STT hike effective 2026-04-01 (secondary); exchange rate UNVERIFIED",
)


@dataclass(frozen=True)
class CostBreakdown:
    stt_bps: float
    stamp_bps: float
    exchange_bps: float
    sebi_bps: float
    brokerage_bps: float
    gst_bps: float

    @property
    def total_bps(self) -> float:
        return (self.stt_bps + self.stamp_bps + self.exchange_bps
                + self.sebi_bps + self.brokerage_bps + self.gst_bps)


def _pct_to_bps(pct: float) -> float:
    return pct * 100.0


def side_cost_bps(card: RateCard, side: Side, turnover_inr: float) -> CostBreakdown:
    """Statutory + broker cost for one side, in basis points of turnover.

    Brokerage is min(cap, pct x turnover), so it is regressive: a small order
    pays a larger share. Passing the real order value matters.
    """
    if turnover_inr <= 0:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    stt = _pct_to_bps(card.stt_buy_pct if side == "buy" else card.stt_sell_pct)
    stamp = _pct_to_bps(card.stamp_buy_pct) if side == "buy" else 0.0
    exch = _pct_to_bps(card.exchange_txn_pct)
    sebi = _pct_to_bps(card.sebi_fee_pct)

    brokerage_inr = min(card.brokerage_cap_inr, turnover_inr * card.brokerage_pct / 100.0)
    brokerage = 10_000.0 * brokerage_inr / turnover_inr

    gst = (brokerage + exch + sebi) * card.gst_pct / 100.0
    return CostBreakdown(stt, stamp, exch, sebi, brokerage, gst)


def round_trip_bps(card: RateCard, turnover_inr: float) -> float:
    """Total statutory + broker cost of entering and exiting, in bps."""
    buy = side_cost_bps(card, "buy", turnover_inr)
    sell = side_cost_bps(card, "sell", turnover_inr)
    return buy.total_bps + sell.total_bps
