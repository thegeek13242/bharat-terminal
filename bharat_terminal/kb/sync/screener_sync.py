"""
Screener.in data sync -- fetches fundamental financial data for companies.
Screener.in provides free access to financial data for Indian listed companies.
"""
import logging
import re
from typing import Optional, Dict, Any
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SCREENER_BASE_URL = "https://www.screener.in"
SCREENER_COMPANY_URL = "https://www.screener.in/company/{symbol}/consolidated/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.screener.in/",
}


def _parse_number(text: str) -> Optional[float]:
    """Parse Indian number format: '1,23,456.78' -> 123456.78"""
    if not text:
        return None
    cleaned = re.sub(r"[,\s]", "", text.strip())
    # Handle crore/lakh suffixes
    if cleaned.endswith("Cr"):
        try:
            return float(cleaned[:-2]) * 1e7  # Crores to absolute, then back to crores
        except ValueError:
            pass
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_pct(text: str) -> Optional[float]:
    """Parse percentage: '12.5%' -> 12.5"""
    if not text:
        return None
    cleaned = text.strip().rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


async def fetch_screener_data(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Fetch financial data from Screener.in for a given NSE symbol.

    Returns a dict with:
      - revenue_ttm_cr
      - ebitda_margin_pct
      - pat_cr
      - eps_ttm
      - pe_ratio
      - pb_ratio
      - roe_pct
      - net_debt_cr
      - interest_coverage
      - fcf_yield_pct
      - mcap_cr
      - sector
      - description
    """
    url = SCREENER_COMPANY_URL.format(symbol=symbol.upper())

    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                # Try standalone (non-consolidated) URL
                url_standalone = f"{SCREENER_BASE_URL}/company/{symbol.upper()}/"
                resp = await client.get(url_standalone)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning(f"Screener fetch failed for {symbol}: HTTP {e.response.status_code}")
            return None
        except httpx.RequestError as e:
            logger.warning(f"Screener fetch failed for {symbol}: {e}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        data: Dict[str, Any] = {"symbol": symbol.upper()}

        # Parse ratios section
        ratios_section = soup.find("section", id="top-ratios")
        if ratios_section:
            for li in ratios_section.find_all("li"):
                name_el = li.find("span", class_="name")
                value_el = li.find("span", class_="value")
                if not name_el or not value_el:
                    continue
                name = name_el.get_text(strip=True).lower()
                value_text = value_el.get_text(strip=True)

                if "market cap" in name:
                    data["mcap_cr"] = _parse_number(value_text)
                elif "p/e" in name or "stock p/e" in name:
                    data["pe_ratio"] = _parse_number(value_text)
                elif "book value" in name:
                    data["book_value"] = _parse_number(value_text)
                elif "dividend yield" in name:
                    data["dividend_yield_pct"] = _parse_pct(value_text)
                elif "roce" in name:
                    data["roce_pct"] = _parse_pct(value_text)
                elif "roe" in name:
                    data["roe_pct"] = _parse_pct(value_text)
                elif "face value" in name:
                    data["face_value"] = _parse_number(value_text)

        # Parse P&L section for TTM revenue and PAT
        pl_section = soup.find("section", id="profit-loss")
        if pl_section:
            table = pl_section.find("table")
            if table:
                headers_row = table.find("thead")
                if headers_row:
                    # Last column is usually TTM or most recent year
                    _extract_pl_data(table, data)

        # Parse quarterly results for TTM estimates
        quarterly_section = soup.find("section", id="quarters")
        if quarterly_section:
            _extract_quarterly_data(quarterly_section, data)

        # Parse company description
        about_section = soup.find("div", class_="company-profile")
        if about_section:
            desc_el = about_section.find("p")
            if desc_el:
                data["description"] = desc_el.get_text(strip=True)[:1000]

        # Parse EPS from data attributes if available
        eps_el = soup.find("span", attrs={"data-field": "eps"})
        if eps_el:
            data["eps_ttm"] = _parse_number(eps_el.get_text(strip=True))

        logger.info(f"Screener data fetched for {symbol}: {list(data.keys())}")
        return data


def _extract_pl_data(table, data: dict):
    """Extract P&L data from Screener table (revenue, PAT, EBITDA margin)."""
    rows = table.find_all("tr")
    for row in rows:
        th = row.find("td", class_="text")
        if not th:
            th = row.find("th")
        if not th:
            continue

        label = th.get_text(strip=True).lower()
        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        # Use last data cell (most recent year / TTM)
        last_val_text = cells[-1].get_text(strip=True)
        value = _parse_number(last_val_text)

        if value is None:
            continue

        if "sales" in label or "revenue" in label:
            data["revenue_ttm_cr"] = value
        elif "net profit" in label or "pat" in label:
            data["pat_cr"] = value
        elif "ebitda" in label or "operating profit" in label:
            data["ebitda_cr"] = value
        elif "interest" in label and "coverage" not in label:
            data["interest_expense_cr"] = value
        elif "eps" in label:
            data["eps_ttm"] = value

    # Compute derived metrics
    if data.get("revenue_ttm_cr") and data.get("ebitda_cr"):
        data["ebitda_margin_pct"] = (data["ebitda_cr"] / data["revenue_ttm_cr"]) * 100

    if data.get("ebitda_cr") and data.get("interest_expense_cr") and data["interest_expense_cr"] > 0:
        data["interest_coverage"] = data["ebitda_cr"] / data["interest_expense_cr"]


def _extract_quarterly_data(section, data: dict):
    """Extract TTM estimates from quarterly data."""
    table = section.find("table")
    if not table:
        return

    rows = table.find_all("tr")
    quarterly_revenues = []
    quarterly_pats = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label_cell = row.find("td", class_="text") or (cells[0] if cells else None)
        if not label_cell:
            continue
        label = label_cell.get_text(strip=True).lower()

        # Get last 4 quarters (TTM)
        value_cells = [c for c in cells if c != label_cell]
        last_4 = value_cells[-4:] if len(value_cells) >= 4 else value_cells

        values = [_parse_number(c.get_text(strip=True)) for c in last_4]
        values = [v for v in values if v is not None]

        if "sales" in label or "revenue" in label:
            quarterly_revenues = values
        elif "net profit" in label or "pat" in label:
            quarterly_pats = values

    if quarterly_revenues and len(quarterly_revenues) == 4:
        data["revenue_ttm_cr"] = sum(quarterly_revenues)
    if quarterly_pats and len(quarterly_pats) == 4:
        data["pat_cr"] = sum(quarterly_pats)
