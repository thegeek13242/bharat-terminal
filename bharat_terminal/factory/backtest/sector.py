"""Equal-weighted sector indices, for neutralising against sector rotation.

WHY THIS EXISTS
`controls` were validated by the compiler and then silently ignored by the
engine: StudyConfig had no notion of them, so every spec declaring a control
ran UNCONTROLLED and carried a controls_not_applied bias flag. This is the
first control that is actually applied.

WHAT NEUTRALISING MEANS HERE
An event's abnormal return is normally measured against one broad benchmark.
Against a SECTOR benchmark instead, a top-decile result that only reflects
whichever sectors happened to report well in a given quarter has its sector
component removed, and what survives is the part specific to the event.

WHY EQUAL-WEIGHTED
The study itself equal-weights its events, so a cap-weighted sector index would
compare an equal-weighted portfolio against a benchmark dominated by its
largest members. Equal-weighting both sides keeps the comparison honest.

TWO CAVEATS, NEITHER HIDDEN
Members are whatever priced symbols carry the label TODAY, so the index has the
same survivorship property as the rest of this snapshot -- it cannot know that
a company left the sector, or the market, in 2021. And the peer set includes
companies that are themselves reporting that week; with hundreds of members per
sector the contamination is small, but it is not zero.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from bharat_terminal.factory.backtest.returns import Bar, PriceSeries

# A handful of names is not a sector benchmark. Below this the index is not
# built at all, and events in that sector fall back or drop rather than being
# measured against noise.
MIN_SECTOR_MEMBERS = 5

INDEX_BASE = 100.0


def build_sector_indices(
    members_by_sector: Mapping[str, Sequence[str]],
    prices: Mapping[str, PriceSeries],
    market_dates: Sequence[date],
    min_members: int = MIN_SECTOR_MEMBERS,
) -> Dict[str, PriceSeries]:
    """One equal-weighted index per sector, as a synthetic PriceSeries.

    The level chains daily equal-weighted returns across members priced on both
    the current and previous session. `open` is carried as the equal-weighted
    overnight gap, so a study entering at the open is compared against an index
    measured the same way rather than against the prior close.
    """
    out: Dict[str, PriceSeries] = {}
    dates = sorted(market_dates)

    for sector, symbols in members_by_sector.items():
        series = [prices[s] for s in symbols if s in prices and len(prices[s])]
        if len(series) < min_members:
            continue

        bars: List[Bar] = []
        level = INDEX_BASE
        prev_d: Optional[date] = None
        for d in dates:
            if prev_d is None:
                bars.append(Bar(d=d, open=level, close=level))
                prev_d = d
                continue

            close_rets: List[float] = []
            open_rets: List[float] = []
            for ps in series:
                cur, prev = ps.bar_on(d), ps.bar_on(prev_d)
                if cur is None or prev is None or not prev.close or prev.close <= 0:
                    continue
                close_rets.append(cur.close / prev.close - 1.0)
                if cur.open and cur.open > 0:
                    open_rets.append(cur.open / prev.close - 1.0)

            if not close_rets:
                # No member traded: carry the level rather than inventing a move.
                bars.append(Bar(d=d, open=level, close=level))
                prev_d = d
                continue

            open_level = level * (1.0 + (sum(open_rets) / len(open_rets)
                                         if open_rets else 0.0))
            level = level * (1.0 + sum(close_rets) / len(close_rets))
            bars.append(Bar(d=d, open=open_level, close=level))
            prev_d = d

        out[sector] = PriceSeries(f"__SECTOR__{sector}", bars)

    return out


def members_from_rows(rows: Iterable[Mapping[str, object]],
                      sector_key: str = "sector_bse") -> Dict[str, List[str]]:
    """Group snapshot company rows into {sector: [symbol, ...]}."""
    out: Dict[str, List[str]] = {}
    for r in rows:
        sec = (r.get(sector_key) or "")
        sym = (r.get("symbol") or "")
        if sec and sym:
            out.setdefault(str(sec), []).append(str(sym))
    return out
