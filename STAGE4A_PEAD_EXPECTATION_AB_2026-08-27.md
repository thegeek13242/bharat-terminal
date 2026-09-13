# PEAD A/B — does a better expectation sharpen the drift signal?

**Date:** 2026-08-27 · **Code:** `bharat_terminal/research/pead_expectations_ab.py`
**Design:** swap ONLY the expectation inside composite-SUE; hold the σ
denominator, winsor cap, cohort deciling and return window fixed.
**Sample:** 27,347 events, 18 quarterly cohorts (2021→2026), 20-trading-day
abnormal return vs NIFTYBEES, entered D+1 (announcement gap excluded).

---

> ## ⚠ CORRECTION 2026-09-13 — the verdict below is WITHDRAWN
>
> **This study was computed against a broken benchmark.** `price_points` had no
> NIFTYBEES bars between **2025-05-09 and 2026-06-03** — 264 trading sessions —
> and `pead_expectations_ab.py` has no missing-bar guard; it simply subtracts the
> benchmark. **13,002 events had 20-day windows inside that hole.**
>
> Root cause: `sync_prices` (`kb/sync/pipeline.py:826`) filters each bhavcopy
> against symbols present in `companies`. The benchmark ETFs only entered
> `companies` on 2026-07-03, so daily sync never carried them; their history came
> entirely from bulk backfills, and this stretch was never backfilled.
>
> The gap was repaired on 2026-09-13 (1,056 rows, four ETFs, verified continuous
> at 100%). Re-running this exact script on repaired data **reverses the headline
> result**:
>
> | arm | as published (holed) | re-run 2026-09-13 (repaired) |
> |---|---|---|
> | composite (naive) — incumbent | 2.84%, t=5.69 | 2.78%, t=5.77 |
> | **composite (GBM) — challenger** | **2.48%, t=4.36** | **2.88%, t=5.72**, 16/18 |
> | eps only (naive) | 3.29%, t=3.38 | 3.44%, t=3.57 |
> | eps only (GBM) | 2.47%, t=3.33 | 2.76%, t=3.72 |
> | revenue only (naive) | 1.24%, t=1.70 | 1.01%, t=1.47 |
> | revenue only (GBM) | 2.36%, t=4.59 | 2.30%, t=4.92 |
> | hybrid | 3.28%, t=4.89 | 3.22%, t=4.99 |
>
> Sample moved 27,347 → 29,076 events. Attribution is clean: only **24** events
> are new since 2026-08-27, so the change is the repair, not data drift.
>
> **The GBM expectation now scores BETTER than the incumbent, not worse. "Do not
> swap the expectation" no longer follows from this evidence — the swap is a live
> candidate again and must be decided before the 2026-10-09 pre-registration.**
>
> **What survives unchanged:** the reframe in "Why — the reframe that matters"
> below rests on EPS and revenue moving in *opposite* directions, and that pattern
> holds on repaired data (naive EPS still beats GBM EPS; GBM revenue still beats
> naive revenue). Scoring Stage 5 on reaction prediction rather than forecast MAE
> still stands. The hybrid caution still stands — it remains a post-hoc pick
> across seven arms.
>
> Numbers below are left exactly as published so the error and its cause stay
> legible. Reproduce the re-run with:
> `docker compose run --rm --no-deps --entrypoint python research-runner -m bharat_terminal.research.pead_expectations_ab`
>
> Note: this script pins no data snapshot — it reads whatever the database holds.
> If these figures enter a pre-registration, pin a snapshot first.

---

## Result

| arm | D10−D1 | t-stat | cohorts>0 |
|---|---|---|---|
| **composite (naive)** — the incumbent | **2.84%** | **5.69** | 15/18 |
| composite (GBM expectation) | 2.48% | 4.36 | 16/18 |
| eps only (naive) | 3.29% | 3.38 | 15/18 |
| eps only (GBM) | 2.47% | 3.33 | 15/18 |
| revenue only (naive) | 1.24% | 1.70 | 12/18 |
| **revenue only (GBM)** | **2.36%** | **4.59** | 16/18 |
| hybrid (naive-eps + GBM-rev) | 3.28% | 4.89 | 15/18 |

## Verdict: do NOT swap the expectation. The Stage 3 recommendation is withdrawn.

> **SUPERSEDED 2026-09-13 — see the correction block at the top of this file.**
> This verdict was produced against a benchmark missing 264 sessions. On repaired
> data the GBM arm beats the incumbent (2.88% vs 2.78%), so the swap is open again.

Stage 3 recommended rebuilding composite-SUE on GBM expectations because
the GBM forecasts better (+12.8% to +20.7% skill). **Tested, and it is
wrong**: the swap *lowers* the composite spread (2.84% → 2.48%) and its
t-stat (5.69 → 4.36). Forecast accuracy and signal quality are not the
same objective — which is precisely why this was worth one day of work
rather than a quarter of assumption.

The components move in **opposite** directions, and that is the finding:

- **EPS: the better forecast makes the signal WORSE** (3.29% → 2.47%).
- **Revenue: the better forecast makes the signal MUCH better**
  (1.24% → 2.36%; t 1.70 → 4.59; 12/18 → 16/18 cohorts positive) — from
  statistically unconvincing to solid on the same events.

## Why — the reframe that matters for the whole E-series

PEAD is a **behavioural** phenomenon: the drift exists because investors
underreact to a surprise **relative to what THEY expected**, not relative
to the statistically optimal forecast. Bernard & Thomas (1990) argued the
drift is consistent with investors failing to grasp the seasonal
autocorrelation in earnings — i.e. anchoring on naive models. Measuring
surprise against a *better* model therefore moves you AWAY from the
behavioural wedge that generates the drift. The EPS result is exactly that
pattern; the market appears to anchor naively on the headline number.

Revenue behaves oppositely, which suggests investors do **not** anchor
naively there — so the naive revenue surprise is mostly predictable growth
(noise), and a better model strips it out to leave a cleaner signal.

**Consequence for E1/Stage 5 — the target is not what we assumed.** E1's
job is NOT to forecast fundamentals as accurately as possible. It is to
estimate **what the market expected**. Those are different objectives and
today proves they can diverge sharply. This *strengthens* the case for the
text-derived estimators: guidance and management commentary are what the
market actually anchors on, so they may capture market expectation better
than any statistical model — while a pure accuracy contest was never the
right test.

Stage 5's go/no-go should therefore be scored on **reaction prediction**
(does the estimator's surprise explain the announcement reaction and the
subsequent drift), not on forecast MAE. The spec already frames the
validation this way; today makes it non-negotiable.

## What to do with it

1. **Leave the live PEAD strategy alone.** The incumbent composite-SUE
   survives its challenger. No change to `pead_signals.py`.
2. **Revenue-SUE on GBM expectations is a genuine, single, pre-specifiable
   improvement** — carry it into the Stage 7 pre-registration as a named
   candidate, not into production now.
3. **Do not trust the hybrid yet.** Higher spread (3.28%) but lower t-stat
   (4.89) than the incumbent, and it is a POST-HOC construction chosen
   after seeing 7 arms over 18 cohorts. That is textbook selection-on-noise
   territory; it needs pre-registration and out-of-sample confirmation.

## Limits

18 cohorts (not the validated study's 51) because the GBM needs training
years; both arms share the same events, so the paired comparison is valid
but absolute spreads are not comparable to the validated +2.11%.
NIFTYBEES is a poor benchmark for microcaps, applied identically to both
arms. PIT revision-in-place (Stage 0) affects both arms equally.
