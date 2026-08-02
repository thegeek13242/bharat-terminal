"""Canonical join keys for guidance statements (C2 v1).

WHY: transcript_guidance.metric is free text — 1,351 distinct labels over
2,589 rows (42% singletons), units spelled five ways ('crore'/'crores'/
'Crores'/'INR crores'), periods as 'FY27'/'FY 27'/'Q1 FY27'. Without
canonical keys the ledger cannot chain a company's guidance across quarters
(UJJIVANSFB renamed 'advances growth'→'asset growth' between calls) and
verdict computation silently degrades on spelling (GRAVITA 'maintained'
scored as 'changed' over 'lakh metric per annum' vs 'lakh metric tons per
annum').

Pure functions, no I/O, deterministic — unit-tested via `python -m
bharat_terminal.kb.sync.guidance_normalize` (asserts, exit 0 = pass).

metric_key families: the ~20 highest-volume families get named keys; the
long tail falls back to a slug of the raw label, which still chains
same-company same-wording statements. `formula` is non-None only for
class-a metrics scoreable against financial_results with exact arithmetic
(recon 2026-07-31: ~25% of rows) — the outcomes scorer refuses everything
else rather than guess.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime

# (pattern, metric_key, better_direction, formula) — first match wins, so
# growth/margin variants must precede their bare-metric parents.
_METRIC_RULES: list[tuple[str, str, str | None, str | None]] = [
    (r"revenue\s+growth|sales\s+growth|top\s*line\s+growth|topline\s+growth",
     "revenue_growth", "higher", "revenue_growth_pct"),
    (r"revenue\s+cagr|sales\s+cagr", "revenue_cagr", "higher", None),
    (r"revenue|sales|top\s*line|topline|turnover",
     "revenue", "higher", "revenue_cr"),
    (r"ebitda\s+margin\s+expansion|margin\s+expansion",
     "margin_expansion", "higher", None),
    (r"ebitda\s+margin", "ebitda_margin", "higher", "ebitda_margin_pct"),
    (r"ebitda\s+growth", "ebitda_growth", "higher", "ebitda_growth_pct"),
    (r"ebitda", "ebitda", "higher", "ebitda_cr"),
    (r"(pat|net\s+profit|net)\s+margin", "pat_margin", "higher",
     "pat_margin_pct"),
    (r"(pat|net\s+profit|profit)\s+growth", "pat_growth", "higher",
     "pat_growth_pct"),
    (r"\bpat\b|net\s+profit|profit\s+after\s+tax", "pat", "higher", "pat_cr"),
    (r"gross\s+margin", "gross_margin", "higher", None),
    (r"operating\s+margin|ebit\s+margin", "operating_margin", "higher", None),
    (r"\beps\b|earnings\s+per\s+share", "eps", "higher", "eps"),
    (r"(effective\s+)?tax\s+rate", "tax_rate", "lower", "tax_rate_pct"),
    (r"capex|capital\s+expenditure", "capex", None, None),
    (r"\bnim\b|net\s+interest\s+margin", "nim", "higher", None),
    (r"\broa\b|return\s+on\s+assets", "roa", "higher", None),
    (r"\broe\b|return\s+on\s+equity", "roe", "higher", None),
    (r"credit\s+cost", "credit_cost", "lower", None),
    (r"(credit|loan(\s+book)?|advances|asset)\s+growth",
     "loan_growth", "higher", None),
    (r"deposit\s+growth", "deposit_growth", "higher", None),
    (r"\baum\b", "aum", "higher", None),
    (r"order\s+(book|inflow|intake|pipeline)", "order_book", "higher", None),
    (r"volume\s+growth", "volume_growth", "higher", None),
    (r"volumes?\b", "volume", "higher", None),
    (r"capacity|utili[sz]ation", "capacity", None, None),
    (r"occupancy", "occupancy", "higher", None),
    (r"cost.to.income", "cost_to_income", "lower", None),
    (r"(net\s+)?debt", "debt", "lower", None),
]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def canonical_metric(raw: str) -> tuple[str, str | None, str | None]:
    """(metric_key, better_direction, formula). Fallback: slug of the label."""
    label = (raw or "").strip().lower()
    for pattern, key, direction, formula in _METRIC_RULES:
        if re.search(pattern, label):
            return key, direction, formula
    slug = _SLUG_RE.sub("_", label).strip("_") or "unknown"
    return slug[:64], None, None


_UNIT_MAP = [
    (r"basis\s*points?|bps", "bps"),
    (r"%|percent|pct", "pct"),
    (r"crores?|\bcr\b", "cr"),
    (r"lakhs?", "lakh"),
    (r"millions?|\bmn\b", "mn"),
    (r"billions?|\bbn\b", "bn"),
    (r"times|\bx\b", "x"),
]


def canonical_unit(raw: str | None) -> str | None:
    if not raw:
        return None
    label = raw.strip().lower()
    if re.search(r"usd|\$|dollar", label):
        return "usd_" + (canonical_unit(re.sub(r"usd|\$|dollars?", "", label))
                         or "amt")
    for pattern, key in _UNIT_MAP:
        if re.search(pattern, label):
            return key
    return _SLUG_RE.sub("_", label).strip("_")[:32] or None


def to_crore(value: float, unit_key: str | None) -> float | None:
    """INR amount → crore; None if not an INR amount unit."""
    return {"cr": value, "lakh": value / 100.0, "mn": value / 10.0,
            "bn": value * 100.0}.get(unit_key or "")


_FY_RE = re.compile(r"^\s*fy\s*'?\s*(\d{2,4})\s*$", re.I)
_Q_RE = re.compile(r"^\s*q([1-4])\s*[- ]?\s*fy\s*'?\s*(\d{2,4})\s*$", re.I)
_H_RE = re.compile(r"^\s*h([12])\s*[- ]?\s*fy\s*'?\s*(\d{2,4})\s*$", re.I)
_CY_RE = re.compile(r"^\s*cy\s*'?\s*(\d{2,4})\s*$", re.I)
_MONTH_RE = re.compile(
    r"^\s*(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(\d{4})\s*$", re.I)


def _fy_year(two_or_four: str) -> int:
    y = int(two_or_four)
    return y if y > 1900 else 2000 + y


def canonical_period(raw: str | None) -> tuple[str | None, date | None]:
    """(period_key, period_end). Indian FY: FY27 ends 2027-03-31; Q1FY27 =
    Apr-Jun 2026. Unparseable → (slug, None); NULL stays (None, None)."""
    if not raw or not raw.strip():
        return None, None
    s = raw.strip()
    m = _FY_RE.match(s)
    if m:
        y = _fy_year(m.group(1))
        return f"FY{y % 100:02d}", date(y, 3, 31)
    m = _Q_RE.match(s)
    if m:
        q, y = int(m.group(1)), _fy_year(m.group(2))
        ends = {1: date(y - 1, 6, 30), 2: date(y - 1, 9, 30),
                3: date(y - 1, 12, 31), 4: date(y, 3, 31)}
        return f"Q{q}FY{y % 100:02d}", ends[q]
    m = _H_RE.match(s)
    if m:
        h, y = int(m.group(1)), _fy_year(m.group(2))
        return (f"H{h}FY{y % 100:02d}",
                date(y - 1, 9, 30) if h == 1 else date(y, 3, 31))
    m = _CY_RE.match(s)
    if m:
        y = _fy_year(m.group(1))
        return f"CY{y % 100:02d}", date(y, 12, 31)
    m = _MONTH_RE.match(s)
    if m:
        mo = datetime.strptime(m.group(1)[:3], "%b").month
        y = int(m.group(2))
        return f"{y}-{mo:02d}", date(y, mo, calendar.monthrange(y, mo)[1])
    return _SLUG_RE.sub("_", s.lower()).strip("_")[:32] or None, None


# Formula → expected canonical unit of the GUIDED value; the scorer refuses
# unit mismatches instead of guessing (HCLTECH constant-currency lesson).
FORMULA_UNIT = {
    "revenue_cr": "cr", "pat_cr": "cr", "ebitda_cr": "cr",
    "revenue_growth_pct": "pct", "pat_growth_pct": "pct",
    "ebitda_growth_pct": "pct", "ebitda_margin_pct": "pct",
    "pat_margin_pct": "pct", "tax_rate_pct": "pct", "eps": None,
}


def normalize_row(metric_raw: str, period_raw: str | None,
                  unit_raw: str | None, stated_at) -> dict:
    """Full canonicalization of one transcript_guidance row."""
    metric_key, direction, formula = canonical_metric(metric_raw)
    unit_key = canonical_unit(unit_raw)
    period_key, period_end = canonical_period(period_raw)
    flags = []
    if formula:
        want = FORMULA_UNIT.get(formula)
        if want == "cr" and unit_key in ("lakh", "mn", "bn"):
            flags.append(f"unit_converted_{unit_key}")
        elif want is not None and unit_key != want and unit_key is not None:
            formula = None
            flags.append("unit_mismatch")
        elif unit_key is None and want is not None:
            formula = None
            flags.append("unit_missing")
    if unit_key and unit_key.startswith("usd_"):
        formula = None
        flags.append("non_inr_basis")
    stated_date = stated_at.date() if hasattr(stated_at, "date") else stated_at
    if period_end and stated_date and period_end < stated_date:
        # 'Guidance' about an already-elapsed period is a misparse of a
        # historical reference (TIMETECHNO FY'17 rows) — never scoreable.
        formula = None
        flags.append("past_period")
    return {"metric_key": metric_key, "better_direction": direction,
            "formula": formula, "unit_key": unit_key,
            "period_key": period_key, "period_end": period_end,
            "flags": flags, "scoreable": formula is not None}


if __name__ == "__main__":
    d = date(2026, 6, 3)
    assert canonical_metric("Revenue growth")[0] == "revenue_growth"
    assert canonical_metric("consolidated revenue")[:1] == ("revenue",)
    assert canonical_metric("EBITDA Margin") == (
        "ebitda_margin", "higher", "ebitda_margin_pct")
    assert canonical_metric("advances growth")[0] == "loan_growth"
    assert canonical_metric("asset growth")[0] == "loan_growth"  # UJJIVANSFB
    assert canonical_metric("effective tax rate")[1] == "lower"
    assert canonical_metric("store additions in core states")[2] is None
    assert canonical_unit("Crores") == canonical_unit("INR crores") == "cr"
    assert canonical_unit("basis points") == "bps"
    assert canonical_unit("USD million") == "usd_mn"
    assert to_crore(500, "mn") == 50.0 and to_crore(1.5, "bn") == 150.0
    assert canonical_period("FY27") == ("FY27", date(2027, 3, 31))
    assert canonical_period("FY 27") == ("FY27", date(2027, 3, 31))
    assert canonical_period("Q1 FY27") == ("Q1FY27", date(2026, 6, 30))
    assert canonical_period("Q4FY27") == ("Q4FY27", date(2027, 3, 31))
    assert canonical_period("H2 FY27") == ("H2FY27", date(2027, 3, 31))
    assert canonical_period("June 2027") == ("2027-06", date(2027, 6, 30))
    assert canonical_period("medium-term") == ("medium_term", None)
    assert canonical_period(None) == (None, None)
    r = normalize_row("Revenue", "FY17", "crores", d)     # TIMETECHNO junk
    assert not r["scoreable"] and "past_period" in r["flags"]
    r = normalize_row("revenue growth", "FY27", "crores", d)
    assert not r["scoreable"] and "unit_mismatch" in r["flags"]
    r = normalize_row("EBITDA margin", "FY27", "%", d)
    assert r["scoreable"] and r["period_end"] == date(2027, 3, 31)
    print("guidance_normalize: all asserts pass")
