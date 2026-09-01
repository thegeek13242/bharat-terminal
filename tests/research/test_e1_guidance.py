"""E1 guidance estimator — the conversions and the scope filter.

Stage 4B (2026-09-01) found the ledger's metric_key='revenue' bucket
conflates company-level, SEGMENT, INCREMENTAL and multi-year ASPIRATIONAL
guidance. Using a segment figure as a company expectation is a silent ~6x
error (measured median guided/actual 0.19), so ambiguity must resolve to
"reject" rather than "guess".
"""
import pandas as pd
import pytest

from bharat_terminal.research.e1_composite import (fy_label, guidance_expectation,
                                                   horizon_ok, is_company_level)


# ── fiscal-year labelling (Indian FY ends 31 March) ──────────────────────

@pytest.mark.parametrize("period_end,expected", [
    ("2026-06-30", "FY27"),   # Q1 of FY27
    ("2026-03-31", "FY26"),   # Q4 of FY26 — the boundary that bites
    ("2026-12-31", "FY27"),
    ("2027-03-31", "FY27"),
])
def test_fy_label(period_end, expected):
    assert fy_label(period_end) == expected


# ── scope filter (verified against live metric_raw values) ───────────────

def test_segment_guidance_rejected():
    for raw in ("EPC business revenue", "paint business revenue",
                "new business revenue", "export revenue",
                "Total incremental revenue from capacity expansions"):
        assert is_company_level(raw) is False, raw


def test_company_level_guidance_accepted():
    for raw in ("revenue", "", "Revenue (Standalone)", "total revenue",
                "consolidated revenue"):
        assert is_company_level(raw) is True, raw


def test_total_qualifier_overrides_segment_word():
    # "total company business" must not be rejected for containing "business"
    assert is_company_level("total company business revenue") is True


def test_horizon_rejects_multi_year_aspirations():
    # VSTTILLERS guiding FY29 says nothing about an FY27 quarter.
    assert horizon_ok("FY29", "FY27") is False
    assert horizon_ok("FY28", "FY27") is True     # next year is allowed
    assert horizon_ok("FY27", "FY27") is True
    assert horizon_ok(None, "FY27") is True       # undated: not our filter's job


# ── conversions ──────────────────────────────────────────────────────────

def _g(**kw):
    base = {"metric_key": "revenue_growth", "metric_raw": "revenue",
            "period_key": "FY27", "value_low": 15.0, "value_high": 15.0,
            "unit_key": "pct", "stated_at": pd.Timestamp("2026-05-01", tz="UTC")}
    base.update(kw)
    return pd.DataFrame([base])


def test_growth_pct_applied_to_year_ago_quarter():
    # The conversion measured at median guided/actual 1.02 in production.
    got = guidance_expectation(_g(), y4=100.0, trailing_year=400.0,
                               metric="revenue")
    assert got is not None
    assert got[0] == pytest.approx(115.0)
    assert got[3] == "revenue_growth_pct"


def test_growth_range_becomes_band():
    got = guidance_expectation(_g(value_low=10.0, value_high=20.0),
                               y4=100.0, trailing_year=400.0, metric="revenue")
    assert got[1] == pytest.approx(110.0) and got[2] == pytest.approx(120.0)


def test_annual_absolute_uses_seasonal_share():
    got = guidance_expectation(
        _g(metric_key="revenue", unit_key="cr", value_low=400.0,
           value_high=400.0),
        y4=100.0, trailing_year=400.0, metric="revenue")
    # share = 100/400 = 0.25 -> 400 * 0.25 = 100
    assert got[0] == pytest.approx(100.0) and got[3] == "revenue_annual"


def test_annual_absolute_refuses_without_seasonal_share():
    # Refusing beats guessing: a missing share silently mis-scales by ~4x.
    got = guidance_expectation(
        _g(metric_key="revenue", unit_key="cr", value_low=400.0,
           value_high=400.0),
        y4=100.0, trailing_year=None, metric="revenue")
    assert got is None


def test_margin_guidance_multiplies_expected_revenue():
    rows = pd.concat([_g(), _g(metric_key="ebitda_margin", value_low=20.0,
                             value_high=20.0)])
    got = guidance_expectation(rows, y4=100.0, trailing_year=400.0,
                               metric="ebitda")
    # revenue 115 (from +15% growth) * 20% margin = 23
    assert got[0] == pytest.approx(23.0)
    assert got[3] == "ebitda_margin_x_revenue"


def test_no_applicable_guidance_returns_none():
    assert guidance_expectation(pd.DataFrame(), 100.0, 400.0, "revenue") is None
