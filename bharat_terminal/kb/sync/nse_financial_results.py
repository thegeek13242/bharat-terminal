"""
NSE Financial Results sync — fetches filing index + downloads & parses XBRL.

Primary data source for Bharat Terminal knowledge base.
Uses NSE's corporates-financial-results API to discover filings,
then downloads XBRL XML and parses with stdlib ElementTree (zero dependencies).

XBRL Context Reference:
  OneD  = Current quarter (duration period)
  FourD = Year-to-date cumulative (duration period)
  OneI  = Current quarter-end (instant — balance sheet items)
"""
import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import List, Dict, Optional, Any, Tuple

import httpx

from bharat_terminal.kb.sync.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
NSE_API = f"{NSE_BASE}/api/corporates-financial-results"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# XBRL tag → our field mapping (values are in absolute INR, we convert to Crores)
XBRL_PL_TAGS = {
    "RevenueFromOperations": "revenue_cr",
    "OtherIncome": "other_income_cr",
    "Income": "total_income_cr",
    "CostOfMaterialsConsumed": "cost_of_materials_cr",
    "PurchasesOfStockInTrade": "purchases_stock_in_trade_cr",
    "ChangesInInventoriesOfFinishedGoodsWorkInProgressAndStockInTrade": "inventory_changes_cr",
    "EmployeeBenefitExpense": "employee_expense_cr",
    "FinanceCosts": "finance_costs_cr",
    "DepreciationDepletionAndAmortisationExpense": "depreciation_cr",
    "OtherExpenses": "other_expenses_cr",
    "Expenses": "total_expenses_cr",
    "ProfitBeforeExceptionalItemsAndTax": "pbt_before_exceptional_cr",
    "ExceptionalItemsBeforeTax": "exceptional_items_cr",
    "ProfitBeforeTax": "pbt_cr",
    "TaxExpense": "tax_cr",
    "ProfitLossForPeriod": "pat_cr",
    "ProfitOrLossAttributableToOwnersOfParent": "pat_attributable_cr",
    "OtherComprehensiveIncomeNetOfTaxes": "other_comprehensive_income_cr",
}

XBRL_EPS_TAGS = {
    "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_basic",
    "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_diluted",
    "BasicEarningsLossPerShareFromContinuingOperations": "eps_basic_continuing",
    "DilutedEarningsLossPerShareFromContinuingOperations": "eps_diluted_continuing",
}

XBRL_META_TAGS = {
    "FaceValueOfEquityShareCapital": "face_value",
    "PaidUpValueOfEquityShareCapital": "paid_up_capital_cr",
    "DebtEquityRatio": "debt_equity_ratio",
    "InterestServiceCoverageRatio": "interest_coverage_ratio",
}

# Value in Crores = absolute_value / 1e7.  But XBRL values are already in INR.
# Actually: XBRL reports in INR absolute. LevelOfRoundingUsedInFinancialStatements = "Crores"
# means the values ARE already in Crores * 1e7 (i.e., absolute INR).
# We divide by 1e7 to get Crores.
INR_TO_CRORES = 1e7


# ── Filing Index ─────────────────────────────────────────────────────────────

async def fetch_filing_index(
    symbol: str,
    client: httpx.AsyncClient,
    limiter: RateLimiter,
    period: str = "Quarterly",
) -> List[Dict[str, Any]]:
    """
    Fetch the list of financial result filings from NSE for a symbol.
    Returns raw JSON list from NSE API.
    """
    async with limiter:
        params = {"index": "equities", "symbol": symbol}
        if period:
            params["period"] = period
        try:
            resp = await client.get(NSE_API, params=params)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "json" not in content_type and "html" in content_type:
                # NSE returned HTML instead of JSON — cookie expired
                logger.warning(f"NSE returned HTML for {symbol} (cookie expired?)")
                # Refresh cookies and retry once
                await refresh_nse_cookies(client)
                resp = await client.get(NSE_API, params=params)
                resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            logger.warning(f"Unexpected response type for {symbol}: {type(data)}")
            return []
        except httpx.HTTPStatusError as e:
            logger.warning(f"NSE filing index failed for {symbol}: HTTP {e.response.status_code}")
            return []
        except Exception as e:
            logger.warning(f"NSE filing index failed for {symbol}: {type(e).__name__}: {e}")
            return []


