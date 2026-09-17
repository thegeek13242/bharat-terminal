"""Trailing-window deciles. The reference set is the PAST, never the future.

WHY THIS EXISTS AND WHY IT IS NOT SQL
The incumbent signal arrives pre-ranked: earnings_surprises carries `decile`
and `reference_n`, computed by the production pipeline against a trailing
window. Any NEW signal has to be ranked somewhere, and the obvious
`NTILE(10) OVER (ORDER BY value)` would rank each event against the entire
history, including events that had not happened yet. That is look-ahead bias
of the purest kind, and it would make every new feature look predictive.

So ranking happens here, explicitly, where it can be tested, rather than
inside a window frame that is easy to get wrong and impossible to see.

THE RULES
  * An event ranks against events STRICTLY BEFORE it. Same-day events never
    see each other, so a day's worth of results cannot rank itself.
  * Below `min_reference` prior observations there is no decile: None, not a
    guess. A decile drawn from nine neighbours is noise wearing a number.
  * Ties take the midrank, so a feature with many repeated values does not
    pile all of them into decile 10.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Sequence, Tuple

# A decile needs enough neighbours to mean anything. The production pipeline's
# reference_n runs to five figures; this floor only rejects the cold start.
DEFAULT_MIN_REFERENCE = 100
DEFAULT_LOOKBACK_DAYS = 365


@dataclass(frozen=True)
class Ranked:
    decile: Optional[int]          # 1..10, or None when under-referenced
    percentile: Optional[float]    # 0..1, or None
    reference_n: int


def trailing_deciles(
    observations: Sequence[Tuple[date, float]],
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    min_reference: int = DEFAULT_MIN_REFERENCE,
) -> List[Ranked]:
    """Rank each observation against the ones before it.

    `observations` is (event_date, value) in any order. The result comes back
    in the SAME order as the input, so callers can zip it straight onto their
    rows without re-sorting.
    """
    n_all = len(observations)
    out: List[Optional[Ranked]] = [None] * n_all
    order = sorted(range(n_all), key=lambda i: observations[i][0])

    live_vals: List[float] = []                  # sorted values inside the window
    pending: List[Tuple[date, float]] = []       # (date, value) in date order
    evict_at = 0

    i = 0
    while i < len(order):
        day = observations[order[i]][0]
        cutoff = day - timedelta(days=lookback_days)

        # Drop anything that has aged out of the lookback.
        while evict_at < len(pending) and pending[evict_at][0] < cutoff:
            stale = pending[evict_at][1]
            pos = bisect.bisect_left(live_vals, stale)
            if pos < len(live_vals) and live_vals[pos] == stale:
                live_vals.pop(pos)
            evict_at += 1

        # Rank every event on this date against the window as it stands, BEFORE
        # any of them joins it. Same-day events must not rank each other.
        j = i
        same_day: List[int] = []
        while j < len(order) and observations[order[j]][0] == day:
            same_day.append(order[j])
            j += 1

        ref_n = len(live_vals)
        for idx in same_day:
            v = observations[idx][1]
            if ref_n < min_reference:
                out[idx] = Ranked(None, None, ref_n)
            else:
                lo = bisect.bisect_left(live_vals, v)
                hi = bisect.bisect_right(live_vals, v)
                pct = (lo + (hi - lo) / 2.0) / ref_n
                out[idx] = Ranked(min(10, max(1, int(pct * 10) + 1)), round(pct, 6), ref_n)

        # Only now do they enter the reference set for later dates.
        for idx in same_day:
            v = observations[idx][1]
            bisect.insort(live_vals, v)
            pending.append((day, v))

        i = j

    return [r for r in out]  # type: ignore[misc]
