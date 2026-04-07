"""
NSE market hours helper.
Trading session: Mon–Fri, 09:00–15:30 IST.
"""
from datetime import datetime, time
import zoneinfo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

_OPEN  = time(9, 0)
_CLOSE = time(15, 30)


def is_market_hours(now: datetime | None = None) -> bool:
    """Return True if *now* falls inside NSE trading hours."""
    if now is None:
        now = datetime.now(IST)
    else:
        now = now.astimezone(IST)

    if now.weekday() >= 5:          # Sat/Sun
        return False
    t = now.time()
    return _OPEN <= t <= _CLOSE
