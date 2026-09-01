# Stage 3 — Statistical Baseline: Results & Exit Gate

**Date:** 2026-08-26 · **Scope:** §12 Stage 3 of `EXPECTATIONS_ENGINE_SPEC_2026-08-15.md`
**Code:** `bharat_terminal/research/seasonal_baseline.py` (read-only; writes nothing)
**Data:** `financial_results`, 106,805 quarterly rows / 3,633 companies, 2016→2026-06

## Exit gate: PASSED — and the winning model is cross-learning, not Foster

The gate was *"must beat seasonal-naive or E1's floor is broken."* It is
beaten decisively, but **not** by the accounting literature's classical
model. A single **global gradient-boosted model trained across all ~3,400
companies at once** (the M4/M5 cross-learning result, sklearn's
histogram-GBM — same family as the production materiality v4.1 artifact)
roughly triples the skill of the best per-company model:

| metric | seasonal naive | shrunk Foster | constant drift | **global GBM** | direction | always-up floor |
|---|---|---|---|---|---|---|
| revenue | 0.0% | +7.6% | +2.7% | **+20.7%** | 75.1% | 63.4% |
| ebitda  | 0.0% | +2.3% | +1.3% | **+13.7%** | 68.6% | 57.7% |
| pat     | 0.0% | +0.9% | +1.1% | **+12.8%** | 67.1% | 56.9% |

Skill = 1 − MAE/MAE(naive), out-of-sample, expanding-window annual
retraining (train on everything reported before year Y, predict Y).

**Stability across years** (revenue): 2023 +21.2%, 2024 +20.5%,
2025 +20.6%, 2026 +20.4% — n≈6k–12k per year. Flat to four significant
figures across four years is the strongest available evidence this is not
a regime artifact or an overfit.

**Two controls, both passed.** (1) A *constant* adjustment (the
train-period median) earns only +2.7% / +1.3% / +1.1% — so ~18pp, ~12pp
and ~12pp of the GBM's skill is genuinely conditional, not "things grow on
average." (2) A constant adjustment scores the always-up floor on
direction *by construction*; the GBM beats that floor by +11.7pp (revenue),
+10.9pp (ebitda) and +10.2pp (pat), which cannot come from drift.

**Why cross-learning wins, and why Foster had to be shrunk.** Foster fits
(δ, φ) per company on ~25 quarterly points. That data starvation *is* the
tail-blowup that forced the shrinkage hack below. Pooling 40,000
company-quarters lets one model learn the shared dynamics and use the
individual series only as features — exactly the M4/M5 finding, and it
arrives here without any new dependency.

## Models

- **sn** — seasonal naive, F = Y[t−4]. What composite-SUE uses today.
- **snd** — seasonal naive + trailing drift.
- **foster** — Foster (1977), the accounting literature's standard:
  seasonal differences follow an AR(1), F = Y[t−4] + δ + φ·(Y[t−1] − Y[t−5]),
  (δ, φ) fitted per company on trailing data only, φ clipped to ±0.95.
  Fitted on ~70% of forecasts; shorter histories fall back to `snd`.
- **shrunk foster** (the deliverable) — the Foster adjustment scaled by λ
  and capped at C× the company's own scale, both grid-tuned on train only.

## The finding that matters: raw Foster is a good signal wrecked by tails

Before shrinkage, Foster **beat** seasonal-naive on median error
(revenue 0.2039 vs 0.2207) and called direction 67.5% of the time — yet
**lost** on mean error (0.4999 vs 0.4595). A minority of AR extrapolations
blow up and the mean punishes them. Shrinking the adjustment (λ=0.6) and
capping it at half the company's scale keeps the signal and cuts the tail,
flipping −8.8% into **+7.6%** out-of-sample. Any future estimator that
extrapolates per-company parameters should expect the same pathology.

## Strategic implication for E1/E2 — CORRECTED after the GBM result

An earlier draft of this report (shrunk-Foster only) concluded that *"PAT
is very nearly unforecastable (+0.9%), so E1's text estimators must earn
their keep on MARGIN."* **That conclusion was an artifact of a weak model
and is withdrawn.** With cross-learning, PAT reaches +12.8% skill and
+10.2pp of directional edge. The corrected picture:

1. **The E1 floor is far higher than the classical model suggested.** Text
   estimators (guidance, business updates) must beat a statistical tier
   with real skill on *every* metric — +12.8% on PAT, not +0.9%. Stage 5's
   within-company test now runs against an honest control. A strawman floor
   would have manufactured a false positive; this is the single most
   valuable thing Stage 3 produced.
2. **T4 (statistical-only) is no longer a weak tier.** It covers the whole
   universe, including the ~1,631 BSE-only names where the thesis says the
   edge lives, and it now supplies genuinely informative expectations there.
3. **The margin thesis softens but survives.** Revenue (+20.7%) is still
   materially more forecastable than PAT (+12.8%), so relatively more
   residual uncertainty sits in the bottom line — where margin guidance
   speaks. Keep E2's margin-surprise term prominent, but it is no longer
   *the* channel, and Stage 5 should still be run per metric.
4. **NEW, and independent of all text work: composite-SUE may be leaving
   signal on the table.** The validated PEAD strategy standardises
   surprises against a *seasonal-naive* expectation. The GBM beats that
   expectation by 12.8% on PAT. Rebuilding SUE on GBM expectations is a
   cheap, self-contained experiment that could sharpen an already-validated
   edge — recommend running it in Stage 4 alongside the composite build.

## Limitations (stated, not hidden)

- **PIT**: `financial_results` rows are revised in place (Stage 0), so the
  backtest reads today's values, not first-published ones. This measures
  MODEL skill, not what a live system would have seen. The E-series event
  clock remains `earnings_surprises.announced_at`.
- **Survivorship**: only 25 of 3,923 symbols ever stopped trading in the
  corpus; forecast accuracy on a survivor panel is flattered.
- **Basis**: each company series is pinned to one basis (consolidated
  preferred, by observation count) — 638 companies report both, and mixing
  them mid-series would manufacture fake surprises.
- **Annual retrain granularity**: training cuts on `period_end`, so a test
  year's first forecast uses training targets announced up to ~2 months
  earlier. The ordering is still legitimate (Dec-quarter results are
  announced in February, before the March quarter is announced in May), but
  a per-announcement-date cut would be stricter.
- **Feature attribution not yet run** — sklearn's HistGBM exposes no native
  importances and permutation importance on 40k rows was out of scope; worth
  running before the GBM becomes an E1 input, to confirm no single feature
  is doing something suspicious.
- Forecast **uncertainty bands** are not yet emitted; Stage 4 needs them
  (trailing forecast-error σ per company is the natural estimator, and the
  walk-forward already computes the inputs).

## Hands to Stage 4

A universe-wide (3,341–3,386 companies), walk-forward, per-metric
expectation from the **global GBM** (shrunk Foster retained as the
interpretable fallback and as the `f_foster` feature) — the T4 tier of the
E1 composite and the control arm of the Stage 5 go/no-go. Reproduce with:

    python -m bharat_terminal.research.seasonal_baseline --metric revenue,pat,ebitda
