"""
Seed data for macro_events / event_degrees / event_sector_edges /
event_transmissions, ported from the trading-strategy framework
(TRADING_STRATEGY_CONTEXT.md).

This is the STRUCTURAL / CAUSAL layer — hand-curated economics. Beta values
and leader rankings are NOT in here; those come from the research agent
populating `trigger_betas` and `sector_leaders` against this skeleton.

The events covered:
  crude_up        — Crude Oil Spike (>5%)
  fed_hike        — US Fed Hawkish Surprise
  rbi_hike        — RBI Rate Hike Surprise
  vix_spike       — Global Risk-Off (CBOE VIX > 30)
  china_stim      — China Economic Stimulus
  fii_surge       — Strong FII Inflow Wave
  monsoon_fail    — Monsoon Failure (>15% below LPA)
  budget_infra    — Union Budget Infra Push
  commodity_shock — Continuous-trigger price shock (used by commodity_engine)
"""
from __future__ import annotations


# Each row of `event_sector_edges`:
#   (event_id, degree, sector, direction, magnitude_prior, lag_text,
#    lag_min_days, lag_max_days, mechanism)
EVENT_SECTOR_EDGES: list[tuple] = [
    # ── crude_up ──────────────────────────────────────────────────────────
    ("crude_up", 1, "Oil Marketing Cos",  "bear", 5, "Intraday", 0, 1,
     "Marketing margin squeezed as crude rises before retail price revision. Under-recovery risk builds instantly."),
    ("crude_up", 1, "Aviation",            "bear", 5, "Intraday", 0, 1,
     "ATF is 35-40% of operating costs. Every $10/bbl ≈ ₹800cr annual loss for a major LCC."),
    ("crude_up", 1, "Upstream E&P",        "bull", 5, "Intraday", 0, 1,
     "Revenue directly indexed to crude. Every $1/bbl rise ≈ ₹500cr EBITDA uplift for ONGC-scale producer."),
    ("crude_up", 1, "Paints",              "bear", 4, "T+1-3",   1, 3,
     "TiO2, solvents, resins — 60-65% of raw material basket is petroleum-linked."),
    ("crude_up", 1, "Tyres",               "bear", 4, "T+1",     1, 3,
     "Synthetic rubber (SBR, PBR) is crude-derived. ~60% of raw material cost."),
    ("crude_up", 2, "Banks & NBFCs",       "bear", 3, "T+7-21",  7, 21,
     "Rate-hike cycle begins. NIMs compress on floating-rate books in the near term."),
    ("crude_up", 2, "IT Services",         "bull", 2, "T+1-3",   1, 3,
     "INR depreciation expands USD revenue in rupee terms. 1% INR fall ≈ 0.4% EBIT margin tailwind."),
    ("crude_up", 3, "Two-Wheelers",        "bear", 3, "T+30-60", 30, 60,
     "Retail petrol hike hits rural mobility cost. Volume de-growth 6-10% expected."),
    ("crude_up", 3, "FMCG (rural-facing)", "bear", 2, "T+14-45", 14, 45,
     "Rural disposable income shrinks. Downtrading from premium to value. Volume growth slows 2-4%."),

    # ── fed_hike ──────────────────────────────────────────────────────────
    ("fed_hike", 1, "Banks (high-FII)",   "bear", 4, "Intraday", 0, 5,
     "FII forced selling concentrated in high-FII-ownership banks; 1.18x BankNifty beta names fall hardest."),
    ("fed_hike", 1, "NBFCs (wholesale-funded)", "bear", 4, "Intraday", 0, 5,
     "Wholesale liabilities reprice immediately on rate move; NIM impact -25 to -40bps in Q1."),
    ("fed_hike", 1, "IT Services",         "bull", 3, "Intraday", 0, 3,
     "INR sells off → USD-revenue translation tailwind; defensive positioning."),
    ("fed_hike", 2, "Banks (high-CASA)",   "bull", 3, "T+14-90", 14, 90,
     "Asset-sensitive book reprices upward over 2-3 quarters as floating loans roll; NIM expansion +12 to +18bps Q2/Q3."),
    ("fed_hike", 2, "Pharma",              "bull", 2, "T+5-30",  5, 30,
     "Defensive rotation; INR weakness benefits US-revenue exposure."),
    ("fed_hike", 3, "Real Estate",         "bear", 3, "T+30-90", 30, 90,
     "Mortgage rate rise compresses affordability and pre-sales pipeline."),

    # ── rbi_hike ──────────────────────────────────────────────────────────
    ("rbi_hike", 1, "NBFCs",               "bear", 4, "Intraday", 0, 5,
     "MCLR/EBLR-linked liabilities reprice fast, asset book lags."),
    ("rbi_hike", 1, "Auto Finance",        "bear", 3, "T+1-7",   1, 7,
     "Loan EMIs rise, demand at retail dampens within weeks."),
    ("rbi_hike", 2, "Banks",               "bull", 2, "T+30-90", 30, 90,
     "Eventual NIM expansion as floating loans reprice; offset by demand drag."),
    ("rbi_hike", 3, "Real Estate",         "bear", 3, "T+30-90", 30, 90,
     "Affordability hit; new launches slow."),

    # ── vix_spike (Global Risk-Off, CBOE VIX > 30) ────────────────────────
    ("vix_spike", 1, "Mid/Small Caps",     "bear", 5, "Intraday", 0, 3,
     "Liquidity-driven names sold first; FII redemption pressure cascades."),
    ("vix_spike", 1, "BankNifty",          "bear", 4, "Intraday", 0, 3,
     "Highest-beta sector to global risk; FII selling concentrated here."),
    ("vix_spike", 1, "Metals",             "bear", 4, "Intraday", 0, 3,
     "Cyclicals priced off global growth expectations."),
    ("vix_spike", 1, "IT Services",        "bull", 2, "Intraday", 0, 3,
     "Defensive USD-revenue book; INR weakness adds tailwind."),
    ("vix_spike", 1, "FMCG / Pharma",      "bull", 2, "Intraday", 0, 3,
     "Classic defensive rotation."),
    ("vix_spike", 2, "Gold-loan NBFCs",    "bull", 3, "T+1-14",  1, 14,
     "Risk-off bid for gold lifts collateral values; AUM tailwind."),

    # ── china_stim ────────────────────────────────────────────────────────
    ("china_stim", 1, "Metals",            "bull", 5, "Intraday", 0, 5,
     "Chinese demand reflation flows directly into LME prices; Indian metal producers re-rate."),
    ("china_stim", 1, "Speciality Chem",   "bull", 3, "T+1-7",   1, 7,
     "Restocking cycle starts; pricing tailwind for India-export plays."),
    ("china_stim", 2, "FII Allocations to India", "bear", 2, "T+7-30", 7, 30,
     "FII rotation INTO China risk pulls passive flows out of India broad market."),

    # ── fii_surge ─────────────────────────────────────────────────────────
    ("fii_surge", 1, "BankNifty",          "bull", 4, "Intraday", 0, 5,
     "FII flows concentrate in liquid large-cap banks first."),
    ("fii_surge", 1, "Nifty Index",        "bull", 3, "Intraday", 0, 5,
     "Passive ETF inflows lift the index broadly."),
    ("fii_surge", 2, "Mid Caps",           "bull", 3, "T+5-21",  5, 21,
     "Once large-cap is bought, flows trickle to mid-cap with lag."),

    # ── monsoon_fail ──────────────────────────────────────────────────────
    ("monsoon_fail", 1, "Two-Wheelers",   "bear", 4, "T+30-60", 30, 60,
     "Rural income compression hits 60%-rural OEMs hardest."),
    ("monsoon_fail", 1, "Agrochemicals",   "bear", 3, "T+30-60", 30, 60,
     "Acreage and application rates fall."),
    ("monsoon_fail", 1, "Rural-finance NBFCs", "bear", 4, "T+30-90", 30, 90,
     "Credit costs rise as rural cash-flows weaken."),
    ("monsoon_fail", 2, "FMCG (rural)",    "bear", 3, "T+45-90", 45, 90,
     "Rural disposable-income compression compounds; volume growth slows."),
    ("monsoon_fail", 2, "Water Infra",     "bull", 2, "T+30-180",30, 180,
     "Policy response — water-management capex gets accelerated."),

    # ── budget_infra ──────────────────────────────────────────────────────
    ("budget_infra", 1, "Capital Goods",  "bull", 5, "Intraday", 0, 7,
     "Direct order-book uplift; backlog visibility extends."),
    ("budget_infra", 1, "Defence",         "bull", 4, "Intraday", 0, 7,
     "Allocation increase signals multi-year orders."),
    ("budget_infra", 1, "Railways",        "bull", 4, "Intraday", 0, 7,
     "Capex push directly benefits rail equipment vendors."),
    ("budget_infra", 1, "Cement",          "bull", 3, "T+1-30",  1, 30,
     "Volume tailwind from infra construction cycle."),
    ("budget_infra", 2, "Steel / Construction Materials", "bull", 3, "T+30-90", 30, 90,
     "Second-order demand pull from infra projects ramping."),

    # ── commodity_shock — NO sector edges on purpose.
    # The commodity engine + Polymarket bridge resolve exposures by trigger_id
    # against per-company `trigger_betas`; the calibrated beta IS the filter
    # (only structurally-exposed companies get one). Seeding generic sector
    # labels here ("Direct cost-exposed sectors", …) would NOT match any real
    # companies.sector_nse value (which is ~68% NULL and uses NSE sector strings),
    # so resolve_exposures would silently drop every symbol. Leaving this event
    # edge-less makes resolve_exposures pass betas through unscoped — verified.
]


