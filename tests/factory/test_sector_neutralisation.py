"""Equal-weighted sector indices, and what they are allowed to hide.

Built 2026-09-18. The first control the engine actually applies: H-000007 read
+4.187% (t=3.72) against the broad benchmark and -0.346% (t=-0.33) against its
own sector, so the machinery has to be right or it silently rewrites results.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from bharat_terminal.factory.backtest.engine import EventInput, StudyConfig, run_event_study
from bharat_terminal.factory.backtest.calendar import TradingCalendar
from bharat_terminal.factory.backtest.returns import Bar, PriceSeries
from bharat_terminal.factory.backtest.sector import (
    INDEX_BASE, MIN_SECTOR_MEMBERS, build_sector_indices, members_from_rows)

D0 = date(2024, 1, 1)
DATES = [D0 + timedelta(days=i) for i in range(40)]


def _series(sym, fn):
    return PriceSeries(sym, [Bar(d=d, open=fn(i), close=fn(i)) for i, d in enumerate(DATES)])


def test_a_flat_sector_produces_a_flat_index():
    prices = {f"S{i}": _series(f"S{i}", lambda _i: 100.0) for i in range(6)}
    idx = build_sector_indices({"Flat": list(prices)}, prices, DATES)
    bars = [idx["Flat"].bar_on(d) for d in DATES]
    assert all(abs(b.close - INDEX_BASE) < 1e-9 for b in bars)


def test_the_index_tracks_a_uniformly_rising_sector():
    prices = {f"S{i}": _series(f"S{i}", lambda i_: 100.0 * (1.01 ** i_)) for i in range(6)}
    idx = build_sector_indices({"Up": list(prices)}, prices, DATES)
    first, last = idx["Up"].bar_on(DATES[0]), idx["Up"].bar_on(DATES[-1])
    assert last.close > first.close
    assert abs(last.close / first.close - 1.01 ** (len(DATES) - 1)) < 1e-6


def test_a_sector_below_the_member_floor_gets_no_index():
    """A handful of names is not a benchmark; better absent than misleading."""
    prices = {f"S{i}": _series(f"S{i}", lambda _i: 100.0)
              for i in range(MIN_SECTOR_MEMBERS - 1)}
    assert build_sector_indices({"Tiny": list(prices)}, prices, DATES) == {}


def test_equal_weighting_is_not_dominated_by_one_large_mover():
    """Cap-weighting would let one name carry the index; equal-weighting must not."""
    prices = {f"S{i}": _series(f"S{i}", lambda _i: 100.0) for i in range(9)}
    prices["BIG"] = _series("BIG", lambda i_: 100.0 * (2.0 ** i_))
    idx = build_sector_indices({"Mixed": list(prices)}, prices, DATES)
    b = idx["Mixed"]
    # One of ten members doubling daily moves the index ~10% a day, not ~100%.
    step = b.bar_on(DATES[1]).close / b.bar_on(DATES[0]).close - 1.0
    assert 0.05 < step < 0.15


def test_members_are_grouped_from_snapshot_rows():
    rows = [{"symbol": "A", "sector_bse": "Energy"},
            {"symbol": "B", "sector_bse": "Energy"},
            {"symbol": "C", "sector_bse": ""},
            {"symbol": "", "sector_bse": "Energy"}]
    assert members_from_rows(rows) == {"Energy": ["A", "B"]}


def test_an_event_without_a_sector_index_is_dropped_not_silently_broad():
    """Neutralising some events and not others measures two things, reports one."""
    prices = {f"S{i}": _series(f"S{i}", lambda _i: 100.0) for i in range(6)}
    prices["ORPHAN"] = _series("ORPHAN", lambda _i: 100.0)
    prices["BENCH"] = _series("BENCH", lambda _i: 100.0)
    idx = build_sector_indices({"Known": [f"S{i}" for i in range(6)]}, prices, DATES)

    events = [EventInput(symbol="S0", event_date=DATES[5], median_traded_value_cr=99,
                         meta={"sector": "Known"}),
              EventInput(symbol="ORPHAN", event_date=DATES[5], median_traded_value_cr=99,
                         meta={"sector": "Missing"})]
    out = run_event_study(events, prices, prices["BENCH"], TradingCalendar(DATES),
                          StudyConfig(holding_days=3, min_median_traded_value_cr=0),
                          sector_benchmarks=idx)
    assert out.exclusions.get("no_sector_benchmark") == 1


def test_without_sector_benchmarks_the_broad_one_is_used():
    """The default path must be untouched by any of this."""
    prices = {"A": _series("A", lambda _i: 100.0), "BENCH": _series("BENCH", lambda _i: 100.0)}
    events = [EventInput(symbol="A", event_date=DATES[5], median_traded_value_cr=99,
                         meta={"sector": "Missing"})]
    out = run_event_study(events, prices, prices["BENCH"], TradingCalendar(DATES),
                          StudyConfig(holding_days=3, min_median_traded_value_cr=0))
    assert "no_sector_benchmark" not in out.exclusions
