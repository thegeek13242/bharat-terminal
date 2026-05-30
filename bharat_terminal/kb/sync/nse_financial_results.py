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
from typing import List, Dict, Optional, Any

import httpx

from bharat_terminal.kb.sync.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

NSE_BASE = "https://www.nseindia.com"
# Legacy endpoint (frozen at the Dec-2024 quarter since NSE migrated to Integrated Filing).
NSE_API_LEGACY = f"{NSE_BASE}/api/corporates-financial-results"
# Current endpoint: SEBI "Integrated Filing - Financials" (iXBRL/XBRL, taxonomy in-capmkt 2026-01-31).
NSE_API = f"{NSE_BASE}/api/integrated-filing-results"
# Exact `type` string the endpoint expects (note the space after the hyphen).
NSE_INTEGRATED_TYPE = "Integrated Filing- Financials"
NSE_INTEGRATED_REFERER = (
    f"{NSE_BASE}/companies-listing/corporate-integrated-filing"
    "?integratedType=integratedfilingfinancials"
)
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_INTEGRATED_REFERER,
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
    period: str = "Quarterly",  # retained for signature compatibility; unused by the new endpoint
) -> List[Dict[str, Any]]:
    """
    Fetch the list of Integrated-Filing (Financials) records from NSE for a symbol.

    The endpoint is paginated ({"data": [...], "size": N, "page": P, "totalCount": T}).
    A single symbol typically returns < 20 records (a handful of recent quarters,
    Consolidated + Standalone), so one page is usually enough; we page through
    defensively in case a symbol has more.
    """
    async def _get_page(page: int) -> Optional[Dict[str, Any]]:
        params = {
            "index": "equities",
            "type": NSE_INTEGRATED_TYPE,
            "symbol": symbol,
            "page": page,
        }
        resp = await client.get(NSE_API, params=params)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type and "html" in content_type:
            # NSE returned HTML instead of JSON — cookie expired. Refresh + retry once.
            logger.warning(f"NSE returned HTML for {symbol} (cookie expired?)")
            await refresh_nse_cookies(client)
            resp = await client.get(NSE_API, params=params)
            resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            logger.warning(f"Unexpected response type for {symbol}: {type(body)}")
            return None
        return body

    async with limiter:
        try:
            records: List[Dict[str, Any]] = []
            page = 1
            first = await _get_page(page)
            if first is None:
                return []
            total = int(first.get("totalCount") or 0)
            records.extend(first.get("data", []) or [])
            # Page through any remainder (rare for a single symbol).
            while len(records) < total and (first.get("data")):
                page += 1
                nxt = await _get_page(page)
                if not nxt or not nxt.get("data"):
                    break
                records.extend(nxt["data"])
            return records
        except httpx.HTTPStatusError as e:
            logger.warning(f"NSE filing index failed for {symbol}: HTTP {e.response.status_code}")
            return []
        except Exception as e:
            logger.warning(f"NSE filing index failed for {symbol}: {type(e).__name__}: {e}")
            return []


