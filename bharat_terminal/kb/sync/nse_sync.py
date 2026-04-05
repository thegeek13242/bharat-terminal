"""
NSE company master sync -- fetches complete list of NSE-listed companies.
Uses NSE's official equity master CSV (no authentication required).
"""
import csv
import io
import logging
import os
from datetime import datetime, timezone
from typing import List, Dict

import httpx

logger = logging.getLogger(__name__)

# NSE's official equity master download (public URL)
NSE_EQUITY_MASTER_URL = "https://www.nseindia.com/api/equity-master"
NSE_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv,application/json",
    "Referer": "https://www.nseindia.com/",
}


async def fetch_nse_company_list() -> List[Dict]:
    """
    Fetch complete NSE equity master.
    Returns list of dicts with: symbol, company_name, isin, series, listing_date
    """
    async with httpx.AsyncClient(timeout=30, headers=HEADERS) as client:
        # First get session cookie
        await client.get("https://www.nseindia.com/")

        resp = await client.get(NSE_EQUITY_LIST_URL)
        resp.raise_for_status()

        companies = []
        reader = csv.DictReader(io.StringIO(resp.text))

        for row in reader:
            symbol = (row.get("SYMBOL") or "").strip()
            if not symbol:
                continue

            companies.append({
                "symbol": symbol,
                "company_name": (row.get("NAME OF COMPANY") or "").strip(),
                "isin": (row.get("ISIN NUMBER") or "").strip(),
                "series": (row.get("SERIES") or "EQ").strip(),
                "listing_date": (row.get("DATE OF LISTING") or "").strip(),
                "exchange": "NSE",
            })

        logger.info(f"Fetched {len(companies)} NSE companies")
        return companies
