# Stage 1 — Guidance-Fulfillment Labeling: Findings & Fork

**Date:** 2026-08-15 · **Scope:** §12 Stage 1 read-only phase · **Method:**
schema/code audit + full scorer dry-run against production (zero writes).

## Headline: the labeler already exists, is correct, and is caught up

The C0 commentary build went further than recorded. The full loop is live
in `kb/sync/guidance_ledger_sync.py` (migration 034 applied):

- **Normalization:** 5,854 `transcript_guidance` rows → metric/period/unit
  keys; lakh/mn/bn → crore; 1,513 scoreable (25.8%).
- **Lifecycle:** 4,850 active / 1,004 superseded; change verdicts
  recomputed (raised 222 / lowered 305 / maintained 260 / changed 217) —
  the E2 guidance-delta signal already has data.
- **Scoring:** candidates = active + scoreable + period-elapsed → resolves
  actuals from `financial_results` (consolidated preferred,
  `standalone_basis` flagged; EBITDA derived + flagged; YoY needs full 4+4
  quarters or refuses; H1/H2/month periods refused in v1), verdict
  above/in/below + `met` by better-direction, ambiguity flags preserved.
- **Dry-run 2026-08-15:** 6 candidates, 5 scored — e.g. INDHOTEL rev-growth
  guided 12% → 14.6% (met); SURYAROSNI EBITDA guided ₹120–125cr → ₹112.3
  (miss). The 6th refused rather than guessed. **There is nothing left to
  label from current data.**
- A **credibility multiplier** (companies with ≥2 resolved outcomes) exists
  and is dormant awaiting data.

**No migration is needed.** Stage 1's "build the labeler" is done.

## Rules audit (vs the Stage-1 design checklist)

| Rule | Status | Note |
|---|---|---|
| Prefer-unverifiable-over-wrong | ✅ | unresolvable → skipped, never guessed |
| Basis matching | ✅ | consolidated preferred, standalone flagged |
| Unit normalization | ✅ | crore-normalized with flags |
| Tolerance bands | ⚠️ strict | knife-edge misses labeled `below`; **non-blocking** — `delivered` + guided band are stored, so any tolerance can be applied at analysis time without relabeling |
| Superseded items | ⚠️ not scored | only `active` items score; "was the ORIGINAL promise kept" (Paper A angle) would need scoring superseded items too — analysis-layer choice, schema supports it |
| Period types | ⚠️ FY/Q only | H1/H2/month refused (v1) — part of why only 71% of items are dated |
| Auto-cadence | ❌ missing | scorer has been run MANUALLY (scored_at: Aug 3/7/13/14); no kb-monitor wiring — small build + deploy approval |

## The binding constraint: transcript history, not tooling

Transcripts span **2026-05-10 → 2026-08-15 only** (1,037 docs). Guidance
stated May–Aug 2026 targets FY27/FY28, so elapsed-period candidates ≈ 6.
The spec's "backfill FY26 quarters" is impossible from current data —
there are no FY26-era transcripts in the corpus.

Accrual timeline if we only wait: a small tranche at Q2 FY27 results
(Oct–Nov 2026: Q2/H1-targeted items), the main tranche at FY27 annuals
(Apr–May 2027). Consequences: the ≥60%-of-T1 gate is unreachable this
year; Paper A's dataset reaches scale ~mid-2027; the credibility
multiplier stays OFF through the Q2 season.

## The fork (founder decision)

**Option A — accrue naturally.** Zero cost. Stage 1 reduces to wiring the
auto-scoring cadence; the dataset and credibility feature arrive ~mid-2027.