# (event_id, label, trigger_id, trigger_spec, propagation_chain, notes)
MACRO_EVENTS: list[tuple] = [
    ("crude_up",         "Crude Oil Spike (>5%)",         "BRENT",
     "Brent/WTI rises >5% in a session — OPEC cut, supply shock or geopolitical escalation",
     ["CAD widens", "INR depreciates", "Imported inflation rises",
      "RBI turns hawkish", "Credit tightens", "Consumption slows"],
     "Reference event for the 3-degree framework."),

    ("fed_hike",         "US Fed Hawkish Surprise",       "FOMC_DECISION",
     "FOMC delivers ≥25bps surprise above CME FedWatch implied; or hawkish dot-plot shift",
     ["FII outflows from EM", "INR depreciates", "BankNifty sells off",
      "T+14: NIM expansion narrative emerges"],
     "Two-phase trade: Phase 1 bear (T+0-5), Phase 2 bull (T+14+) for high-CASA banks."),

    ("rbi_hike",         "RBI Surprise Rate Hike",        "RBI_MPC",
     "RBI MPC delivers an off-consensus rate hike",
     ["NBFC funding cost spikes", "Auto/housing demand cools",
      "Eventual bank NIM expansion as loans reprice"],
     None),

    ("vix_spike",        "Global Risk-Off (VIX > 30)",    "CBOE_VIX",
     "CBOE VIX spikes above 30, OR India VIX > 25",
     ["EM equities sold", "FII outflows", "Mid/small caps cascade",
      "Rotation into defensives"],
     None),

    ("china_stim",       "China Economic Stimulus",        "DXY",
     "Major PBoC easing or Beijing stimulus package announced",
     ["LME metals rally", "Indian metals re-rate",
      "Possible passive-flow rotation away from India"],
     None),

    ("fii_surge",        "FII Inflow Surge",               "FII_FLOW",
     "FII net buying > ₹3000cr in a session",
     ["Large-cap bid first", "Index moves up", "Mid-cap with lag"],
     None),

    ("monsoon_fail",     "Monsoon Failure (>15% below LPA)", "USDINR",
     "Cumulative monsoon rainfall >15% below LPA",
     ["Rural cash-flow stress", "Two-wheeler / FMCG rural slowdown",
      "NBFC credit costs rise", "Policy response on water infra"],
     "Discrete-ish event — fires once IMD declares deficit."),

    ("budget_infra",     "Union Budget Infra Push",        "INDIA_VIX",
     "Capital expenditure outlay raised >15% YoY in Budget",
     ["Order-book uplift for cap-goods/defence/railways",
      "Cement and steel demand follows"],
     None),

    ("commodity_shock",  "Continuous Commodity Price Shock", "CRUDEOIL",
     "Z-scored shock in any tracked MCX commodity (detector-fired)",
     ["Direct cost / revenue impact",
      "Currency / rate transmission (case-by-case)"],
     "Used by the live commodity_engine path. trigger_id is canonical CRUDEOIL but the sector edges apply across commodities; the engine resolves which commodity fired at query time."),
]


