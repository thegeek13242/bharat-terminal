"""The trading calendar comes from the data, not from a hardcoded list.

`analysis/market_hours.py` only carries holidays for 2026 (verified), so it
cannot date a 2020 backtest. Every date on which the exchange actually printed
prices is in `price_points`, so the set of those dates IS the historical
calendar — no holiday table to fall out of date, and no silent drift when an
unscheduled closure happens.

market_hours stays the authority for LIVE session decisions (09:15 floor,
square-off), where "has it printed yet" is not yet knowable.
"""
from __future__ import annotations

import bisect
from datetime import date
from typing import Iterable, List, Optional, Sequence


class TradingCalendar:
    """Sorted, de-duplicated trading dates with session arithmetic."""

    __slots__ = ("_dates", "_index")

    def __init__(self, dates: Iterable[date]):
        self._dates: List[date] = sorted(set(dates))
        self._index = {d: i for i, d in enumerate(self._dates)}

    def __len__(self) -> int:
        return len(self._dates)

    def __contains__(self, d: date) -> bool:
        return d in self._index

    @property
    def dates(self) -> Sequence[date]:
        return tuple(self._dates)

    @property
    def first(self) -> Optional[date]:
        return self._dates[0] if self._dates else None

    @property
    def last(self) -> Optional[date]:
        return self._dates[-1] if self._dates else None

    def index_of(self, d: date) -> Optional[int]:
        return self._index.get(d)

    def next_session_index(self, d: date, strictly_after: bool = True) -> Optional[int]:
        """Index of the first session on/after (or strictly after) `d`."""
        if not self._dates:
            return None
        i = bisect.bisect_right(self._dates, d) if strictly_after else bisect.bisect_left(self._dates, d)
        return i if i < len(self._dates) else None

    def next_session(self, d: date, strictly_after: bool = True) -> Optional[date]:
        i = self.next_session_index(d, strictly_after=strictly_after)
        return self._dates[i] if i is not None else None

    def shift(self, d: date, sessions: int) -> Optional[date]:
        """Move `sessions` trading days from a date that must itself be a session."""
        i = self._index.get(d)
        if i is None:
            return None
        j = i + sessions
        return self._dates[j] if 0 <= j < len(self._dates) else None

    def sessions_between(self, start: date, end: date) -> int:
        """Trading sessions strictly after `start` up to and including `end`."""
        if not self._dates:
            return 0
        lo = bisect.bisect_right(self._dates, start)
        hi = bisect.bisect_right(self._dates, end)
        return max(0, hi - lo)

    def date_at(self, index: int) -> Optional[date]:
        if 0 <= index < len(self._dates):
            return self._dates[index]
        return None
