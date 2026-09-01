# Stage 2 — Business updates (T2): coverage, contamination, and build scope

**Date:** 2026-08-27 · **Scope:** §12 Stage 2 of the E-series spec · read-only.

## Headline: T2 is small — best estimate ~105–150 companies/quarter (3–5%)

Every successive tightening cut the number roughly in half. The final
figure is the one to plan against, not the first.

| measurement | companies | % of 3,191 Q1-FY27 results events |
|---|---|---|
| headline-pattern detection (Stage 0 method) | 136 | 4.3% |
| body-level, naive time filter | 509 | 16.0% |
| body-level, contamination removed (≥10-day gap) | 213 | 6.7% |
| **body-level, ≥10 days, numeric-proximity tightened** | **105** | **3.3%** |

Two independent error sources, each verified by sampling rather than
assumed:

1. **Timing contamination** — 463 of 792 "pre-results" filings were
   results-day investor decks (below).
2. **Pattern imprecision** — the loose body patterns fire on narrative
   prose. Verified false positives: Chambal Fertilisers matched
   "disbursement" in *"timely subsidy disbursements have supported industry
   liquidity"*; Simbhaoli Sugars in *"disbursement of dues & benefits to
   contract workers"*. Requiring the keyword within ~120 characters of a
   2+ digit number (and disambiguating "disbursement" to a period context)
   removes 108 of 213 companies.

The 105 figure is itself **not yet hand-validated** — a 50-item manual
sample is a prerequisite of the detector build, so treat ~105 as an upper
bound on precision-checked coverage.

## The contamination — and the design rule it forces

Naive "filed before the results announcement" is **wrong** and would have
silently poisoned E1. Of 792 pre-results-looking filings, **463 (325
companies) were filed less than a day before results** — they are
results-day **investor presentations**, not expectations. Two samples that
exposed it: Bank of Baroda's "update" contains Q1FY27 *Net Profit*, and
Maruti's is a results deck comparing against Q4 FY26.

The mechanism is subtle and worth recording: `earnings_surprises.announced_at`
is XBRL-derived, and the **press release leads XBRL by hours** (a documented
property of this pipeline). Anything keyed to that timestamp will absorb
same-session results material and call it prior information.

**Hard rules for the T2 detector:**
1. Require a **minimum gap of 10 days** before the results announcement
   (3 days as the absolute floor, with the residual risk flagged).
2. Prefer the **press-release** timestamp over the XBRL timestamp as the
   results clock where both exist.
3. Body-level detection, not headline: exchange headlines are boilerplate
   wrappers ("Announcement under Regulation 30 (LODR)-Press Release") and
   miss ~4x the real disclosures — Maruti, Eicher, M&M, Ashok Leyland and
   TVS all file monthly sales under generic headlines.

## Does T2 still earn a place at 3–5%?

A business update **literally discloses the quarter's revenue** before the
print. The 2026-08-27 A/B showed revenue-SUE is exactly the channel where
better expectations pay (D10−D1 1.24% → 2.36%, t 1.70 → 4.59). For the
213 companies that pre-disclose, the expectation is not a model estimate —
it is the company's own number. That is the strongest possible revenue
expectation, and it also tells E2 to shift weight onto **margin** surprise
for those names, because revenue is already known to the market.

Three honest counterweights, and together they demote T2 from "build it
now" to "optional":
- **3–5% coverage**, an order of magnitude below T1's ~500+ companies.
- These names skew large and liquid (banks, autos, realty) — the *covered*
  end, where the drift edge is smallest by the program's own thesis.
- **No history** (live filings start 2026-05/07), so T2 cannot enter the
  Stage 5 within-company test at all.

## Recommended build scope (respects the Sep 20 E1 deadline)

**v1 (optional, cheap, no LLM) — must NOT consume Stage 4 time:** a deterministic body-level detector with the
≥10-day rule, emitting a per-(company, quarter) **flag** plus the source
filing id. Even as a flag it changes E1/E2 behaviour: revenue is known →
weight shifts to margin surprise. No `business_updates` migration needed
if it lands as a derived view; a table is preferable but is a migration
approval.

**v1.1 (fast-follow, after Stage 4):** numeric extraction of the disclosed
revenue/volume/deposit figures. Cost is trivial (~213 extractions/quarter,
a few dollars), but the parser needs building and validating, and the
calendar says that must not block the E1 composite.

**Not now:** the sector map for estimator #4 (peer read-across) stays cut
per the degradation path.

## Corpus limitation (carry into Stage 4)

Live filings only span NSE 2026-05-10 and BSE 2026-07-01 → present, i.e.
**one results season**. T2 therefore has no history for the Stage 5
within-company test, and can only be validated forward from Q2 FY27. The
BSE archive walker built for transcripts (`kb/sync/bse_transcript_backfill.py`)
generalises to any announcement subcategory and could backfill business
updates the same way — a decision to take AFTER Stage 5, not before.