# (event_id, degree, label, transmission_channel)
EVENT_DEGREES: list[tuple] = [
    ("crude_up", 1, "Direct cost exposure",
     "Immediate P&L hit — no lag between input price and margin compression"),
    ("crude_up", 2, "Macro transmission",
     "CAD widens → rupee sells off → imported inflation embeds → rate-hike expectations price in"),
    ("crude_up", 3, "Demand destruction",
     "Retail petrol/diesel hike → rural mobility cost rises → rural consumption contracts"),

    ("fed_hike", 1, "FII forced selling",
     "Risk-asset rotation out of EM banking on rate-differential compression"),
    ("fed_hike", 2, "Domestic NIM repricing",
     "Floating-rate asset book reprices upward; high-CASA banks structurally benefit"),
    ("fed_hike", 3, "Real economy effect",
     "Mortgage / auto loan rates pass through; rate-sensitive demand cools"),

    ("rbi_hike", 1, "Liability-side shock",
     "NBFC and wholesale-funded liabilities reprice immediately"),
    ("rbi_hike", 2, "Asset-side benefit",
     "Bank NIMs expand 1-2 quarters out as loans reprice"),
    ("rbi_hike", 3, "Demand destruction",
     "Real-estate, auto demand cools as EMIs rise"),

    ("vix_spike", 1, "Risk-off cascade",
     "FII deleveraging; high-beta names sold first"),
    ("vix_spike", 2, "Defensive rotation",
     "Capital recycles into pharma/FMCG/IT defensives"),
    ("vix_spike", 3, "Macro spillover",
     "Sustained risk-off feeds into EM credit spreads, FX volatility"),

    ("china_stim", 1, "Commodity reflation",
     "LME metals lift; Indian metal producers re-rate on price + spread"),
    ("china_stim", 2, "FII rotation",
     "Passive flows may rotate out of India broad-market into China-risk"),
    ("china_stim", 3, "Real economy spillover",
     "Restocking cycle filters into industrial demand globally"),

    ("fii_surge", 1, "Large-cap absorption",
     "Liquidity flows into Nifty / BankNifty large-caps first"),
    ("fii_surge", 2, "Mid-cap follow-through",
     "Once large-cap saturates, flows reach mid-caps"),
    ("fii_surge", 3, "Speculative tail",
     "Small-caps and momentum names rally with widest spread"),

    ("monsoon_fail", 1, "Rural demand shock",
     "Direct hit to rural-cycle sectors (2W, agri, NBFC)"),
    ("monsoon_fail", 2, "Inflation impulse",
     "Food inflation rises; RBI policy stance impacted"),
    ("monsoon_fail", 3, "Policy response",
     "Government water-infra and rural-relief spending accelerates"),

    ("budget_infra", 1, "Order-book uplift",
     "Cap-goods / defence / railways books expand on announcement"),
    ("budget_infra", 2, "Materials demand",
     "Cement / steel / construction materials demand follows"),
    ("budget_infra", 3, "Multi-year cycle",
     "Sustained capex translates into earnings visibility"),

    ("commodity_shock", 1, "Direct impact",
     "Cost-side and revenue-side first-degree exposures fire"),
    ("commodity_shock", 2, "FX / rates transmission",
     "Persistent moves transmit to currency / rate expectations"),
    ("commodity_shock", 3, "Demand-side adjustment",
     "Consumer cyclicality plays out over weeks-months"),
]


