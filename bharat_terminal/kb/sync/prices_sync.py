"""
Daily price sync -- fetches end-of-day OHLCV data for NSE-listed equities.
Uses NSE's bhavcopy (public, no auth required) for bulk daily downloads.
"""
import csv
import gzip
import io
import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Optional
from zipfile import ZipFile

import httpx

logger = logging.getLogger(__name__)

# NSE bhavcopy URL pattern (daily OHLCV zip)
# Format: https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
NSE_BHAVCOPY_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
)

# Fallback: older bhavcopy format
NSE_BHAVCOPY_OLD_URL = (
    "https://www1.nseindia.com/content/historical/EQUITIES/{year}/{month_abbr}/"
    "cm{dd}{month_abbr}{year}bhav.csv.zip"
)

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/octet-stream",
    "Referer": "https://www.nseindia.com/",
}

MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


async def fetch_bhavcopy(for_date: date) -> List[Dict]:
    """
    Download NSE bhavcopy (bulk OHLCV) for a given date.
    Returns list of dicts: symbol, date, open, high, low, close, volume.
    Skips non-EQ series (e.g., ETFs, derivatives series).
    """
    date_str = for_date.strftime("%Y%m%d")

    async with httpx.AsyncClient(timeout=60, headers=NSE_HEADERS, follow_redirects=True) as client:
        # Warm up session cookie
        try:
            await client.get("https://www.nseindia.com/", timeout=10)
        except Exception:
            pass

        url = NSE_BHAVCOPY_URL.format(date=date_str)
        logger.info(f"Downloading bhavcopy from {url}")

        try:
            resp = await client.get(url)
            resp.raise_for_status()
            zip_bytes = resp.content
        except httpx.HTTPStatusError as e:
            logger.warning(f"New bhavcopy URL failed ({e.response.status_code}), trying legacy URL")
            # Fallback to legacy format
            url = NSE_BHAVCOPY_OLD_URL.format(
                year=for_date.year,
                month_abbr=MONTH_ABBR[for_date.month],
                dd=for_date.strftime("%d"),
            )
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                zip_bytes = resp.content
            except Exception as e2:
                logger.error(f"Both bhavcopy URLs failed for {for_date}: {e2}")
                return []

        # Parse the zip
        prices = []
        try:
            with ZipFile(io.BytesIO(zip_bytes)) as zf:
                csv_filename = zf.namelist()[0]
                with zf.open(csv_filename) as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
                    for row in reader:
                        series = (row.get("SERIES") or row.get("SctySrs") or "").strip()
                        if series not in ("EQ", "BE"):
                            continue

                        symbol = (
                            row.get("SYMBOL") or
                            row.get("TckrSymb") or
                            ""
                        ).strip()
                        if not symbol:
                            continue

                        def safe_float(key_candidates):
                            for k in key_candidates:
                                v = row.get(k, "").strip()
                                if v:
                                    try:
                                        return float(v.replace(",", ""))
                                    except ValueError:
                                        pass
                            return None

                        def safe_int(key_candidates):
                            for k in key_candidates:
                                v = row.get(k, "").strip()
                                if v:
                                    try:
                                        return int(float(v.replace(",", "")))
                                    except ValueError:
                                        pass
                            return None

                        close = safe_float(["CLOSE", "ClsePric", "LAST"])
                        if close is None:
                            continue

                        prices.append({
                            "symbol": symbol,
                            "date": for_date,
                            "open": safe_float(["OPEN", "OpnPric"]),
                            "high": safe_float(["HIGH", "HghPric"]),
                            "low": safe_float(["LOW", "LwPric"]),
                            "close": close,
                            "volume": safe_int(["TOTTRDQTY", "TtlTradgVol"]),
                        })
        except Exception as e:
            logger.error(f"Failed to parse bhavcopy zip for {for_date}: {e}")
            return []

        logger.info(f"Parsed {len(prices)} price records for {for_date}")
        return prices


async def fetch_price_range(
    symbol: str,
    start_date: date,
    end_date: date,
) -> List[Dict]:
    """
    Fetch historical OHLCV for a specific symbol over a date range.
    Uses NSE's historical data API endpoint.
    """
    url = "https://www.nseindia.com/api/historical/cm/equity"
    params = {
        "symbol": symbol.upper(),
        "series": ["EQ"],
        "from": start_date.strftime("%d-%m-%Y"),
        "to": end_date.strftime("%d-%m-%Y"),
    }

    headers = {
        **NSE_HEADERS,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        # Must warm up with a cookie first
        try:
            await client.get("https://www.nseindia.com/", timeout=10)
        except Exception:
            pass

        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            json_data = resp.json()
        except Exception as e:
            logger.error(f"NSE historical API failed for {symbol}: {e}")
            return []

    prices = []
    for record in json_data.get("data", []):
        try:
            rec_date = datetime.strptime(record["CH_TIMESTAMP"], "%Y-%m-%d").date()
            prices.append({
                "symbol": symbol.upper(),
                "date": rec_date,
                "open": float(record.get("CH_OPENING_PRICE") or 0),
                "high": float(record.get("CH_TRADE_HIGH_PRICE") or 0),
                "low": float(record.get("CH_TRADE_LOW_PRICE") or 0),
                "close": float(record.get("CH_CLOSING_PRICE") or 0),
                "volume": int(float(record.get("CH_TOT_TRADED_QTY") or 0)),
            })
        except (KeyError, ValueError) as e:
            logger.debug(f"Skipping malformed price record: {e}")
            continue

    logger.info(f"Fetched {len(prices)} price records for {symbol} ({start_date} to {end_date})")
    return prices


def last_trading_day(reference: Optional[date] = None) -> date:
    """
    Return the most recent NSE trading day (weekdays only, no holiday check).
    For production use, integrate with NSE holiday calendar.
    """
    d = reference or date.today()
    # Go back until we hit a weekday
    while d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        d -= timedelta(days=1)
    return d
