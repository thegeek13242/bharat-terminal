"""
NSE market hours helper.
Trading session: Mon–Fri, 09:00–15:30 IST, excluding exchange holidays.
"""
import logging
import os
from datetime import date, datetime, time, timedelta
import zoneinfo

logger = logging.getLogger(__name__)

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

_OPEN  = time(9, 0)
_CLOSE = time(15, 30)

# Continuous trading starts 09:15 IST; 09:00–09:15 is the pre-open auction,
# where quotes are stale/indicative and the day's `open` does not exist yet.
# Anything that FILLS, MARKS or PRICES must gate on is_session_live(), not
# is_market_hours() — the 2026-07-31/08-14 phantom-fill lesson (REFEX, MINID).
SESSION_FLOOR = time(9, 15)

# NSE equity-segment trading holidays. Source: NSE "Market Timings & Holidays"
# circular for 2026, cross-checked against two secondary lists on 2026-08-15.
# Weekend-falling holidays are included for documentation completeness (the
# weekday check already excludes them). Muhurat trading (2026-11-08, Sunday)
# is deliberately NOT a session for this system. Ad-hoc closures announced
# mid-year (e.g. 2026-01-15 municipal elections) go here on the next update,
# or immediately via TRADING_HOLIDAYS_EXTRA (comma-separated ISO dates) with
# no code change.
NSE_HOLIDAYS: frozenset = frozenset({
    # 2026
    date(2026, 1, 15),   # Maharashtra municipal elections (ad-hoc closure)
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 15),   # Mahashivratri (Sunday)
    date(2026, 3, 3),    # Holi
    date(2026, 3, 21),   # Id-Ul-Fitr (Saturday)
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
    date(2026, 4, 3),    # Good Friday
    date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    date(2026, 5, 1),    # Maharashtra Day
    date(2026, 5, 28),   # Bakri Id
    date(2026, 6, 26),   # Muharram
    date(2026, 8, 15),   # Independence Day (Saturday)
    date(2026, 9, 14),   # Ganesh Chaturthi
    date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    date(2026, 10, 20),  # Dussehra
    date(2026, 11, 8),   # Diwali Laxmi Pujan (Sunday; Muhurat session ignored)
    date(2026, 11, 10),  # Diwali — Balipratipada
    date(2026, 11, 24),  # Prakash Gurpurb Sri Guru Nanak Dev
    date(2026, 12, 25),  # Christmas
})

# Years the hardcoded calendar covers. Outside these, is_trading_day()
# degrades to weekday-only and WARNS once per year — a stale calendar in
# Jan 2027 must be a loud log line, not a silent regression to the
# pre-calendar bug (fills/exits running against frozen holiday prices).
_CALENDAR_YEARS = frozenset({2026})


def _extra_holidays() -> frozenset:
    """TRADING_HOLIDAYS_EXTRA env: ad-hoc closures without a redeploy."""
    out = set()
    for tok in os.getenv("TRADING_HOLIDAYS_EXTRA", "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(date.fromisoformat(tok))
        except ValueError:
            logger.warning(
                f"TRADING_HOLIDAYS_EXTRA: unparseable date {tok!r} ignored")
    return frozenset(out)


_HOLIDAYS = NSE_HOLIDAYS | _extra_holidays()
_warned_years: set = set()


def is_trading_day(d: date) -> bool:
    """Weekday and not an exchange holiday."""
    if d.weekday() >= 5:            # Sat/Sun
        return False
    if d.year not in _CALENDAR_YEARS and d.year not in _warned_years:
        _warned_years.add(d.year)
        logger.warning(
            f"holiday calendar has no entries for {d.year} — trading-day "
            f"checks degrade to weekday-only until NSE_HOLIDAYS is updated")
    return d not in _HOLIDAYS


def is_market_hours(now: datetime | None = None) -> bool:
    """Return True if *now* falls inside NSE trading hours."""
    if now is None:
        now = datetime.now(IST)
    else:
        now = now.astimezone(IST)

    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return _OPEN <= t <= _CLOSE


def is_session_live(now: datetime | None = None) -> bool:
    """Continuous session only: is_market_hours() minus the 09:00–09:15
    pre-open auction. Injectable `now` for tests (same convention as
    is_market_hours)."""
    if now is None:
        now = datetime.now(IST)
    else:
        now = now.astimezone(IST)
    return is_market_hours(now) and now.time() >= SESSION_FLOOR


def trading_days_between(start: date, end: date) -> int:
    """Trading days strictly after `start`, up to and including `end` —
    the holding-period clock (entry day counts as 0). Returns 0 when
    end <= start. Validated strategy horizons are SESSIONS, not calendar
    days (C2, review 2026-07-30): 20 calendar days is only ~14 sessions,
    which silently exited PEAD ~30% early."""
    if end <= start:
        return 0
    n = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n
