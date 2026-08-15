"""is_market_hours + the NSE holiday calendar — gates 6 papertrade loops
and the analysis RT path. Injectable `now`, so boundaries are exact. All
inputs tz-aware (naive-input semantics depend on the host TZ and are
deliberately not pinned here).

M3 (review 2026-07-30) FIXED 2026-08-15: NSE_HOLIDAYS covers 2026 and
is_market_hours/is_trading_day consult it.
"""
from datetime import date, datetime, timezone

from bharat_terminal.analysis.market_hours import (IST, is_market_hours,
                                                   is_trading_day,
                                                   trading_days_between)


def ist(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


def test_open_boundary_inclusive():
    assert not is_market_hours(ist(2026, 7, 29, 8, 59, 59))   # Wednesday
    assert is_market_hours(ist(2026, 7, 29, 9, 0, 0))


def test_close_boundary_inclusive():
    assert is_market_hours(ist(2026, 7, 29, 15, 30, 0))
    assert not is_market_hours(ist(2026, 7, 29, 15, 30, 1))


def test_midsession():
    assert is_market_hours(ist(2026, 7, 29, 12, 0))


def test_weekend_closed():
    assert not is_market_hours(ist(2026, 8, 1, 12, 0))        # Saturday
    assert not is_market_hours(ist(2026, 8, 2, 12, 0))        # Sunday


def test_utc_input_converted_to_ist():
    # 05:00 UTC = 10:30 IST -> open; 03:29 UTC = 08:59 IST -> closed
    assert is_market_hours(datetime(2026, 7, 29, 5, 0, tzinfo=timezone.utc))
    assert not is_market_hours(datetime(2026, 7, 29, 3, 29, tzinfo=timezone.utc))


def test_m3_nse_holiday_is_closed():
    # Gandhi Jayanti 2026 falls on a Friday — before the calendar landed,
    # fills/exits/marks would have run against frozen holiday prices.
    assert not is_market_hours(ist(2026, 10, 2, 11, 0))


def test_weekday_holidays_are_not_trading_days():
    assert not is_trading_day(date(2026, 1, 26))    # Republic Day (Mon)
    assert not is_trading_day(date(2026, 10, 2))    # Gandhi Jayanti (Fri)
    assert not is_trading_day(date(2026, 11, 10))   # Diwali Balipratipada
    assert is_trading_day(date(2026, 10, 1))        # ordinary Thursday


def test_muhurat_sunday_is_not_a_trading_day():
    # Diwali Laxmi Pujan 2026-11-08 (Sunday): the Muhurat session is
    # deliberately not a session for this system.
    assert not is_trading_day(date(2026, 11, 8))


def test_trading_days_between_skips_weekends():
    # Wed Jul 1 -> Wed Jul 8 2026: Jul 2, 3, 6, 7, 8 = 5 sessions.
    assert trading_days_between(date(2026, 7, 1), date(2026, 7, 8)) == 5


def test_trading_days_between_skips_holidays():
    # Wed Sep 30 -> Thu Oct 8 2026: Oct 1, 5, 6, 7, 8 = 5 sessions
    # (Oct 2 Gandhi Jayanti + the Oct 3/4 weekend do not advance the clock).
    assert trading_days_between(date(2026, 9, 30), date(2026, 10, 8)) == 5


def test_trading_days_between_zero_for_same_or_reversed():
    d = date(2026, 7, 1)
    assert trading_days_between(d, d) == 0
    assert trading_days_between(date(2026, 7, 8), d) == 0
