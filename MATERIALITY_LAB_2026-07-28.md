# Materiality Lab — problem statement & experiment ledger

Started 2026-07-28. Internal-only research (RA compliance Lane A: nothing
here reaches customer surfaces). Parent goal: automated trading on exchange
filings as new information arrives — this lab owns the "is this filing worth
acting on at all?" gate.

## Problem statement

Given an exchange filing for company `s` observed at event time `t`, with:
- the filing headline and full attachment text (news_items.body — complete
  PDF extraction),
- the analysis pipeline's LLM outputs (magnitude 1-5, sentiment, horizon,
  confidence, n_companies),
- cross-referenced earnings-surprise data (|composite SUE| when the filing
  sits within ±3d of a surprise row),
- pre-event price context (5-session run-up, scaled by trailing vol),

estimate **P(material)** where

    material ≡ |next-session close-to-close return| ≥ max(2·σ60, 1.5%)

using the proven public-day convention (IST date; post-15:30 filings roll to
the next trading session). Direction-agnostic **by design**: the 2026-07-04
signal audit showed direction is a coin flip out-of-time, while reaction
SIZE is conditioned by persistent, learnable structure (event type, surprise
size, priced-in run-up).

For the trading parent-goal, what matters (in order):
1. **Ranking at the top** — which filings deserve immediate attention/capital.
   Top-decile observed materiality and lift, not global accuracy.
2. **Calibration** — position sizing and thresholds need honest
   probabilities (Brier, reliability by decile).
3. **Event-time discipline** — every feature knowable at `t`; no look-ahead;
   vol appears in the LABEL (it defines materiality) but never in FEATURES
   (closes the labeling-geometry loophole found in v1).

## Fixed evaluation protocol

- Dataset: impact_reports ⋈ news_items, **filings-only** (NSE_FILINGS +
  BSE_FILINGS — now includes dual-listed BSE copies post 2026-07-23),
  exploded per company_impact, magnitude > 0, joined to price_points.
- Split: **time-based**, last 30% of events as test (`split_frac 0.3`).
  No random splits, ever (regime shifts broke run 1).
- Seed 42, visible, everywhere.
- DB: read-only; dataset built ONCE per refresh and cached to
  `exports/materiality_lab/` — experiments never re-hit prod Postgres.
- Guards (the no-improvement rule, executable):
  - must beat constant-base-rate Brier;
  - must beat the empirical P(material | LLM magnitude bucket) mapping on
    Brier AND AUC — that mapping is what the pipeline already provides free.
- A challenger replaces the champion only by beating it on the SAME fresh
  time split, primary metric AUC with top-decile lift and Brier as
  tie-breakers.

## Current state (verified from exports/materiality_calibrator_v1_2026-07-19.json)

Champion: **v3 logistic** (commit 1e9d411, data through 2026-07-19,
n = 7,854 filing-events, base rate 7.1% train / 7.5% test):

| metric | v3 logistic | magnitude baseline | constant |
|---|---|---|---|
| AUC | **0.6311** | 0.4145 | — |
| Brier | **0.0684** | 0.0692 | 0.0691 |
| top decile | pred 0.147 / obs 0.140 | — | — |

Established negative results (do not re-litigate without new data volume):
- LLM magnitude alone is ~noise for filings materiality (AUC 0.41).
- HGB lost to logistic twice (v2, v3) — capacity was not binding at ~8K rows.
- **Headline** SVD-64 embeddings added nothing over the 16-bucket keyword
  taxonomy (0.617 < 0.627).
- Stock-context features (vol/liquidity/mcap) were HARMFUL (alone 0.512;
  removing them raised AUC 0.627 → 0.638 on the v2 split).
- Feature set v3 = event_content (taxonomy + |SUE| + runup_sigma) +
  llm_outputs (magnitude, sentiment, horizon, confidence) only.

Standing gate: customer-facing wiring waits for a confirmation re-run in the
mid-Q2FY27 results season (~Nov 2026). Internal digest-ordering wiring is
already eligible on v3 numbers.

## Experiment roadmap

- **E0 — re-baseline**: exact v3 replication on current data. The corpus
  grew ~9 days incl. the heart of Q1FY27 results season and dual-exchange
  BSE rows; every comparison below is against E0, not against the 07-19
  numbers.
- **E1 — filing body text**: TF-IDF (train-fit) over headline + first 8,000
  chars of the attachment text → linear models. The single biggest untapped
  source: the calibrator has only ever seen headlines; the actual results
  tables, order values, and rating rationales live in the body.
- **E2 — model sweep on v3 features**: regularization grid, class weights,
  tuned HGB, random forest, isotonic-calibrated HGB. Re-tests the
  "capacity non-binding" conclusion now that the dataset is larger.
- **E3 — combo**: body-TF-IDF → SVD (train-fit) stacked with v3 structured
  features → linear + boosted.
- Later (queued, in rough order): per-event-type specialist models
  (results vs order-win vs rating have different priors and vocab);
  guidance/transcript-derived features for results filings; abstention
  thresholds + expected-value analysis under the trading use-case (cost of
  acting on immaterial vs missing material); label-robustness checks
  (K_SIGMA sweep as sensitivity, NOT as target-shopping); Nov season
  confirmation re-run.

## Ledger

Machine-readable: `exports/materiality_lab/ledger.jsonl` — one JSON line per
variant per run (experiment id, params, all metrics, split cutoff, n).
Human summary of each loop iteration appended below.

### Champion history