# (event_id, from_degree, to_degree, gating_trigger_id, confirms_threshold,
#  refutes_threshold, description)
# Threshold semantics: signed % move (or std-dev) of the gating trigger
# observed during the lag window for the FROM degree.
EVENT_TRANSMISSIONS: list[tuple] = [
    ("crude_up", 1, 2, "USDINR", +0.005, -0.002,
     "INR depreciation > 0.5% confirms macro-transmission path → light up degree 2."),
    ("crude_up", 1, 2, "US10Y", +0.05, None,
     "US 10Y yield rise reinforces hawkish-Fed feedback loop."),
    ("crude_up", 2, 3, "INDIA_CPI", +0.3, None,
     "India CPI surprise > +0.3% confirms imported-inflation pass-through → light up degree 3."),

    ("fed_hike", 1, 2, "FII_FLOW", -3000.0, +1000.0,
     "FII net selling > ₹3000cr confirms forced-selling phase; reversal flips into Phase-2 bull."),
    ("fed_hike", 2, 3, "USDINR", +0.01, None,
     "Sustained INR depreciation transmits to real-economy rate channel."),

    ("vix_spike", 1, 2, "INDIA_VIX", +5.0, -3.0,
     "India VIX rises >5pts confirms domestic risk-off; falling VIX refutes."),

    ("monsoon_fail", 1, 2, "INDIA_CPI", +0.5, None,
     "CPI surprise >0.5% confirms food-inflation impulse."),
]
