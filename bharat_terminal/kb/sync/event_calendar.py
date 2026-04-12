"""
NSE Event Calendar — fetches board meeting dates for financial result announcements.

Polls the NSE corporate-board-meetings API to discover when companies will
announce quarterly results, enabling targeted single-symbol sync instead of
full-tier sweeps.

API: https://www.nseindia.com/api/corporate-board-meetings?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
"""
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Set

import httpx

from bharat_terminal.kb.sync.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

NSE_BOARD_MEETINGS_API = "https://www.nseindia.com/api/corporate-board-meetings"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# Keywords in bm_desc that indicate a financial results announcement
RESULT_KEYWORDS = ["financial result", "quarterly result", "half yearly result",
                   "annual result", "audited result", "unaudited result"]


def _is_result_announcement(entry: Dict) -> bool:
    """Check if a board meeting entry is about financial results."""
    desc = (entry.get("bm_desc") or "").lower()
    purpose = (entry.get("bm_purpose") or "").lower()
    combined = desc + " " + purpose
    return any(kw in combined for kw in RESULT_KEYWORDS)


def _parse_nse_date(date_str: str) -> Optional[date]:
    """Parse NSE date format like '16-Apr-2026'."""
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


async def fetch_result_calendar(
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    from_date: date,
    to_date: date,
) -> List[Dict]:
    """
    Fetch upcoming financial result announcement dates from NSE board meetings API.

    Returns deduplicated list of:
        {"symbol": str, "result_date": date, "description": str}
    """
    params = {
        "index": "equities",
        "from_date": from_date.strftime("%d-%m-%Y"),
        "to_date": to_date.strftime("%d-%m-%Y"),
    }

    async with limiter:
        try:
            resp = await client.get(NSE_BOARD_MEETINGS_API, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch board meetings calendar: {e}")
            return []

    if not isinstance(data, list):
        logger.warning(f"Unexpected board meetings response type: {type(data)}")
        return []

    # Filter to result announcements and deduplicate by (symbol, date)
    seen: Set[tuple] = set()
    results = []

    for entry in data:
        if not _is_result_announcement(entry):
            continue

        symbol = (entry.get("bm_symbol") or "").strip()
        result_date = _parse_nse_date(entry.get("bm_date", ""))

        if not symbol or not result_date:
            continue

        key = (symbol, result_date)
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "symbol": symbol,
            "result_date": result_date,
            "description": (entry.get("bm_desc") or "").strip()[:500],
        })

    logger.info(
        f"Calendar {from_date} to {to_date}: "
        f"{len(data)} board meetings, {len(results)} result announcements"
    )
    return results


async def get_symbols_with_results_on(
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    target_date: date,
    lookback_days: int = 3,
) -> List[str]:
    """
    Get symbols that had results announced on target_date or in the lookback window.

    The lookback handles cases where results are filed a day or two after the
    board meeting, giving XBRL time to appear on NSE.
    """
    from_date = target_date - timedelta(days=lookback_days)
    entries = await fetch_result_calendar(client, limiter, from_date, target_date)
    symbols = sorted(set(e["symbol"] for e in entries))
    logger.info(f"Symbols with results {from_date} to {target_date}: {symbols}")
    return symbols


async def get_upcoming_result_symbols(
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    days_ahead: int = 7,
) -> Dict[date, List[str]]:
    """
    Get a schedule of upcoming result announcements for the next N days.

    Returns: {date: [symbol, ...], ...}
    """
    today = date.today()
    to_date = today + timedelta(days=days_ahead)
    entries = await fetch_result_calendar(client, limiter, today, to_date)

    schedule: Dict[date, List[str]] = {}
    for entry in entries:
        d = entry["result_date"]
        schedule.setdefault(d, []).append(entry["symbol"])

    for d in sorted(schedule):
        logger.info(f"  {d}: {len(schedule[d])} companies — {schedule[d][:10]}")

    return schedule
