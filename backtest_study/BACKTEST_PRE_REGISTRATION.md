BHARAT TERMINAL — THESIS BACKTEST PRE-REGISTRATION
====================================================

Date locked: 24/05/2026
Status: PRE-DATA. No price data has been examined under these criteria.

Document version: 1.0
Locked. Modifications require a new document.

-----------------------------------------------------
1. HYPOTHESIS BEING TESTED
-----------------------------------------------------

PRIMARY HYPOTHESIS (H1):
When a meaningful macro/commodity trigger occurs, sectors with non-direct
exposure to that trigger via plausible economic transmission channels
(2nd-degree sectors) exhibit cumulative abnormal returns (CAR) over a
delayed window [T+1, T+10] that are:
  (a) statistically distinguishable from a matched control set of
      unrelated sectors at p < 0.05, AND
  (b) at least 30% of the magnitude of 1st-degree sector CAR over [T+0, T+1]

NULL HYPOTHESIS (H0):
2nd-degree sector CAR is statistically indistinguishable from control
sector CAR, OR the 2nd-degree CAR magnitude is less than 10% of 1st-degree
CAR magnitude.

WHAT FALSIFICATION LOOKS LIKE:
- Control sectors move as much as 2nd-degree sectors → H0 cannot be rejected
- 2nd-degree CAR is in the wrong direction relative to channel prediction
- No detectable lag pattern (i.e., 2nd-degree response is fully complete by T+1)

-----------------------------------------------------
2. EVENT SELECTION
-----------------------------------------------------

EVENT TYPES (pre-frozen):
  A. Crude oil shocks: Brent moves >= ±5% in 24h (single trading day)
  B. USD/INR shocks: INR moves >= ±0.7% in 24h
  C. RBI MPC surprises: policy decision differs from consensus
     (consensus defined as Bloomberg or Reuters poll median)
  D. US FOMC surprises: Fed action differs from CME FedWatch
     implied probabilities at T-1 close
  E. Industrial metal shocks: copper, aluminium, or steel moves >= ±4% in 24h

