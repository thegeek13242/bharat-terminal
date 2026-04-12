"""
Screener.in data sync -- fetches fundamental financial data for companies.
Screener.in provides free access to financial data for Indian listed companies.

Secondary data source for Bharat Terminal. Primary is NSE XBRL.
Used for verification and fields not available in XBRL (description, P/B, ROE).
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

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


# ── Full extraction (secondary source for verification) ────────────────────

async def fetch_screener_full(
    symbol: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Dict[str, Any]]:
    """
    Full data extraction from Screener.in.
    Returns everything available: TTM ratios, quarterly results, annual P&L,
    balance sheet summary, shareholding, and description.

    Accepts an optional httpx.AsyncClient for connection reuse in batch pipelines.
    """
    url = SCREENER_COMPANY_URL.format(symbol=symbol.upper())
    own_client = client is None

    if own_client:
        client = httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True)

    try:
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                url_standalone = f"{SCREENER_BASE_URL}/company/{symbol.upper()}/"
                resp = await client.get(url_standalone)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning(f"Screener full fetch failed for {symbol}: HTTP {e.response.status_code}")
            return None
        except httpx.RequestError as e:
            logger.warning(f"Screener full fetch failed for {symbol}: {e}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        data: Dict[str, Any] = {"symbol": symbol.upper()}

        # Ratios (same as fetch_screener_data)
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
                elif "p/b" in name or "price to book" in name:
                    data["pb_ratio"] = _parse_number(value_text)
                elif "dividend yield" in name:
                    data["dividend_yield_pct"] = _parse_pct(value_text)
                elif "roce" in name:
                    data["roce_pct"] = _parse_pct(value_text)
                elif "roe" in name:
                    data["roe_pct"] = _parse_pct(value_text)
                elif "face value" in name:
                    data["face_value"] = _parse_number(value_text)

        # Description
        about_section = soup.find("div", class_="company-profile")
        if about_section:
            desc_el = about_section.find("p")
            if desc_el:
                data["description"] = desc_el.get_text(strip=True)[:1000]

        # Quarterly results
        data["quarterly_results"] = _extract_full_quarterly(soup)

        # Annual P&L
        data["annual_results"] = _extract_full_annual(soup)

        # Balance sheet summary
        data["balance_sheet"] = _extract_balance_sheet(soup)

        # Shareholding
        data["shareholding"] = _extract_shareholding(soup)

        # P/B ratio from balance sheet if not in ratios
        if not data.get("pb_ratio") and data.get("mcap_cr") and data.get("balance_sheet"):
            equity = data["balance_sheet"].get("total_equity_cr")
            if equity and equity > 0:
                data["pb_ratio"] = round(data["mcap_cr"] / equity, 2)

        logger.info(
            f"Screener full: {symbol} | "
            f"quarters={len(data.get('quarterly_results', []))} "
            f"annual={len(data.get('annual_results', []))}"
        )
        return data

    finally:
        if own_client:
            await client.aclose()


def _extract_full_quarterly(soup) -> List[Dict[str, Any]]:
    """Parse section#quarters table: all visible quarters with full P&L."""
    section = soup.find("section", id="quarters")
    if not section:
        return []

    table = section.find("table")
    if not table:
        return []

    # Parse column headers (dates)
    thead = table.find("thead")
    if not thead:
        return []
    header_cells = thead.find_all("th")
    dates = []
    for th in header_cells[1:]:  # Skip label column
        date_text = th.get_text(strip=True)
        parsed_date = _parse_screener_date(date_text)
        dates.append(parsed_date)

    if not dates:
        return []

    # Parse rows
    row_data = _parse_table_rows(table, len(dates))

    # Build quarterly result dicts
    results = []
    for i, d in enumerate(dates):
        if not d:
            continue
        q = {"period_end": d}
        for label, values in row_data.items():
            if i < len(values):
                val = values[i]
                if "sales" in label or "revenue" in label:
                    q["revenue_cr"] = val
                elif "expense" in label and "total" not in label:
                    pass
                elif "operating profit" in label or "ebitda" in label:
                    q["ebitda_cr"] = val
                elif "net profit" in label or "profit after" in label:
                    q["pat_cr"] = val
                elif "eps" in label:
                    q["eps"] = val
        if q.get("revenue_cr") is not None or q.get("pat_cr") is not None:
            results.append(q)

    return results


def _extract_full_annual(soup) -> List[Dict[str, Any]]:
    """Parse section#profit-loss table: all visible years."""
    section = soup.find("section", id="profit-loss")
    if not section:
        return []

    table = section.find("table")
    if not table:
        return []

    thead = table.find("thead")
    if not thead:
        return []
    header_cells = thead.find_all("th")
    dates = []
    for th in header_cells[1:]:
        date_text = th.get_text(strip=True)
        parsed_date = _parse_screener_date(date_text)
        dates.append(parsed_date)

    if not dates:
        return []

    row_data = _parse_table_rows(table, len(dates))

    results = []
    for i, d in enumerate(dates):
        if not d:
            continue
        yr = {"period_end": d}
        for label, values in row_data.items():
            if i < len(values):
                val = values[i]
                if "sales" in label or "revenue" in label:
                    yr["revenue_cr"] = val
                elif "operating profit" in label or "ebitda" in label:
                    yr["ebitda_cr"] = val
                elif "net profit" in label or "profit after" in label:
                    yr["pat_cr"] = val
                elif "eps" in label:
                    yr["eps"] = val
                elif "interest" in label and "coverage" not in label:
                    yr["interest_cr"] = val
                elif "depreciation" in label:
                    yr["depreciation_cr"] = val
                elif "tax" in label and "before" not in label:
                    yr["tax_cr"] = val
        if yr.get("revenue_cr") is not None:
            results.append(yr)

    return results


