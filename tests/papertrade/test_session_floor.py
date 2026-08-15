"""_session_live — the pre-open fill-bypass fix (2026-08-15).

is_market_hours() opens at 09:00 IST but continuous trading starts 09:15;
09:00-09:15 is the pre-open auction where LTP is stale/indicative. Every
fill/mark/hedge decision must gate on _session_live(), not is_market_hours().

Regression pinned: MINID orderwin_long 2026-08-14 arrived 09:00:32 IST and
took the immediate-fill branch at the PREVIOUS day's close (₹5.13 + slip)
while the real auction open was ₹5.19 — and it bypassed the pre-open book
confirmation. handle_report/pead_scan/hedge_loop now use _session_live(),
so a pre-floor arrival queues as PENDING instead.
"""
from datetime import datetime

from bharat_terminal.analysis.market_hours import IST, is_market_hours
from bharat_terminal.papertrade.main import _session_live


def ist(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


def test_preopen_window_is_market_hours_but_not_session_live():
    # The distinction the 2026-08-15 fix relies on: 09:00-09:15 weekday.
    t = ist(2026, 8, 14, 9, 0, 32)  # the MINID arrival moment (Friday)
    assert is_market_hours(t)
    assert not _session_live(t)


def test_session_floor_boundary():
    assert not _session_live(ist(2026, 8, 14, 9, 14, 59))
    assert _session_live(ist(2026, 8, 14, 9, 15, 0))


def test_close_boundary_matches_market_hours():
    assert _session_live(ist(2026, 8, 14, 15, 30, 0))
    assert not _session_live(ist(2026, 8, 14, 15, 30, 1))


def test_weekend_not_live():
    assert not _session_live(ist(2026, 8, 15, 10, 0))  # Saturday