def parse_filing_metadata(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a single filing record from the NSE API into our schema."""
    xbrl_url = raw.get("xbrl", "")
    # NSE returns "https://nsearchives.nseindia.com/corporate/xbrl/-" for no XBRL
    has_xbrl = xbrl_url and not xbrl_url.endswith("/-")

    def parse_nse_date(date_str: str) -> Optional[date]:
        if not date_str:
            return None
        for fmt in ("%d-%b-%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return None

    def parse_nse_datetime(dt_str: str) -> Optional[datetime]:
        if not dt_str:
            return None
        for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
            try:
                return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    return {
        "symbol": raw.get("symbol", ""),
        "isin": raw.get("isin"),
        "from_date": parse_nse_date(raw.get("fromDate", "")),
        "to_date": parse_nse_date(raw.get("toDate", "")),
        "period": raw.get("period", ""),
        "relating_to": raw.get("relatingTo"),
        "financial_year": raw.get("financialYear"),
        "consolidated": raw.get("consolidated", ""),
        "audited": raw.get("audited", ""),
        "ind_as": raw.get("indAs"),
        "format": raw.get("format"),
        "xbrl_url": xbrl_url if has_xbrl else None,
        "detail_url": raw.get("resultDetailedDataLink") or None,
        "seq_number": raw.get("seqNumber"),
        "filing_date": parse_nse_datetime(raw.get("filingDate", "")),
        "broadcast_date": parse_nse_datetime(raw.get("broadCastDate", "")),
    }


def filter_consolidated_quarterly(filings: List[Dict]) -> List[Dict]:
    """
    Filter to keep only consolidated quarterly filings with XBRL.
    Prefers consolidated; falls back to non-consolidated if no consolidated exists.
    """
    quarterly = [f for f in filings if f["period"] == "Quarterly"]

    # Group by period
    by_period = {}
    for f in quarterly:
        key = (f["from_date"], f["to_date"])
        by_period.setdefault(key, []).append(f)

    result = []
    for key, group in sorted(by_period.items(), reverse=True):
        # Prefer consolidated with XBRL
        consolidated_xbrl = [f for f in group if f["consolidated"] == "Consolidated" and f["xbrl_url"]]
        if consolidated_xbrl:
            result.append(consolidated_xbrl[0])
            continue
        # Fallback: non-consolidated with XBRL
        any_xbrl = [f for f in group if f["xbrl_url"]]
        if any_xbrl:
            result.append(any_xbrl[0])

    return result


# ── XBRL Download ────────────────────────────────────────────────────────────

async def download_xbrl(
    url: str,
    client: httpx.AsyncClient,
    limiter: RateLimiter,
) -> Optional[bytes]:
    """Download an XBRL XML file from NSE archives."""
    async with limiter:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            logger.warning(f"XBRL download failed: {url}: {e}")
            return None


# ── XBRL Parsing ─────────────────────────────────────────────────────────────

def parse_xbrl(xml_bytes: bytes) -> Dict[str, Any]:
    """
    Parse an NSE XBRL XML file and extract financial facts.

    Returns a dict with:
      - quarter: dict of quarterly P&L fields
      - ytd: dict of YTD cumulative fields
      - metadata: company info, dates, audit status
      - segment_data: list of segment revenue/profit dicts
      - raw_facts: dict of ALL tag→value pairs for audit trail
    """
    root = ET.fromstring(xml_bytes)

    # Collect all facts grouped by context
    facts_by_context: Dict[str, Dict[str, str]] = {}
    raw_facts: Dict[str, str] = {}

    for elem in root.iter():
        ctx = elem.get("contextRef")
        if ctx and elem.text and elem.text.strip():
            tag = elem.tag.split("}")[1] if "}" in elem.tag else elem.tag
            value = elem.text.strip()
            facts_by_context.setdefault(ctx, {})[tag] = value
            raw_facts[f"{ctx}:{tag}"] = value

    one_d = facts_by_context.get("OneD", {})
    four_d = facts_by_context.get("FourD", {})
    one_i = facts_by_context.get("OneI", {})

    # Parse metadata
    metadata = _extract_metadata(one_d)

    # Parse P&L for current quarter
    quarter = _extract_financials(one_d, metadata)

    # Parse YTD cumulative
    ytd = _extract_financials(four_d, metadata)

    # Parse segment data
    segment_data = _extract_segments(facts_by_context)

    return {
        "quarter": quarter,
        "ytd": ytd,
        "metadata": metadata,
        "segment_data": segment_data,
        "raw_facts": raw_facts,
    }


def _extract_metadata(facts: Dict[str, str]) -> Dict[str, Any]:
    """Extract metadata fields from XBRL facts."""

    def parse_xbrl_date(s: str) -> Optional[date]:
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None

    return {
        "symbol": facts.get("Symbol", ""),
        "company_name": facts.get("NameOfTheCompany", ""),
        "scrip_code": facts.get("ScripCode", ""),
        "period_start": parse_xbrl_date(facts.get("DateOfStartOfReportingPeriod", "")),
        "period_end": parse_xbrl_date(facts.get("DateOfEndOfReportingPeriod", "")),
        "fy_start": parse_xbrl_date(facts.get("DateOfStartOfFinancialYear", "")),
        "fy_end": parse_xbrl_date(facts.get("DateOfEndOfFinancialYear", "")),
        "reporting_quarter": facts.get("ReportingQuarter", ""),
        "audited": facts.get("WhetherResultsAreAuditedOrUnaudited", ""),
        "consolidated": facts.get("NatureOfReportStandaloneConsolidated", ""),
        "rounding": facts.get("LevelOfRoundingUsedInFinancialStatements", ""),
        "multi_segment": facts.get("IsCompanyReportingMultisegmentOrSingleSegment", ""),
    }


def _extract_financials(facts: Dict[str, str], metadata: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Extract P&L line items from XBRL facts, converting to Crores."""
    result = {}

    # Determine the divisor based on rounding level
    rounding = metadata.get("rounding", "").lower()
    if "lakh" in rounding:
        divisor = 1e5
    elif "crore" in rounding:
        divisor = INR_TO_CRORES
    elif "million" in rounding:
        divisor = 1e6
    else:
        # Default: assume absolute INR → convert to Crores
        divisor = INR_TO_CRORES

    # P&L tags → fields (these are monetary, convert to Crores)
    for xbrl_tag, field_name in XBRL_PL_TAGS.items():
        raw = facts.get(xbrl_tag)
        if raw is not None:
            try:
                result[field_name] = round(float(raw) / divisor, 2)
            except (ValueError, TypeError):
                result[field_name] = None

    # EPS tags → fields (these are per-share, NOT converted)
    for xbrl_tag, field_name in XBRL_EPS_TAGS.items():
        raw = facts.get(xbrl_tag)
        if raw is not None:
            try:
                result[field_name] = round(float(raw), 2)
            except (ValueError, TypeError):
                result[field_name] = None

    # Use the combined EPS if available, else fall back to continuing ops
    if result.get("eps_basic") is None and result.get("eps_basic_continuing") is not None:
        result["eps_basic"] = result["eps_basic_continuing"]
    if result.get("eps_diluted") is None and result.get("eps_diluted_continuing") is not None:
        result["eps_diluted"] = result["eps_diluted_continuing"]
    result.pop("eps_basic_continuing", None)
    result.pop("eps_diluted_continuing", None)

    # Meta tags (face value is per-share, paid-up capital is monetary)
    for xbrl_tag, field_name in XBRL_META_TAGS.items():
        raw = facts.get(xbrl_tag)
        if raw is not None:
            try:
                val = float(raw)
                # Paid-up capital is monetary → convert to Crores
                if field_name == "paid_up_capital_cr":
                    val = round(val / divisor, 2)
                result[field_name] = val
            except (ValueError, TypeError):
                result[field_name] = None

    # Compute EBITDA = PBT + FinanceCosts + Depreciation - OtherIncome + ExceptionalItems
    pbt = result.get("pbt_cr")
    finance = result.get("finance_costs_cr")
    dep = result.get("depreciation_cr")
    other_inc = result.get("other_income_cr")
    exceptional = result.get("exceptional_items_cr")
    if pbt is not None and finance is not None and dep is not None:
        ebitda = pbt + finance + dep
        if other_inc is not None:
            ebitda -= other_inc
        if exceptional is not None:
            ebitda -= exceptional
        result["ebitda_cr"] = round(ebitda, 2)
        rev = result.get("revenue_cr")
        if rev and rev > 0:
            result["ebitda_margin_pct"] = round((ebitda / rev) * 100, 2)

    return result


def _extract_segments(facts_by_context: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """Extract segment-wise revenue and profit data."""
    segments = []

    # Segment contexts follow pattern: OneReportableSegmentRevenue{NN}D
    for ctx_id, facts in facts_by_context.items():
        if not ctx_id.startswith("OneReportableSegment"):
            continue

        desc = facts.get("DescriptionOfReportableSegment")
        revenue = facts.get("SegmentRevenue")
        profit = facts.get("SegmentProfitLossBeforeTaxAndFinanceCosts")

        if desc:
            seg = {"name": desc}
            if revenue:
                try:
                    seg["revenue_cr"] = round(float(revenue) / INR_TO_CRORES, 2)
                except (ValueError, TypeError):
                    pass
            if profit:
                try:
                    seg["profit_cr"] = round(float(profit) / INR_TO_CRORES, 2)
                except (ValueError, TypeError):
                    pass
            # Only add if we have at least a name
            if seg.get("revenue_cr") or seg.get("profit_cr"):
                segments.append(seg)

    return segments


# ── High-level orchestration helpers ─────────────────────────────────────────

async def create_nse_client() -> httpx.AsyncClient:
    """Create an httpx client with NSE session cookies."""
    client = httpx.AsyncClient(
        timeout=30,
        headers=NSE_HEADERS,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
    )
    # Warm up session cookie
    await refresh_nse_cookies(client)
    return client


async def refresh_nse_cookies(client: httpx.AsyncClient) -> None:
    """Hit NSE homepage to get fresh session cookies."""
    try:
        resp = await client.get(NSE_BASE)
        logger.debug(f"NSE cookie refresh: HTTP {resp.status_code}")
    except Exception as e:
        logger.debug(f"NSE cookie refresh failed: {e}")


async def fetch_and_parse_xbrl(
    filing: Dict[str, Any],
    client: httpx.AsyncClient,
    limiter: RateLimiter,
) -> Optional[Dict[str, Any]]:
    """Download and parse a single XBRL filing. Returns parsed data or None."""
    url = filing.get("xbrl_url")
    if not url:
        return None

    xml_bytes = await download_xbrl(url, client, limiter)
    if not xml_bytes:
        return None

    try:
        parsed = parse_xbrl(xml_bytes)
        return parsed
    except ET.ParseError as e:
        logger.warning(f"XBRL parse error for {filing.get('symbol')}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected XBRL parse error for {filing.get('symbol')}: {e}")
        return None