def _extract_balance_sheet(soup) -> Optional[Dict[str, Any]]:
    """Parse section#balance-sheet for latest period: equity, debt, book value."""
    section = soup.find("section", id="balance-sheet")
    if not section:
        return None

    table = section.find("table")
    if not table:
        return None

    rows = table.find_all("tr")
    bs = {}
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label_cell = row.find("td", class_="text") or cells[0]
        label = label_cell.get_text(strip=True).lower()
        last_val = _parse_number(cells[-1].get_text(strip=True))
        if last_val is None:
            continue

        if "total equity" in label or "shareholder" in label:
            bs["total_equity_cr"] = last_val
        elif "borrowing" in label or "total debt" in label:
            bs.setdefault("total_debt_cr", 0)
            bs["total_debt_cr"] += last_val
        elif "total liabilities" in label:
            bs["total_liabilities_cr"] = last_val
        elif "total assets" in label:
            bs["total_assets_cr"] = last_val

    return bs if bs else None


def _extract_shareholding(soup) -> Optional[Dict[str, Any]]:
    """Parse section#shareholding if present: latest quarter percentages."""
    section = soup.find("section", id="shareholding")
    if not section:
        return None

    table = section.find("table")
    if not table:
        return None

    rows = table.find_all("tr")
    sh = {}
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).lower()
        last_val = _parse_pct(cells[-1].get_text(strip=True))
        if last_val is None:
            continue

        if "promoter" in label and "pledge" not in label:
            sh["promoter_pct"] = last_val
        elif "fii" in label or "foreign" in label:
            sh["fii_pct"] = last_val
        elif "dii" in label or "domestic" in label:
            sh["dii_pct"] = last_val
        elif "public" in label or "retail" in label:
            sh["public_pct"] = last_val

    return sh if sh else None


# ── Helpers ─────────────────────────────────────────────────────────────────

def _parse_screener_date(text: str) -> Optional[str]:
    """Parse Screener column header date like 'Mar 2025' or 'Jun 2024' to 'YYYY-MM-DD'."""
    text = text.strip()
    if not text:
        return None
    # Try "Mar 2025" format
    for fmt in ("%b %Y", "%B %Y", "%b %y"):
        try:
            dt = datetime.strptime(text, fmt)
            # Screener dates are period-end: Mar 2025 = 2025-03-31
            import calendar
            last_day = calendar.monthrange(dt.year, dt.month)[1]
            return f"{dt.year}-{dt.month:02d}-{last_day:02d}"
        except ValueError:
            continue
    # Try "TTM" — use today
    if text.upper() == "TTM":
        return "TTM"
    return None


def _parse_table_rows(table, num_cols: int) -> Dict[str, List[Optional[float]]]:
    """Parse a Screener data table into {row_label: [values...]}."""
    result = {}
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        label_cell = row.find("td", class_="text") or cells[0]
        label = label_cell.get_text(strip=True).lower()
        value_cells = [c for c in cells if c != label_cell]

        values = []
        for cell in value_cells[:num_cols]:
            values.append(_parse_number(cell.get_text(strip=True)))

        result[label] = values
    return result


async def verify_against_screener(
    symbol: str,
    nse_revenue_cr: Optional[float],
    nse_pat_cr: Optional[float],
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Cross-check NSE XBRL data against Screener.in.
    Returns dict with match status and discrepancies.
    """
    screener_data = await fetch_screener_full(symbol, client)
    if not screener_data:
        return {"verified": False, "reason": "screener_unavailable"}

    result = {"verified": True, "discrepancies": []}

    scr_revenue = screener_data.get("revenue_ttm_cr")
    scr_pat = screener_data.get("pat_cr")

    # Revenue check (allow 2% tolerance — different rounding)
    if nse_revenue_cr and scr_revenue:
        pct_diff = abs(nse_revenue_cr - scr_revenue) / max(nse_revenue_cr, 1) * 100
        if pct_diff > 2.0:
            result["discrepancies"].append({
                "field": "revenue_ttm_cr",
                "nse": nse_revenue_cr,
                "screener": scr_revenue,
                "pct_diff": round(pct_diff, 2),
            })

    # PAT check
    if nse_pat_cr and scr_pat:
        pct_diff = abs(nse_pat_cr - scr_pat) / max(abs(nse_pat_cr), 1) * 100
        if pct_diff > 2.0:
            result["discrepancies"].append({
                "field": "pat_cr",
                "nse": nse_pat_cr,
                "screener": scr_pat,
                "pct_diff": round(pct_diff, 2),
            })

    if result["discrepancies"]:
        result["verified"] = False
        result["reason"] = "data_mismatch"

    # Return extra fields Screener has that XBRL doesn't
    result["extra_fields"] = {}
    for field in ("pe_ratio", "pb_ratio", "roe_pct", "description", "mcap_cr", "shareholding"):
        if screener_data.get(field):
            result["extra_fields"][field] = screener_data[field]

    return result