**Option B — historical transcript backfill (RECOMMENDED).** SEBI has
mandated concall-transcript disclosure since 2021; FY25–FY26 transcripts
sit in exchange announcement archives (BSE history API endpoints are
documented and previously verified; NSE archives as fallback). Build a
backfill fetcher → run the EXISTING C0 extraction → ledger → scorer labels
immediately against results we already hold. Rough scale: ~876 relevant
companies × up to 8 quarters, realistically ~3–5k transcript PDFs.
Estimated cost: 1–2 days fetch-pipeline build + LLM extraction spend on
the order of $200–400 (Haiku/Flash-class at ~40–50k tokens/transcript),
plus approvals (new ingestion writes → migration-free but prod-touching).
Unlocks THIS cycle: Paper A's dataset at scale (thousands of labeled
promises), fulfillment base-rates ("do Indian managements deliver?"), and
the credibility multiplier live for the Q2 season — a real E1 feature.

**Why B is worth it:** Stage 1's entire value was the labeled dataset; the
labeler being pre-built means the marginal cost of the dataset is one
fetcher plus a few hundred dollars of extraction. This is the
highest-value-per-rupee step in the whole program.

## Addendum — Option B feasibility probe (2026-08-15, founder approved B)

All read-only, all confirmed:

- **Where transcripts live on BSE:** `Company Update → Analyst / Investor
  Meet` (no dedicated Transcript subcategory). Named-subcategory queries
  accept multi-day windows; `Table1.ROWCNT` sizes a window without paging.
- **Volume calibration:** May-2025 window = 3,040 subcategory rows, ~10%
  transcript-headline share on sampled pages; Jul-2024 = 1,159 rows, ~6%.
  FY25+FY26 estimate: **~3–6k transcript PDFs**, enumeration ≈ 1,000 API
  calls (~25 min at polite pacing).
- **PDF retrieval:** archived attachments serve from `AttachHis` (verified
  HTTP 200, application/pdf, on a May-2025 transcript).
- **Ingestion contract is reusable as-is:** `transcript_sync`'s queue is
  `news_items` rows with transcript headlines (URL → PDF). The backfill is
  ONE new module — an archive walker that pages the subcategory, filters
  transcript headlines, maps SCRIP_CD → symbol, and inserts
  properly-timestamped `news_items` rows (idempotent ids; direct table
  inserts do NOT touch Kafka, so the analysis pipeline stays asleep). The
  existing chain (transcript_sync → structure → guidance extraction →
  ledger sync → scorer) then runs unchanged.

**Enumeration results (final, both subcategories walked 2026-08-15/16):**
the dedicated subcategory is `Company Update → Earnings Call Transcript`
(discovered mid-probe via a misfiling-apology headline; the first walk of
Analyst / Investor Meet caught only the ~15% who misfile). Combined:

- Earnings Call Transcript: **7,496 candidates → 7,036 mapped, 1,135
  companies** (460 unmapped scrips)
- Analyst / Investor Meet: 1,179 candidates → 1,103 mapped, 335 companies
- **Total ≈ 8,675 candidates / ~8,139 symbol-mapped transcripts**, clean
  results-season rhythm (May/Aug/Nov/Feb: 620–870/month), Apr-2024 →
  May-2026, zero overlap with the live corpus (starts 2026-05-10).
- Manifests: `scratchpad/bse_transcripts_ect.jsonl` + `bse_transcripts.jsonl`.

**Approvals required before execution:** (1) prod DB writes (~8.1k
news_items inserts + the downstream chain's writes), (2) LLM extraction
spend **est. $490–980** (structure + guidance passes over ~8.1k
transcripts). Throughput note: at current LLM rate limits the extraction
trickles over ~1–3 days — start soon and it finishes well before the M1
gate (Sep 05). ~1,135 companies × ~6–7 transcripts each also makes the
credibility multiplier (≥2 resolved outcomes) broadly attainable.

## Next actions

1. **Founder:** choose A or B.
2. Either way: wire auto-scoring (nightly during results season, after the
   results sync) into kb-monitor — needs commit/deploy approval.
3. If B: feasibility probe of the BSE announcement-archive transcript
   category (read-only), then the fetcher build + extraction run
   (LLM-spend approval), then `--execute` scoring backfill.
4. Analysis-time decisions (no schema change): tolerance bands for
   met/miss reporting; whether Paper A scores superseded (original)
   promises.
