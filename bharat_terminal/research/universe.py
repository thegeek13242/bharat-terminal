"""
Universe seed: NIFTY 50 + BankNifty constituents (and the macro triggers we
calibrate against).

Constituent lists are point-in-time snapshots — refresh quarterly via NSE's
indices CSV (https://www.niftyindices.com). Storing as code rather than as
a one-off SQL seed because (a) it's small, (b) we want the agent runner to
be self-contained for fresh installs, (c) the snapshot date is meaningful
provenance.

Snapshot date: 2026-04-30 (post-March-2026 rebalance).
"""
from __future__ import annotations

# ── NIFTY 50 ──────────────────────────────────────────────────────────────
# Source: NSE Indices — Nifty 50 weights as of 2026-04-30 monthly factsheet.
NIFTY50: tuple[str, ...] = (
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "BHARTIARTL", "ITC", "LT", "AXISBANK", "KOTAKBANK",
    "SBIN", "HINDUNILVR", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "M&M", "SUNPHARMA", "TITAN", "ULTRACEMCO", "ONGC",
    "WIPRO", "NTPC", "POWERGRID", "TATAMOTORS", "HCLTECH",
    "JSWSTEEL", "COALINDIA", "BAJAJFINSV", "ADANIENT", "ADANIPORTS",
    "TATASTEEL", "GRASIM", "DRREDDY", "HINDALCO", "BPCL",
    "TECHM", "BRITANNIA", "CIPLA", "EICHERMOT", "NESTLEIND",
    "INDUSINDBK", "DIVISLAB", "TATACONSUM", "HEROMOTOCO", "APOLLOHOSP",
    "BAJAJ-AUTO", "UPL", "SBILIFE", "HDFCLIFE", "LTIM",
)

# ── BANKNIFTY ─────────────────────────────────────────────────────────────
# Mostly subset of NIFTY 50 banks, plus a few non-Nifty names like AU, IDFCFIRST.
BANKNIFTY: tuple[str, ...] = (
    "HDFCBANK", "ICICIBANK", "AXISBANK", "KOTAKBANK", "SBIN",
    "INDUSINDBK", "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB",
    "AUBANK", "BANDHANBNK",
)

# Combined unique universe — what the research agent runs over.
RESEARCH_UNIVERSE: tuple[str, ...] = tuple(
    sorted(set(NIFTY50) | set(BANKNIFTY))
)


# ── Triggers ──────────────────────────────────────────────────────────────
# The quantitative input variables we calibrate equity betas against.
# Each one is a row in the `triggers` table; later, regression jobs will fit
# (symbol, trigger) → beta from price data.

TRIGGERS: tuple[dict, ...] = (
    # Continuous — commodity prices (MCX-quoted, or international where India lacks a contract)
    {"id": "CRUDEOIL",    "label": "MCX Crude Oil (₹/bbl)",       "kind": "continuous", "unit": "INR/bbl",       "data_source": "MCX:CRUDEOIL"},
    {"id": "BRENT",       "label": "Brent Crude (USD/bbl)",        "kind": "continuous", "unit": "USD/bbl",       "data_source": "Yahoo:BZ=F"},
    {"id": "GOLD",        "label": "MCX Gold (₹/10g)",             "kind": "continuous", "unit": "INR/10g",       "data_source": "MCX:GOLD"},
    {"id": "SILVER",      "label": "MCX Silver (₹/kg)",            "kind": "continuous", "unit": "INR/kg",        "data_source": "MCX:SILVER"},
    {"id": "COPPER",      "label": "MCX Copper (₹/kg)",            "kind": "continuous", "unit": "INR/kg",        "data_source": "MCX:COPPER"},
    {"id": "ALUMINIUM",   "label": "MCX Aluminium (₹/kg)",         "kind": "continuous", "unit": "INR/kg",        "data_source": "MCX:ALUMINIUM"},
    {"id": "NATURALGAS",  "label": "MCX Natural Gas (₹/mmBtu)",    "kind": "continuous", "unit": "INR/mmBtu",     "data_source": "MCX:NATURALGAS"},

    # Continuous — FX, rates, vol
    {"id": "USDINR",      "label": "USD/INR",                       "kind": "continuous", "unit": "INR/USD",       "data_source": "Yahoo:INR=X"},
    {"id": "DXY",         "label": "Dollar Index",                  "kind": "continuous", "unit": "index",         "data_source": "Yahoo:DX-Y.NYB"},
    {"id": "US10Y",       "label": "US 10-Year Treasury Yield",     "kind": "continuous", "unit": "%",             "data_source": "Yahoo:^TNX"},
    {"id": "INDIA_VIX",   "label": "India VIX",                      "kind": "continuous", "unit": "index",         "data_source": "NSE:INDIAVIX"},
    {"id": "CBOE_VIX",    "label": "CBOE VIX",                       "kind": "continuous", "unit": "index",         "data_source": "Yahoo:^VIX"},
    {"id": "GIFT_NIFTY",  "label": "GIFT Nifty (premium/discount)",  "kind": "continuous", "unit": "index",         "data_source": "NSEIFSC:GIFTNIFTY"},

    # Discrete (calendar-driven, surprise vs. consensus)
    {"id": "FOMC_DECISION","label": "US Fed Rate Decision",          "kind": "discrete",   "unit": "bps surprise", "data_source": "CME:FedWatch"},
    {"id": "RBI_MPC",     "label": "RBI MPC Rate Decision",          "kind": "discrete",   "unit": "bps surprise", "data_source": "RBI"},
    {"id": "INDIA_CPI",   "label": "India CPI Print",                "kind": "discrete",   "unit": "%-pt surprise","data_source": "MOSPI"},
    {"id": "US_CPI",      "label": "US CPI Print",                   "kind": "discrete",   "unit": "%-pt surprise","data_source": "BLS"},
    {"id": "FII_FLOW",    "label": "FII Net Equity Flow",            "kind": "continuous", "unit": "INR Cr",       "data_source": "NSE:FII_DII"},
)