EVENT WINDOW: Last 12 months from [today's date]

MAXIMUM EVENTS PER TYPE: 5 (to prevent any single category dominating)

TOTAL TARGET EVENT COUNT: 10–15 events

EVENT EXCLUSIONS:
- No event within 3 trading days of a NIFTY 50 result-heavy day
  (defined as: more than 5 NIFTY 50 names reporting results that day)
- No event within 1 trading day of another qualifying event
  (overlapping events confound the analysis; we keep the earlier one)
- No event during expiry week (last Thursday + preceding 2 days)
- No events on or within 2 trading days of a NIFTY 50 rebalance date

SOURCE OF EVENT DATES:
- Crude/INR/metals: derived from existing price_points and commodity data
  in DB; if commodity data unavailable for a date, fetch from external
  source and document
- RBI surprises: official RBI MPC calendar + consensus from Bloomberg
  archives or Mint/ET reporting
- Fed surprises: FOMC calendar + CME FedWatch historical archives

-----------------------------------------------------
3. SECTOR DEFINITIONS
-----------------------------------------------------

SECTOR TAXONOMY: NSE industry classification (use companies.sector and
companies.industry from the DB schema). If both fields exist, prefer
industry as more granular.

For each event type, define three disjoint sector sets:

3.1 FIRST-DEGREE SECTORS
Defined as: sectors with direct exposure to the trigger asset via either
output price (sector produces/sells the asset) or primary input cost
(sector's COGS is >25% the trigger asset, where this can be ascertained
from public disclosure).

Pre-frozen mappings (locked before data analysis):

  Crude oil shock:
    1st-degree: Oil & Gas Exploration, Refineries
    2nd-degree: Paints, Aviation, Logistics/Shipping, Tyres, Petrochemicals
    Control:    IT Services, Pharmaceuticals, Private Banks

  USD/INR shock (INR weakening):
    1st-degree: IT Services, Pharmaceuticals (export-heavy)
    2nd-degree: Oil & Gas (importers), Electronics, Capital Goods
    Control:    FMCG (domestic), Cement, Power Utilities

  RBI MPC dovish surprise:
    1st-degree: Banks, NBFCs
    2nd-degree: Real Estate, Auto, Consumer Durables
    Control:    IT Services, Pharma, FMCG

  Fed surprise (dovish):
    1st-degree: NIFTY Bank, broad NIFTY 200
    (Note: Fed events affect broad market via FII flows; sectoral
    decomposition is weaker. We test only the broad NIFTY 200 effect
    and consider this event type separately.)

  Industrial metal shock (e.g., copper):
    1st-degree: Non-Ferrous Metals, Mining
    2nd-degree: Cables/Wires, Capital Goods, Auto Ancillaries
    Control:    Software, Pharma, FMCG

3.2 CONTROL SECTORS
Selected to be:
  (a) Plausibly unaffected by the trigger via any economic channel
  (b) Matched approximately on market cap distribution within the
      NIFTY 200 universe
  (c) Not on the 1st or 2nd degree list for this event type

-----------------------------------------------------
4. UNIVERSE
-----------------------------------------------------

COMPANY UNIVERSE: NIFTY 200 constituents as of event date.
(Use the most recent universe_membership snapshot prior to each event date.)

If universe_membership doesn't have NIFTY 200 historically, use NIFTY 200
as of today and accept this is a minor look-ahead bias we document and
flag in the final report.

LIQUIDITY FILTER: Companies must have at least 200 non-null price_points
in the 250 trading days prior to the event date. Otherwise excluded.

PRICE DATA: price_points table, daily close.

RETURNS: log returns of close-to-close prices.

-----------------------------------------------------
5. ABNORMAL RETURN COMPUTATION
-----------------------------------------------------

For each company-event observation:

  AR_t = R_company_t - R_market_t

where R_market_t is the NIFTY 200 index return for day t. (If NIFTY 200
index series isn't directly available, construct equal-weighted return
of the universe minus the company itself for that day.)

CUMULATIVE ABNORMAL RETURN over window [a, b]:

  CAR_company[a,b] = sum(AR_t for t in [a, b])

SECTOR-LEVEL CAR for window [a, b]:

  CAR_sector[a,b] = mean(CAR_company[a,b] for company in sector)

Use equal weighting at the sector level. (Cap-weighting can be a
robustness check, not the primary analysis.)

-----------------------------------------------------
6. TEST WINDOWS (PRE-FROZEN)
-----------------------------------------------------

For 1st-degree sectors:
  Primary window:    [T+0, T+1]
  Robustness window: [T+0, T+3]

For 2nd-degree sectors:
  Primary window:    [T+1, T+10]
  Robustness windows: [T+1, T+5], [T+3, T+10]

For control sectors:
  Same windows as the comparison group.

NO POST-HOC WINDOW SELECTION. We report all pre-registered windows. If
results are significant in one robustness window but not the primary
window, this is noted as a caveat — not as a finding.

-----------------------------------------------------
7. STATISTICAL TESTS
-----------------------------------------------------

PRIMARY TEST (per event type, pooled across events):

  Two-sample Welch's t-test:
    Group A: CAR of 2nd-degree sector observations
    Group B: CAR of control sector observations
    H0: mean(A) = mean(B)
    H1: mean(A) ≠ mean(B)
    Significance threshold: p < 0.05 (two-sided)

ROBUSTNESS:
  - Wilcoxon rank-sum test (non-parametric equivalent)
  - Bootstrap 95% confidence intervals on (mean(A) - mean(B))
  - Per-event sign test: fraction of events where 2nd-degree CAR
    > control CAR

POWER NOTE:
  With ~15 events and ~3-5 sectors per group per event, total N per
  group is ~50-75. This is borderline-powered to detect effect sizes
  below ~0.4 standard deviations. We will report achieved power
  alongside the p-value.

MULTIPLE COMPARISONS:
  We are running 5 event types × 3 test windows = 15 primary tests.
  Apply Bonferroni correction: effective significance threshold
  becomes p < 0.003. We report both raw and corrected p-values.

-----------------------------------------------------
8. DECISION RULES (PRE-FROZEN)
-----------------------------------------------------

OUTCOME A — CLEAR POSITIVE:
  ≥ 3 of 5 event types show 2nd-degree CAR significantly different
  from control CAR (raw p < 0.05) in the expected direction, AND
  pooled 2nd-degree effect is at least 30% of 1st-degree effect.
  → ACTION: Push hard on dossier expansion, Stage 4 rewrite, and
  customer outreach. The thesis is empirically supported.

OUTCOME B — AMBIGUOUS:
  1-2 of 5 event types show significance, or magnitudes are small but
  consistent in direction.
  → ACTION: Refine. Either (i) tighten channel definitions (which
  channels showed effects, which didn't), or (ii) extend the study
  to 24-36 months for more power. Do not commit further build effort
  until a refined study clarifies.

OUTCOME C — CLEAR NEGATIVE:
  0 event types show significance, OR 2nd-degree effects are in the
  wrong direction, OR control set moves as much as 2nd-degree set.
  → ACTION: Pivot. The thesis as stated does not hold. Convene a
  founder discussion on whether to (i) redefine the product around
  what *does* work in the data, (ii) shift focus to a different
  alpha source, or (iii) wind down.

-----------------------------------------------------
9. THINGS WE EXPLICITLY DO NOT TEST IN THIS STUDY
-----------------------------------------------------

- Whether the existing Taperead dossier (RELIANCE) correctly predicts
  any specific event. (n=1 cannot test predictive power.)
- Whether company-level signals exist within validated sectors.
  (This is the follow-on study if H1 is supported.)
- Channel-specific lag attribution (INPUT_COST vs FX_TRANSLATION vs
  INTEREST_BURDEN). The current study aggregates across channels.
- Intraday timing or sub-daily signal.
- Statistical arbitrage P&L. We report CAR, not Sharpe.

-----------------------------------------------------
10. REPRODUCIBILITY
-----------------------------------------------------

- All event dates, sector mappings, and universe lists are committed
  to this document and frozen before analysis.
- The analysis notebook is committed to the repo with a deterministic
  seed for any bootstrap sampling.
- Raw output tables are saved alongside the notebook.
- A separate `BACKTEST_RESULTS.md` is written after analysis, citing
  this pre-registration document.

-----------------------------------------------------
SIGNATURES
-----------------------------------------------------

Locked by: Aviral Verma
Reviewed by: Claude (co-founder, advisory)
Lock date: 24/05/2026
