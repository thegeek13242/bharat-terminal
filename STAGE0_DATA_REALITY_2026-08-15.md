# Stage 0 — Data-Reality Report (E-series)

**Date:** 2026-08-15 · **Scope:** the seven §11 verification items from
`EXPECTATIONS_ENGINE_SPEC_2026-08-15.md` · **Method:** read-only queries
against production Postgres, this date. Every number below was measured,
not assumed.

## Exit-gate decisions

| Decision | Verdict | Basis |
|---|---|---|
| Estimator #5 (options-implied) | **OUT for v1** | no options data exists anywhere (item 1) |
| Estimator #7 (news consensus) | **DEFER to v1.1** | thin + not company-specific (item 5) |
| Estimator #4 (peer read-across) | **CONDITIONAL** | needs a sector map that doesn't exist yet (item 6) |
| E1 core (guidance + updates + statistical) | **GO** | T1=509 cos scoreable; T2≈100–110 cos/qtr; statistical=universe (items 2–3) |
| Abnormal-return model | **FEASIBLE** | market+size ETF blend covers the backfill window (item 6) |
| ≥8-quarter backfill | **SUPPORTED** | earnings_surprises 2020→present, ~24 qtrs, event timestamps present |

## Item 1 — Options data (estimator #5)

`derivatives_eod` no longer exists (the audit-era scaffold is gone). The only
derivatives table is `participant_oi` (1,296 rows — market-level
participant-wise OI, not per-stock chains). No `fno_engine` module on disk.
**No historical IV/straddle data → cannot backfill-validate → estimator #5
is out of v1.** Revisit only if option-chain ingestion is built later.

## Item 2 — Business-update coverage (T2)

Last 90 days of NSE+BSE filings, headline patterns (floor, not ceiling):
"business update" 90 filings/~60 cos; bank/NBFC "provisional" 20/~17;
quarterly updates 19/19; operational updates 13/12; monthly
sales/production 10/5; deposits-advances 6/5; disbursements 2/2.
**T2 ≈ 100–110 distinct companies per quarter** — smaller than hoped,
concentrated in banks/NBFCs and recent-disclosure adopters. Exchange
`category` values are too coarse to help (CORPORATE_ANNOUNCEMENT 26k,
COMPANY_UPDATE 9k in 90d). A Stage-2 body/attachment classifier should lift
this floor; plan proceeds with T2 as a minority tier.

## Item 3 — Guidance ledger quality (T1)

Much better than the spec assumed. The C0 build left a STRUCTURED ledger:
`metric_key`, `value_low/high`, `unit_key`, `period_key/period_end`,
`better_direction`, `scoreable`, `status`, `change_verdict`. Measured:
5,735 items / 774 companies; **93.3% carry numeric values, 71.1% dated,
25.8% already marked scoreable (1,479 items) → T1 = 509 companies with ≥1
scoreable target.** Status: 4,753 active / 977 superseded / **5 resolved —
the outcomes gap is confirmed and is exactly Stage 1's job.** Random
scoreable samples eyeballed: genuine period-bound quantitative targets
(e.g., ebitda_margin FY27 16%, revenue_growth 16–17%).

## Item 4 — financial_results PIT-safety

**FAIL as stored, recoverable in principle.** One row per (symbol,
period_end, period_type, consolidated) — unique index — and 37,066 of
106,688 rows have `updated_at` > 1 day after `created_at`: rows are revised
in place and first-published values are not preserved. (A large share of
those updates is the 2026-06 scale-bug reprocessing of 34,559 rows, but
that cannot be separated after the fact — which is itself the problem.)
Mitigations: (a) `raw_xbrl_facts` jsonb + immutable exchange filings allow
re-derivation of first-published values where needed; (b) **event
timestamps come from `earnings_surprises.announced_at`** (present
throughout), not financial_results.`announced_at` (100% only for the
latest quarter, ~55% before); (c) Stage 4's `expectations` table is
append-only by design; (d) RECOMMEND a going-forward first-parse snapshot
(part of the Stage-4 migration proposal). The `consolidated` flag is
healthy: Q1 FY27 has 2,072 consolidated vs 1,824 standalone reporters —
E2 basis-matching is feasible.

## Item 5 — News-reported consensus (estimator #7)

Thin. 94 pattern-matched items in 180 days across the wire sources
(ET/PTI/Mint/Reuters etc.), and sampled items are macro/sector previews
(RBI MPC, FMCG sector preview), not company-level "street expects ₹X cr"
figures. Wire items also lack reliable symbol tagging
(`symbols_mentioned` effectively empty outside filings). **Defer to v1.1**;
sector-level preview content may later inform sector expectations instead.

## Item 6 — Benchmark series + sector map

**Benchmarks: better than expected.** In `price_points`: NIFTYBEES,
JUNIORBEES, BANKBEES from 2017-11; **MID150BEES from 2022-05** (fully
covers the 8-quarter backfill window); sector ETFs for bank/IT/pharma/PSU
bank/infra; SENSEXBEES only since 2026-06. **Recommended abnormal-return
model: market+size blend (NIFTYBEES + MID150BEES/JUNIORBEES), sector-ETF
adjustment only where an ETF exists.** ETF tracking error accepted and
disclosed.

**Sector map: a real gap.** `companies.sector_nse` filled for only
750/3,998 (18.8%); `industry_nse` 0%. Consequences: estimator #4 (peer
read-across) and broad sector adjustment need a sector-map build (small
Stage-2 task, or #4 drops per the degradation path).

## Item 7 — Survivorship

**Confirmed.** Only 25 of 3,923 symbols have a last price before 2026 — a
decade of Indian delistings/suspensions is absent from the corpus, so
backfilled drift statistics are survivorship-flattered (including,
partially, PEAD's own t=4.7). Handling: treat 2025–2026 as the
near-complete window (BSE-only names only joined 2026-07), report
backfill-era results with the flag attached, disclose prominently in Paper
A limitations, and consider delisted-history ingestion as future work.

## Bonus verification

`earnings_surprises` spans 2020→present (~24 quarters; 5.0k–7.1k
events/year; 7,026 already in 2026) with `announced_at` populated — the
event clock and the ≥8-quarter backfill are both supported.

## Addendum (2026-08-15, same day)

Founder proposed Angel One IV ingestion for the F&O universe. Follow-up
verification: **NSE F&O bhavcopy (UDiFF format) returns HTTP 200 on the
403-free nsearchives host from this VM** — historical option settlements
are fetchable, so IV is *reconstructable*, not only capturable. Item-1
verdict amended from "OUT for v1" to **capture-now / validate-later**:
Track A (live pre-close IV snapshots via Angel One optionGreek — small
build, start early, data is irreplaceable) + Track B (bhavcopy
reconstruction, post-M3 only). See spec §3 estimator #5 and §12 Stage 2/2b.

## Consequences for the spec (v1 estimator roster)

#1 guidance (509 cos) · #2 business updates (~100 cos/qtr) · #3 statistical
(universe) · #6 run-up (universe). #4 conditional on the sector map;
#5 out; #7 deferred. Tier reality: T1∪T2 ≈ 550–600 companies — comfortably
enough for the within-company Stage-5 test, and honestly small enough that
the statistical tier carries the tail, exactly as designed.