| version | model | AUC @0.3 split | Brier | notes |
|---|---|---|---|---|
| v3 (2026-07-19) | logistic on structured | 0.7056 (fresh re-run; 0.6311 on 07-19 data) | 0.0951 | |
| v4 (2026-07-28) | mean(lr_C0.1, svd128_hgb) | 0.7227 | 0.0935 | wins ALL 4 splits (0.718–0.723); body text via TF-IDF→SVD-128 is the new signal; ~5pp overconfident in top decile (ranking unaffected) |
| **v4.1 (2026-07-28)** | **v4 + 22-bucket taxonomy** | **0.7242** | **0.0933** | beats v4 on AUC on all 4 splits (0.7195–0.7263, +0.001–0.003), top-decile obs 0.2587 @0.3 (lift 2.32); extra buckets mined from the 'other' mass (press_release, investor_presentation, business_update, litigation_insolvency, procedural_report, general_updates) |

### Iterations

- **2026-07-28 #1**: Lab established. E0–E3 launched (single DB pass →
  cached frame → sequential experiments). Results in the next entry.
- **2026-07-28 #2 (E4 calibration/blends + E5 stability)**: dataset now
  11,496 events (+46% vs 07-19), test = peak Q1FY27 season (base rate 11.2%
  vs 7.2% train). E0 re-baseline: v3 logistic 0.7056 (magnitude baseline
  itself woke to 0.6039 in-season — compare within-split only). Body text
  is real signal (E1 TF-IDF alone 0.669, best top-decile capture 27.3%;
  E3 svdhgb 0.7126). RF ranked 0.7193 but wildly miscalibrated; isotonic
  repair cost its ranking (0.6831) — dropped. NEW CHAMPION v4 =
  mean(lr_C0.1, svd128_hgb): beats v3 + both parents + all baselines on
  every split. NEGATIVE results: inner-split isotonic starves TF-IDF
  models (0.5822 — do not calibrate text models on a thin cal slice);
  3-way blend with calibrated RF adds nothing (0.7195 < 0.7227).
- **2026-07-28 #3 (E6 diagnostic + E7/E8 taxonomy & specialist)**:
  per-type map of the blend — strong: rating 0.925, corp_action 0.899,
  order_win 0.843, mgmt_change 0.773; weak: fund_raise 0.487, agm/investor
  ~0.60; results (biggest high-base type) only 0.6749. 'other' was 29% of
  test and 36% of the top decile → mined it into 6 new buckets → tax22
  taxonomy → v4.1 champion (see table). NEGATIVE: results-specialist
  trained within-type scored 0.6138 vs the global model's 0.6749 on the
  same rows — ~1.1K training rows starve the text model; do NOT specialize
  before the corpus roughly doubles (post-Nov). Artifact note: 
  exports/materiality_lab/materiality_v4_blend.joblib is the 16-bucket v4
  (exported before tax22); any wiring step regenerates it as v4.1.
- **Loop closed 2026-07-28**: v3 0.7056 → v4.1 0.7242 on the identical
  split (magnitude baseline 0.6039); top-decile capture 0.2326 → 0.2587.
  Queued next (in order): top-decile calibration (needed before any
  position-sizing use), guidance/transcript features for results filings,
  per-type specialists post-Nov, November confirmation re-run (unchanged
  gate for customer-facing wiring).
- **2026-07-28 #4 (D-series: DIRECTION, 400-day window, drift-adjusted
  vs same-band neutral controls)**: D0 — negative classifications carry
  real directional edge at EVERY band (high 63.8% hit +22.2pp, medium
  60.0% +19.3pp, low 55.3% +7.8pp); ALL positive bands ≤ drift (−1.1 to
  −4.5pp). Per type: **results_negative 79.5% (+32.2pp, n=88)** best
  signal in the system; litigation_insolvency_negative +12.6pp;
  **order_win_positive 64.1% (+11.5pp, n=237)** = the ONLY positive class
  with real edge; anti-signals: procedural_report_positive −13.9pp,
  fund_raise_positive −8.7pp, results_positive −7.7pp. D1 — global
  direction models weak (AUC ~0.58, acc ~0.55) but top-conviction decile
  hits 67-69%; not better than the class rules. D2 — results direction
  model 63.2% (below the results_negative rule); next-day SUE-SIGN rule
  51.7% = coin flip (PEAD's edge is 20d composite-decile, NOT next-day
  sign); rating upgrade/downgrade keyword rule 47.4% = dead. EX — v4.1
  tax22 gate artifact refit on all 11,496 rows through 2026-07-27 →
  exports/materiality_lab/materiality_v4_1_tax22.joblib (the paper
  trader's gate). PAPER-TRADER RULE IMPLICATIONS: headwind_short at
  mag≥3 (not ≥4); NEW orderwin_long; tailwind_long stays OFF; no rating
  strategy.
- **2026-07-28 #1a (founder request — directional confusion matrix,
  post-dual-exchange cohort, filings ≥ 2026-07-23 09:02 UTC, next-session
  close-to-close, settled through 2026-07-27)**: tailwind 336↑/246↓ (57.7%
  correct), headwind 41↑/64↓ (61.0% correct); high-band (mag≥4) tailwind
  124↑/97↓ (56.1%), headwind 5↑/15↓ (75.0%, median −2.61%). CONTROL:
  neutral-classified filings rose 57.0% (median +0.31%) — the tape drifted
  up, so tailwind accuracy ≈ drift (zero directional edge, and median ret
  +0.26% < neutral's +0.31%), while headwind's 61% down-rate is +18pp
  against a 42.6% down base (z≈3.8). Directional signal is ASYMMETRIC:
  concentrated in negative classifications; positives need materiality
  gating, not direction. Caveats: 3 trading days, one rising-tape regime,
  402 non-neutral impacts unresolved (public day = today), includes
  propagated (hop>0) impacts.
