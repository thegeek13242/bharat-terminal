"""Entry/exit resolution and return construction.

The rule that matters: **entry may never precede the information.** The
existing `grounding/queries.get_forward_returns` enters at the first close
on/after the event date, which for an evening filing is that day's close — a
price that printed before the filing existed. That is a look-ahead, and it is
why this module resolves entry strictly after the event session.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, List, Optional, Sequence, Tuple

from bharat_terminal.factory.backtest.calendar import TradingCalendar


@dataclass(frozen=True)
class Bar:
    d: date
    open: float
    close: float


class PriceSeries:
    """Unadjusted OHLC for one symbol, indexed by date."""

    __slots__ = ("symbol", "_bars", "_index")

    def __init__(self, symbol: str, bars: Sequence[Bar]):
        self.symbol = symbol
        self._bars: List[Bar] = sorted(bars, key=lambda b: b.d)
        self._index = {b.d: i for i, b in enumerate(self._bars)}

    def __len__(self) -> int:
        return len(self._bars)

    @property
    def dates(self) -> List[date]:
        return [b.d for b in self._bars]

    def bar_on(self, d: date) -> Optional[Bar]:
        i = self._index.get(d)
        return self._bars[i] if i is not None else None

    def has(self, d: date) -> bool:
        return d in self._index


@dataclass(frozen=True)
class TradeWindow:
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    holding_days: int
    entry_basis: str          # "open" | "close"


def resolve_trade_window(
    *,
    event_date: date,
    series: PriceSeries,
    calendar: TradingCalendar,
    entry_rule: str,
    holding_days: int,
    event_time_ist: Optional[time] = None,
    same_session_cutoff: Optional[time] = None,
) -> Tuple[Optional[TradeWindow], Optional[str]]:
    """Resolve the tradeable window, or explain why there is none.

    Returns (window, reject_reason). Exactly one is non-None.
    """
    if entry_rule == "same_session_close_if_before":
        if event_time_ist is None or same_session_cutoff is None:
            return None, "same_session_rule_requires_timestamp"
        # Same session only when the filing lands before the cutoff on a day
        # the market actually traded; otherwise it behaves like next session.
        if event_time_ist < same_session_cutoff and event_date in calendar:
            entry_idx = calendar.index_of(event_date)
            basis = "close"
        else:
            entry_idx = calendar.next_session_index(event_date, strictly_after=True)
            basis = "close"
    elif entry_rule in ("next_session_open", "next_session_close"):
        entry_idx = calendar.next_session_index(event_date, strictly_after=True)
        basis = "open" if entry_rule == "next_session_open" else "close"
    else:
        return None, f"unsupported_entry_rule:{entry_rule}"

    if entry_idx is None:
        return None, "no_session_after_event"

    entry_date = calendar.date_at(entry_idx)
    exit_date = calendar.date_at(entry_idx + holding_days)
    if exit_date is None:
        return None, "horizon_not_elapsed"

    entry_bar = series.bar_on(entry_date)
    exit_bar = series.bar_on(exit_date)
    if entry_bar is None:
        return None, "no_price_on_entry_session"
    if exit_bar is None:
        return None, "no_price_on_exit_session"

    entry_price = entry_bar.open if basis == "open" else entry_bar.close
    if entry_price is None or entry_price <= 0 or exit_bar.close <= 0:
        return None, "bad_price"

    return TradeWindow(
        entry_date=entry_date,
        entry_price=float(entry_price),
        exit_date=exit_date,
        exit_price=float(exit_bar.close),
        holding_days=holding_days,
        entry_basis=basis,
    ), None


def gross_return(window: TradeWindow, direction: str) -> float:
    """Signed gross return of the position over the window."""
    raw = window.exit_price / window.entry_price - 1.0
    return raw if direction == "long" else -raw


def benchmark_return(
    benchmark: PriceSeries, entry_date: date, exit_date: date, entry_basis: str
) -> Optional[float]:
    entry_bar = benchmark.bar_on(entry_date)
    exit_bar = benchmark.bar_on(exit_date)
    if entry_bar is None or exit_bar is None:
        return None
    entry_price = entry_bar.open if entry_basis == "open" else entry_bar.close
    if not entry_price or entry_price <= 0:
        return None
    return exit_bar.close / entry_price - 1.0


def has_corporate_action(
    ex_dates: Sequence[date], start: date, end: date, pad_sessions: int = 1,
    calendar: Optional[TradingCalendar] = None,
) -> bool:
    """Is a split/bonus/rights ex-date inside the (padded) trade window?

    Closes in `price_points` are UNADJUSTED, so a split inside the window turns
    a 1:2 split into a fabricated -50% return. v1 excludes such events rather
    than adjusting them.
    """
    lo, hi = start, end
    if calendar is not None and pad_sessions:
        lo = calendar.shift(start, -pad_sessions) or start
        hi = calendar.shift(end, pad_sessions) or end
    return any(lo <= d <= hi for d in ex_dates)


def suspicious_unexplained_gap(
    series: PriceSeries, start: date, end: date, threshold: float = 0.40
) -> bool:
    """Flag a >40% overnight move with no recorded corporate action.

    `corporate_actions` has 20,339 rows but its completeness is unmeasured, so
    a missing row is a real possibility; this is the detector for it.
    """
    bars = [b for b in series._bars if start <= b.d <= end]  # noqa: SLF001
    for prev, cur in zip(bars, bars[1:]):
        if prev.close > 0 and abs(cur.close / prev.close - 1.0) >= threshold:
            return True
    return False
