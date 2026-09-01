# Stage 4B — Guidance estimator (T1): built, validated, and reinterpreted

**Date:** 2026-09-01 · **Code:** `bharat_terminal/research/e1_composite.py`
**Design:** within-company (the Stage 5 form) — guidance vs the statistical
GBM **on the same rows**, so estimator quality is never confounded with
company mix.

## Coverage

15.3% of results events (3,279 of 21,433 since Jun-2024) have applicable
guidance — stated before the announcement, for a period covering the target
quarter. That is ~5x T2 (business updates) and the tier the thesis rests on.
After conversion, 2,274 revenue rows / 522 companies were scorable.

## Result: guidance is a WORSE fundamental forecaster than the statistics

| conversion basis | n | guidance MAE | GBM MAE | guidance vs GBM | median guided/actual |
|---|---|---|---|---|---|
| revenue growth % | 1,396 | 0.177 | 0.126 | **−41.1%** | 1.02 |
| ebitda growth % | 46 | 0.269 | 0.213 | **−26.4%** | 1.09 |
| revenue absolute (annual) | 851 | 0.822 | 0.176 | −368% | 0.19 |
| ebitda margin × revenue | 1,025 | 0.911 | 0.266 | −242% | 0.17 |

The percentage-growth conversions are **well calibrated** (median
guided/actual 1.02 and 1.09), so the −41% and −26% are real measurements,
not conversion artefacts. Management guidance genuinely predicts a
company's own next quarter worse than a model trained on its history.

On reflection this is unsurprising: guidance is annual and coarse while the
GBM targets the specific quarter; it is stated once and goes stale; and it
is strategic — our own fulfilment work puts the met-rate at **56.6%**, so
it is roughly a coin flip whether a promise is kept.

## Data-quality finding: the ledger needs a `scope` dimension

The absolute-value rows are not a modelling failure but a **taxonomy**
failure. Reading `metric_raw` shows `metric_key='revenue'` conflates four
different claims:

- total-company revenue — NBCC *"Revenue (Standalone)"* ₹13,000–14,000cr FY27
- **segment** revenue — HFCL *"EPC business revenue"*, JKCEMENT *"paint
  business revenue"* (₹380cr guided against ₹4,032cr company quarterly)
- **incremental** revenue — GOKEX *"Total incremental revenue from capacity
  expansions"*
- multi-year **aspirations** — VSTTILLERS guiding FY29

Segment and incremental figures are naturally a fraction of company
revenue, which is exactly the 0.19 median ratio. A regex scope filter
(`is_company_level` + `horizon_ok`) removes only ~8%, so the distinction is
mostly not recoverable from `metric_raw` alone — it must be captured at
extraction time.

**This matters beyond E1.** Paper A's fulfilment labels currently treat a
segment promise and a company promise as the same claim. Adding
`scope ∈ {total, segment, incremental}` to the guidance extraction is now a
prerequisite for both the estimator and the dataset's integrity.

## Why this does NOT sink the thesis — and what it changes

Stage 4A established that **E1's target is what the MARKET expected, not
what the fundamentals will be**, and demonstrated that the two can diverge
sharply (a better EPS expectation made the PEAD signal *worse*). Today's
result is the same distinction from the other side: guidance is a poor
*forecast*, but the market may still **anchor** on it. If management guides
15% growth and delivers 12%, the market is disappointed — regardless of the
fact that a GBM would have predicted 12% all along. That is precisely the
behavioural mechanism PEAD's drift is built on.

So guidance's value hypothesis is untouched by this result, and remains
untested. Stage 5 is the decisive experiment, scored on **reaction
prediction**, not MAE.

## Architectural consequence for E1 v1

Do **not** blend guidance into the point forecast — a 50/50 blend scored
−24.8% vs naive, worse than either input, because averaging a good
forecaster with a bad one degrades both. Instead:

- **E1 primary expectation = GBM** (+ peer features), universe-wide.
- **Guidance is carried as a SEPARATE, tracked expectation** with its own
  tier and provenance, never averaged into the primary.
- **E2 then computes TWO surprises:** the *fundamental* surprise (actual vs
  GBM) and the *expectation-gap* surprise (actual vs guidance).
- **Stage 5 tests which one predicts reactions** — and that is the real
  go/no-go for the whole text thesis.

This is a better architecture than the spec's original single-composite
design, and it follows directly from the evidence rather than from taste.

## Next

1. `expectations` table storing both expectations side by side, PIT-stamped.
2. Automated leakage audit (every input timestamp < announcement).
3. Guidance `scope` extraction — required for both E1 and Paper A.