def parse_filing_metadata(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse a single Integrated-Filing record from the NSE API into our schema.

    The Integrated-Filing feed only reports the quarter-end (`qe_Date`); the period
    *start* is not provided, so we derive it from the quarter-end (Indian quarters end
    Mar/Jun/Sep/Dec). The XBRL itself carries the authoritative period_start, and
    `_upsert_financial_result` prefers that — this derived value only seeds the
    nse_filings row and its uniqueness key (symbol, from_date, to_date, consolidated).
    """
    # Prefer the plain XBRL XML; fall back to nothing (we don't parse iXBRL HTML here).
    xbrl_url = (raw.get("xbrl") or "").strip()
    has_xbrl = bool(xbrl_url) and not xbrl_url.endswith("/-") and xbrl_url.lower().endswith(".xml")
    ixbrl_url = (raw.get("ixbrl") or "").strip() or None

    def parse_nse_date(date_str: str) -> Optional[date]:
        if not date_str:
            return None
        # qe_Date arrives uppercase ("31-MAR-2026"); .title() → "31-Mar-2026" for %b.
        s = date_str.strip().title()
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%d-%B-%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    def parse_nse_datetime(dt_str: str) -> Optional[datetime]:
        if not dt_str:
            return None
        s = dt_str.strip().title()
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%m-%Y %H:%M"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    def derive_quarter_start(qe: Optional[date]) -> Optional[date]:
        """First day of the quarter whose last day is `qe` (Mar/Jun/Sep/Dec ends)."""
        if not qe:
            return None
        if qe.month in (3, 6, 9, 12):
            return date(qe.year, qe.month - 2, 1)
        # Non-standard quarter-end: fall back to ~3 months back, day 1.
        m = qe.month - 2
        y = qe.year
        if m <= 0:
            m += 12
            y -= 1
        return date(y, m, 1)

    def derive_financial_year(qe: Optional[date]) -> Optional[str]:
        if not qe:
            return None
        fy_start = qe.year if qe.month > 3 else qe.year - 1
        return f"01-Apr-{fy_start} To 31-Mar-{fy_start + 1}"

    to_date = parse_nse_date(raw.get("qe_Date", ""))
    broadcast = parse_nse_datetime(raw.get("broadcast_Date", ""))
    creation = parse_nse_datetime(raw.get("creation_Date", ""))

    return {
        "symbol": (raw.get("symbol") or "").strip(),
        "isin": raw.get("isin"),  # not present at index level; XBRL carries ISIN
        "from_date": derive_quarter_start(to_date),
        "to_date": to_date,
        "period": "Quarterly",
        "relating_to": raw.get("type_Sub"),  # "Original" / revision marker
        "financial_year": derive_financial_year(to_date),
        "consolidated": (raw.get("consolidated") or "").strip(),  # "Consolidated"/"Standalone"
        "audited": (raw.get("audited") or "").strip(),            # "Audited"/"Un-Audited"
        "ind_as": "Ind-AS Integrated",
        "format": "Integrated",
        "xbrl_url": xbrl_url if has_xbrl else None,
        "ixbrl_url": ixbrl_url,
        "detail_url": ixbrl_url,
        "seq_number": raw.get("seq_Id"),
        "filing_date": broadcast or creation,
        "broadcast_date": broadcast,
    }


def filter_consolidated_quarterly(filings: List[Dict]) -> List[Dict]:
    """
    Keep one filing per quarter: prefer Consolidated over Standalone, and within the
    chosen nature prefer the latest revision (the filing with the most recent
    broadcast date — revisions are re-broadcast after the Original). Only filings
    that carry a parseable XBRL XML are considered.
    """
    quarterly = [
        f for f in filings
        if f.get("period") == "Quarterly" and f.get("to_date") and f.get("xbrl_url")
    ]

    def latest(group: List[Dict]) -> Dict:
        # Most recent broadcast wins (Original < Revised). None dates sort last.
        return max(
            group,
            key=lambda f: f.get("broadcast_date") or datetime.min.replace(tzinfo=timezone.utc),
        )

    by_period: Dict[Any, List[Dict]] = {}
    for f in quarterly:
        by_period.setdefault(f["to_date"], []).append(f)

    result = []
    for _, group in sorted(by_period.items(), reverse=True):
        consolidated = [f for f in group if f["consolidated"] == "Consolidated"]
        if consolidated:
            result.append(latest(consolidated))
        else:
            result.append(latest(group))  # fall back to Standalone (latest revision)

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
    # OneI (instant) facts retained in original parser; not currently consumed
    _one_i = facts_by_context.get("OneI", {})  # noqa: F841

    # Parse metadata
    metadata = _extract_metadata(one_d)

    # Parse P&L for current quarter
    quarter = _extract_financials(one_d, metadata)

    # Parse YTD cumulative
    ytd = _extract_financials(four_d, metadata)

    # Parse segment data
    segment_data = _extract_segments(facts_by_context)

    # Units sanity: monetary facts are absolute INR (÷1e7 = crores), regardless of the
    # taxonomy's display `LevelOfRounding` (e.g. "Lakhs"), which we intentionally ignore.
    # Cross-check scale independently via implied EPS = PAT / (paid-up capital / face value).
    scale_ok = _check_monetary_scale(one_d, quarter, metadata)
    metadata["scale_ok"] = scale_ok

    return {
        "quarter": quarter,
        "ytd": ytd,
        "metadata": metadata,
        "segment_data": segment_data,
        "raw_facts": raw_facts,
    }


def _check_monetary_scale(
    raw_one_d: Dict[str, str],
    quarter: Dict[str, Optional[float]],
    metadata: Dict[str, Any],
) -> bool:
    """
    Best-effort order-of-magnitude check on monetary scaling.

    Compares the PAT-implied EPS (PAT ÷ shares) against the reported EPS and flags a
    >100x divergence — the signature of a per-field scale corruption (e.g. a garbage
    PAT). Returns True (no objection) by default; only returns False when ALL inputs
    are themselves plausible AND still disagree wildly.

    Why the strict input gating: some filings carry garbage in the very fields this
    check relies on — e.g. FaceValueOfEquityShareCapital = 5441.16, or
    PaidUpValueOfEquityShareCapital = 2.09e13 — which would make the derived share
    count meaningless and produce false positives on rows whose P&L is actually fine.
    Indian face values are ₹1/2/5/10 (rarely up to ₹100), and no listed company has
    more than ~1.5e10 shares, so we only trust the check inside those bounds.
    A True result is NOT a precision guarantee; it means "no gross scale error seen."
    """
    try:
        pat_cr = quarter.get("pat_cr")
        eps_reported = quarter.get("eps_basic")
        paid_up = raw_one_d.get("PaidUpValueOfEquityShareCapital")
        face_value = raw_one_d.get("FaceValueOfEquityShareCapital")
        if pat_cr is None or not eps_reported or not paid_up or not face_value:
            return True  # not enough data to check — don't block
        fv = float(face_value)
        shares = float(paid_up) / fv
        # Gate on plausibility of the share-count inputs; if implausible, we can't judge.
        if not (1.0 <= fv <= 1000.0):
            return True
        if not (1e3 <= shares <= 2e10):
            return True
        eps_implied = (pat_cr * INR_TO_CRORES) / shares
        ratio = abs(eps_implied / eps_reported)
        if ratio > 100 or ratio < (1 / 100):
            logger.error(
                "XBRL scale check FAILED for %s %s: implied EPS %.4f vs reported %.4f "
                "(ratio %.1fx, sane inputs) — possible units regression; "
                "row flagged scale_ok=False",
                metadata.get("symbol"), metadata.get("period_end"),
                eps_implied, eps_reported, ratio,
            )
            return False
        return True
    except (ValueError, TypeError, ZeroDivisionError):
        return True  # don't block on a malformed check


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
    """
    Extract current-quarter segment revenue and profit.

    In the Integrated-Filing taxonomy, segment facts are spread across paired
    duration contexts linked by `DescriptionOfReportableSegment`:
      - OneReportable{N}D         → SegmentRevenue (+ SegmentRevenueFromOperations)
      - OneReportableFinance{N}D  → SegmentProfitLossBeforeTaxAndFinanceCosts
    (Instant contexts OneReportable3*/4* carry segment assets/liabilities — ignored.)
    We merge revenue and profit by segment name. Only current-quarter ("One*")
    duration contexts are used; YTD ("Four*") segments are skipped to match the
    quarterly P&L row.
    """
    def to_cr(v: Optional[str]) -> Optional[float]:
        if not v:
            return None
        try:
            return round(float(v) / INR_TO_CRORES, 2)
        except (ValueError, TypeError):
            return None

    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for ctx_id, facts in facts_by_context.items():
        if not (ctx_id.startswith("OneReportable") and ctx_id.endswith("D")):
            continue
        name = facts.get("DescriptionOfReportableSegment")
        if not name:
            continue
        revenue = to_cr(facts.get("SegmentRevenue") or facts.get("SegmentRevenueFromOperations"))
        profit = to_cr(facts.get("SegmentProfitLossBeforeTaxAndFinanceCosts"))
        if revenue is None and profit is None:
            continue
        if name not in merged:
            merged[name] = {"name": name}
            order.append(name)
        if revenue is not None:
            merged[name]["revenue_cr"] = revenue
        if profit is not None:
            merged[name]["profit_cr"] = profit

    return [merged[n] for n in order]


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
    """Hit the Integrated-Filing listing page to get fresh session cookies."""
    try:
        resp = await client.get(NSE_INTEGRATED_REFERER)
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
