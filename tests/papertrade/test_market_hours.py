"""is_market_hours — gates 6 papertrade loops and the analysis RT path.
Injectable `now`, so boundaries are exact. All inputs tz-aware (naive-input
semantics depend on the host TZ and are deliberately not pinned here).

M3 regression (review 2026-07-30): no NSE holiday calendar — xfail until one
exists.
"""
from datetime import datetime, timezone

import pytest

from bharat_terminal.analysis.market_hours import IST, is_market_hours


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


@pytest.mark.xfail(
    strict=True,
    reason="M3 (MAJOR, review 2026-07-30): no NSE trading-holiday calendar — "
           "2026-10-02 (Gandhi Jayanti, a Friday) is treated as a session, so "
           "fills/exits/marks can run against frozen prices. Remove marker "
           "when a holiday calendar lands.")
def test_m3_nse_holiday_is_closed():
    assert not is_market_hours(ist(2026, 10, 2, 11, 0))
