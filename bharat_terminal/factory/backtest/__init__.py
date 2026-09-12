"""Event-study backtester.

This is the reference implementation: plain Python, no I/O, no vectorization
tricks — written to be obviously correct and fully unit-testable. The
production path will read parquet snapshots through DuckDB for speed, and is
validated against THIS implementation (E7). When the fast path and the
reference disagree, the reference wins until proven otherwise.
"""
from __future__ import annotations

from bharat_terminal.factory.backtest.calendar import TradingCalendar
from bharat_terminal.factory.backtest.engine import (
    EventInput,
    EventResult,
    StudyConfig,
    StudyOutput,
    run_event_study,
)
from bharat_terminal.factory.backtest.returns import (
    PriceSeries,
    TradeWindow,
    resolve_trade_window,
)

__all__ = [
    "EventInput",
    "EventResult",
    "PriceSeries",
    "StudyConfig",
    "StudyOutput",
    "TradeWindow",
    "TradingCalendar",
    "resolve_trade_window",
    "run_event_study",
]
