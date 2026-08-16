# Expectations & Surprise Engine (E-series) — Project Spec

**Date:** 2026-08-15 · **Status:** DRAFT v2 (amended same day after CFA-style
review: §3 E3 blocks, §5 within-company test + trade-space endpoint, §6
success commitments, §10 tradeability/survivorship, §11–12 staging) ·
**Owner:** Aviral
**Codename:** E-series (E0–E5). Phase 1 of the direction-signals program.

---

## 0. One-paragraph summary

Build an internal expectations engine for every listed Indian company, use it
to compute *true* surprises at disclosure time (seconds after the filing, not
before it), map surprise × market-state to a **calibrated distribution** of
forward returns, and emit directional signals **only** inside a measured
competence region (selective prediction with abstention). Evaluated
pre-registered against the composite-SUE baseline. Two publishable research
outputs fall out of the build regardless of whether the trading edge
materializes.

**Dual goal:** (a) startup — the signal layer PEAD proved the market leaves
on the table, harvested with the latency/dual-exchange infra we already have;
(b) research — "implied expectations for uncovered equities" and a
guidance-fulfillment dataset, both novel for any market and unbuildable
without Taperead's corpus.

---

## 1. Thesis and evidence base

1. **The Indian market underreacts to scheduled disclosures.** Composite-SUE
   PEAD: D10−D1 +2.11% @ 20 trading days, t=4.7 over 51 cohorts (validated
   2026-07-04). The drift is the proof that directional information survives
   the announcement.
2. **Raw LLM direction is a coin flip** (signal audit 2026-07-04) — but every
   failure postmortem identified a *missing input*, not missing capability:
   POLYCAB = no expectations model; HYUNDAI = no guidance memory; SWSOLAR =
   no scenario rubric. The coin flip was an architecture result.
3. **The tradeable act is not pre-event prediction.** Expectations are priced
   in before the event; the edge is (i) judging the surprise correctly
   *faster than the tape digests it* (BSE leads NSE 2–50 min; press-release
   PDF leads XBRL by hours; our 5s latency work) and (ii) riding the
   documented multi-week underreaction. Phase 1 never takes pre-event
   positions.
4. **The edge, if it exists, is largest where price aggregates the least
   analysis:** the ~1,631 BSE-only names and uncovered NSE small/midcaps with
   zero sell-side coverage — precisely where no licensed consensus exists and
   where our derived consensus is the only one.

## 2. Scope

**IN (Phase 1):** scheduled, periodic disclosures — quarterly results,
earnings calls, provisional/monthly business updates — for the full NSE+BSE
universe (3,998 companies; 3,923 with price history 2017-11→present).

**OUT (Phase 2, explicitly deferred):** unscheduled/macro events
(geopolitical shocks, commodity moves → exposure propagation). Rationale:
tiny event counts make statistical validation take years, and
`company_relationships` (the propagation graph) is still unbuilt. Phase 2's
near-term framing stays "exposure mapping with human judgment," not
validated signals.

**Non-goals:** pre-event positioning; customer-facing signal surfaces
(RA-compliance Lane A stands); replacing the existing pilot strategies.

## 3. Architecture (E0–E5)

```
filing/transcript ──► E0 extract ──► E2 surprise ──► E4 reaction model ──► E5 gate ──► signal / ABSTAIN
                          ▲              ▲                 ▲
                    E1 expectations   E1 output       E3 state/context
                    (built BEFORE     (expected      (positioning, run-up,
                     the event)        values)        ASM/GSM, liquidity)
```

### E0 — Extraction (exists)
Facts, numbers, guidance from filings/PDFs/XBRL/transcripts. Already live:
dual-exchange filings, XBRL chain, results-PDF parsers, transcript stack,
scenario rubrics + nightly accuracy eval. Distillation to a small model is a
**later cost optimization**, not on this critical path.

### E1 — Expectations engine (THE core build)
Per company × quarter, computed **before** results, from an estimator stack
ordered by universe coverage:

| # | Estimator | Coverage | Source | Status |
|---|-----------|----------|--------|--------|
| 1 | Management's own prior guidance | ~774 cos and growing | `guidance_ledger` (5,735 rows) | ledger EXISTS; `guidance_outcomes` ~empty (5 rows) — fulfillment labeling is the build |
| 2 | Provisional/monthly business updates | ~100–110 cos/qtr (Stage 0 headline floor) | filings stream | GO — Stage-2 body/attachment classifier should lift the floor; see STAGE0_DATA_REALITY_2026-08-15.md |
| 3 | Statistical baseline (seasonal panel model) | 100% | `financial_results` (2017→current) | build; composite-SUE's seasonal-naive is the floor it must beat |
| 4 | Peer read-across (early reporters shift late reporters) | sector-dependent | results + sector map | **CONDITIONAL** (Stage 0): `sector_nse` only 18.8% filled — needs a sector-map build, else drops per degradation path |
| 5 | Options-implied magnitude (straddle) + skew | F&O only (~226 underlyings) | two tracks (below) | **CAPTURE-NOW / VALIDATE-LATER** (amended 2026-08-15 after founder proposal): Track A = daily pre-close IV/straddle snapshots via Angel One optionGreek for names with upcoming results (shared-session pattern; endpoint documented, verify against live session; new table = migration approval) — build early, every uncaptured season is gone forever. Track B = historical reconstruction from nsearchives F&O bhavcopy (UDiFF format **verified HTTP 200** 2026-08-15) via BS inversion of near-ATM settlement prices — post-M3 only; EOD-settlement IV, stale-strike filters required. #5 joins validation once either track yields ≥1 season |
| 6 | Pre-event run-up (revealed expectation + surprise-theft correction) | 100% | `price_points` | trivial; already a materiality feature |
| 7 | News-reported consensus ("street expects", agency/TV polls in earnings previews) | thin | already-ingested news flow | **DEFERRED to v1.1** (Stage 0): 94 items/180d, macro/sector previews not company figures, no symbol tagging on wires |

**Data-provenance policy (decided 2026-08-15):** NO scraped aggregator or
licensed consensus data, even strictly internal — internal use does not cure
the licensing problem, it would fail RA/fundraise diligence, and it would
taint Paper A's consensus-free clean-room claim (the paper's core novelty).
Permitted: estimator #7 (news-reported figures from our own ingested flow);
broker research collected only from the publishing broker's own public
pages, with per-document provenance; and, later, a licensed estimates-only
tier (ACE Equity, currently ON HOLD) used strictly as an E1 calibration
benchmark at M3 — never as a pipeline input.

Output per company-quarter: expected revenue / EBITDA / PAT (consolidated
and standalone kept distinct), an uncertainty band, an estimator-provenance
vector, and a **coverage tier** (T1 guidance+updates … T4 statistical-only).
Every value stored point-in-time (`computed_at`), never revised in place —
look-ahead is the classic way this dies.

### E2 — Surprise computation
Standardized surprise vs the E1 composite (generalizing composite-SUE):
revenue/EBITDA/PAT surprises, **margin surprise** (the residual signal when a
business update already revealed revenue), and **guidance-delta** (new
guidance vs prior ledger entry — the HYUNDAI mirror). Where E1 has only the
statistical tier, E2 degrades gracefully to today's SUE.

### E3 — State/context (conditioning features — never standalone signals)
Three blocks. E3 features CONDITION the reaction function; none may gate or
veto on its own — they enter E4 and live or die by walk-forward ablation.

**Positioning/ownership** (all deployed): SHP named-holder deltas, FII/MF
flows (migration 021), bulk/block deals ex-churn (022), ASM/GSM state,
liquidity/float. Encodes the POLYCAB mechanic: crowded ownership × high
run-up × merely-good print ⇒ negative reaction function.

**Price/volume state** (amended 2026-08-15, technicals review): a compact,
economically-motivated set. An indicator earns a slot only if it (i) names
a mechanism — attention, anchoring, positioning, liquidity, uncertainty —
not a chart pattern; (ii) is orthogonal to features already here
(correlation-checked); (iii) is computable point-in-time from ingested
data; (iv) survives walk-forward ablation. Current roster:

*Price:* distance from 52-week high/low (anchoring; documented interaction
with post-earnings drift), 6-month momentum (skip-month), price vs 200d MA
(ONE trend-state flag), 5d run-up σ (exists), vol60 percentile (exists),
pre-event range compression (ATR20 percentile — uncertainty coiling; the
non-F&O substitute for the options straddle).

*Volume/participation* (the strongest family for THIS thesis — drift =
information-processing scarcity, and these measure it directly):
announcement-day abnormal volume vs trailing average (high-attention events
incorporate fast → less drift; quiet surprises drift longer — the
limited-attention literature), Amihud illiquidity percentile (|ret|/turnover
— price impact per rupee; conditions drift speed AND feeds the ADV cost
model), circuit/limit-day frequency (India-specific: band-hit history from
OHLC + ASM/GSM state — flags retail-chase names and predicts fill failure),
delivery-% shift on event day (India-specific conviction measure —
delivery-based buying vs intraday churn; requires small
nsearchives-bhavcopy ingestion → v1 only if trivial, else v1.1).

Explicitly EXCLUDED with reasons: EMA/MA ladders and MACD (collinear
smoothed prices — parameters without information), RSI/stochastics
(spanned by vol-scaled run-up), support/resistance, Fibonacci, pivots,
chart patterns (no mechanism, no robust evidence — data-mining surface).
Sector-relative strength is intentionally absent: the abnormal-return model
already sector-adjusts; adding RS would double-count.

**Macro/market state** (amended 2026-08-15, macro review): observable
regime, never forecasts — NIFTY/smallcap-index 20d trend + vol regime,
India VIX percentile, FII/DII net-flow direction (participant-OI sync
exists), 10y G-sec yield change (banks/NBFCs), USDINR trend (exporters),
relevant commodity trend (commodity feeds exist). Market DIRECTION is
neutralized by hedging, never predicted. Sector-level nowcasts (credit
growth, Vahan, GST) are estimator #8, v1.1, post-M3 only.

**Feature budget:** E4 trains on the FULL event cross-section (~15k events
over an 8-quarter backfill — the gate applies after prediction, not to
training), so ≥30 events/feature is comfortably met at the current roster
(~25 features). The budget still binds: every new feature displaces or
justifies itself via its Stage-6 ablation delta, and tail-segment
performance (where events are scarce) is reported separately so features
cannot hide behind largecap fit.

### E4 — Calibrated reaction model
Input: E2 surprise vector × E3 state. Output: **distribution** over abnormal
returns at 1d / 5d / 20d — magnitude first (materiality-calibrator lineage:
magnitude is far more learnable than sign), direction conditional on
magnitude. Trained walk-forward only. Gradient-boosted trees on tabular
features + calibration layer; no LLM in this layer (LLMs feed E0/E1
extraction, not return prediction). Performance is reported by regime slice
(market trend/vol state): a model trained on ≤8 quarters must prove it is
not a one-regime artifact.

### E5 — Abstention gate
Emit long/short only when calibrated P(direction) and expected |move| clear
pre-registered thresholds; otherwise ABSTAIN (the majority outcome by
design). Every abstention is a recorded counterfactual (the papertrade
gated-signal audit pattern, deployed 2026-08-15). Headline metric is the
**coverage–accuracy curve**, never raw accuracy.

## 4. Data inventory (verified 2026-08-15)

- `earnings_surprises`: ~1,800–1,900/qtr historically; **3,218 in Q1 FY27**
  (dual-exchange boost), 90–97% with `sue_composite`.
- `price_points`: 2017-11-28 → 2026-08-14, 3,923 symbols.
- `companies`: 2,367 NSE + 1,631 BSE.
- Guidance stack: `concall_transcripts` 1,037 (876 cos), `transcript_guidance`
  5,854 (785 cos), `guidance_ledger` 5,735 (774 cos), `guidance_outcomes`
  **5 rows — the gap E1 fills first**.
- Landmines to verify before relying on (= §12 Stage 0): `derivatives_eod`
  (options leg); business-update real coverage (naive pattern undercounts);
  consolidated-vs-standalone flag reliability in `financial_results`;
  **index/sector benchmark series** (required to define abnormal returns —
  NIFTYBEES is the only proxy confirmed ingested); **survivorship** (how far
  back the corpus includes delisted/suspended names — uncorrected, it
  flatters long-side drift, including part of PEAD's own t=4.7).

**New storage (migration-gated, needs explicit approval per house rules):**
`expectations` (company, period, metric, value, band, tier, provenance,
computed_at), `business_updates`, extensions to `guidance_outcomes`
(fulfillment label, realized value, judged_at). All append-only, PIT-stamped.

## 5. Validation plan — every layer independently testable

**Principle:** no layer waits for the trading result to know if it works.
Fixed seeds (np.random.seed(42)) on any sampling; all evals walk-forward.

- **Abnormal-return model (frozen before Stage 5):** market-adjusted vs a
  smallcap benchmark, plus sector-index adjustment where a sector map
  exists; thin-trading beta bias acknowledged for microcaps. Stage 0
  verifies which index/sector series exist to support this.
- **E1 (the go/no-go test, run FIRST) — WITHIN-COMPANY design** (amended
  2026-08-15: comparing T1 companies against T4 companies conflates
  estimator quality with company mix — guidance-givers are systematically
  larger and better covered). On T1/T2 names, does the guidance/updates
  estimator beat the statistical estimator FOR THE SAME FIRMS at predicting
  announcement reactions? Metrics: sign accuracy on |abnormal reaction| >
  1σ, rank-IC of (actual − expected) vs reaction, band calibration.
  **Kill criterion: if the text-derived estimators don't beat the
  statistical estimator within-company by a pre-stated margin, the thesis's
  foundation is wrong — stop and publish the negative result.**
- **E4:** Brier score / calibration curves vs the materiality calibrator as
  baseline; magnitude before direction; regime-sliced reporting.
- **E5:** coverage–accuracy curve dominance over the unconditional model.
- **Trading (pre-registered, Q2 season):** ONE primary endpoint —
  incremental 20-day **trade-space** spread over composite-SUE at the gated
  coverage level (the S0 lesson: close-to-close stats include untradeable
  overnight gap). All other horizons/metrics are secondary. **Baseline
  parity rule:** composite-SUE is measured under the identical universe,
  cost model, and trade-space rules, or the incremental claim is rigged in
  our own favor. **Cost model:** ADV-bucketed slippage (flat 10bps is
  fantasy in the tail — 50–200bps is realistic for microcaps), a
  position-size-vs-ADV cap, and circuit-day no-fill handling (the pilot's
  print-gated fills already enforce this at execution). Cohort-level
  t-stats; the hardened pilot (2026-08-15 deploy) is the harness.

**Sample-size honesty (MDE):** ~1,900–3,200 events/season; assume σ(20d
abnormal) ≈ 10% for small/midcaps. Ungated (n≈3,000): detectable edge ≈
0.37% at t=2 in one season. Gated at 15% coverage (n≈450–480): SE ≈ 0.46%,
**minimum detectable edge ≈ 0.9–1.0% per event in one season; two seasons
for ~0.65%.** Anything smaller than that cannot be confirmed by December —
pre-commit to multi-season evaluation rather than declaring early.

## 6. Pre-registration protocol

A separate `PREREG_E_SERIES_<date>.md` frozen **before 2026-10-09** (Q2
season start), containing: event classes, E5 thresholds, holding windows,
the abnormal-return model, baseline definition + parity rules, ADV-bucketed
cost assumptions, THE single primary endpoint (everything else explicitly
secondary), MDE-derived minimum run length, kill criteria, **and success
commitments** — what a passing result authorizes (scale-up rules, capital,
next phase), so goalposts cannot move in the favorable direction either.
Amendments allowed only before the season
opens; mid-season changes create a new (clearly-labeled) exploratory arm.
This mirrors the paper-trading pilot discipline — it is the only reason the
coin-flip and PEAD results are trustworthy, and reviewers will treat it the
same way.

## 7. Milestones (solo founder + Claude; results season anchors)

| # | When | Deliverable | Gate |
|---|------|-------------|------|
| M1 | → Sep 05 | Guidance-fulfillment labeler (backfill `guidance_outcomes` for FY26 quarters); business-update detector + parser; statistical baseline model | ledger labels ≥60% of T1 companies |
| M2 | → Sep 20 | E1 composite + `expectations` table backfilled ≥8 historical quarters, PIT-clean | — |
| M3 | → Oct 03 | **E1 validation report** (the go/no-go) | E1 kill criterion evaluated |
| M4 | → Oct 09 | E2/E3 features, E4 trained walk-forward, E5 thresholds; **pre-registration frozen**; papertrade strategies `esurprise_long` / `esurprise_short` wired (shadow + gated live-paper) | prereg doc committed |
| M5 | Oct 13 – Nov 14 | Q2 FY27 season live: shadow on all events, paper trades inside the gate | hardened pilot is the harness |
| M6 | → Dec 05 | Season readout vs prereg; decision: iterate / scale / kill | — |
| M7 | Dec–Jan | Paper A submission; Paper B if warranted | see §8 |

Effort honesty: M1–M2 is the bulk (ingestion + labeling). E4/E5 are small
models on tabular features. If M1 slips past mid-Sep, cut estimator #5
(options) and #4 (peer read-across) from v1 — the stack degrades gracefully.

## 8. Research outputs

**Paper A — "Implied expectations for uncovered equities"** (primary; safe to
publish regardless of trading outcome). Contribution: the estimator stack for
consensus-free markets + guidance-fulfillment dataset (774+ companies,
labeled) + validation against announcement reactions across thousands of
Indian events. No open dataset with fulfillment labels exists in any market.
Venues: NeurIPS D&B / EMNLP-FinNLP / CLEF FinMMEval shared-task proposal.
Dataset release as labels + document IDs/URLs + fetch script (EDGAR-style),
never raw exchange PDFs — resolve NSE/BSE redistribution terms before
freezing the release format.

**Paper B — pre-registered incremental-alpha test** ("Do text-derived
expectations add drift capture beyond composite SUE in Indian equities?").
Publishable in either direction *because* it is pre-registered. Publish the
phenomenon and aggregate effect sizes only.

**Stays private:** E5 thresholds, composite estimator weights, execution
logic, anything that is edge rather than science.

## 9. Compliance guardrails

All E-series outputs are internal-only (Lane A posture): papertrade + internal
research surfaces gated by INTERNAL_RESEARCH_EMAILS; nothing reaches customer
surfaces. Any external publication (papers, dataset) gets a compliance pass
against the SEBI RA posture before submission — expectations/extraction
research is clean; signal-performance claims need the Lane A lens.

## 10. Risks

- **Tail tradeability / capacity** (top economic risk, amended 2026-08-15):
  the thesis lives in BSE-only/SME/microcaps — circuit bands (5/10/20%),
  ASM/GSM trade-for-trade settlement, tiny ADV. Upper-circuit locks can make
  the long UNEXECUTABLE on exactly the best events; with no derivatives and
  illiquid SLB, the short side is intraday-cash-only in the tail — a
  structural long/short asymmetry. Strategy capacity is small (a few crore
  at best in the tail); the startup value is the signal product, not a
  fund. Stated here so nobody discovers it in December.
- **Survivorship** (amended 2026-08-15): the corpus reflects the current
  universe; delisted/suspended names are likely absent, flattering
  long-side drift. Stage 0 quantifies completeness; strong claims restricted
  to the complete window; disclosed in Paper A limitations.
- **Look-ahead leakage** (top methodology risk): enforced by PIT stamps on every derived
  row + the XBRL-lag rule (surprise rows lag filings by hours — E-series
  events key on *filing* arrival, entries on surprise-row arrival, exactly
  as the pilot does today).
- **Business-update coverage disappoints** → T2 shrinks; the stack degrades
  to guidance + statistical; validation slices by tier will show it honestly.
- **Options leg unavailable** (`derivatives_eod` empty) → drop estimator #5;
  F&O names are the covered end anyway — the thesis lives in the tail.
- **Multiple-testing temptation** post-M3 → prereg freeze is the control.
- **Solo-founder bandwidth** → M1 timebox + the §7 degradation path; the
  benchmark/dataset work (Paper A) shares ~80% of its labor with M1–M2, so
  research is not additive cost.
- **Edge too small to detect in one season** → MDE math in §5 pre-commits us
  to multi-season patience instead of December over-claiming.

## 11. Immediate next actions = Stage 0 (read-only, no approvals needed)

1. `derivatives_eod` / options data reality → decides estimator #5.
2. True business-update coverage: enumerate headline/category/attachment
   patterns across Q1-FY27 filings, by update type and company count.
3. `guidance_ledger` quality audit: ~50-entry sample vs source transcripts —
   what % carry a quantitative, period-bound, scoreable target? (Sizes the
   real T1.)
4. `financial_results` PIT-safety: are first-published values preserved with
   timestamps, or revised in place? Consolidated/standalone flag
   reliability.
5. Estimator #7 probe: extractable "street expects" figures in the ingested
   news flow — covered-name count.
6. Index/sector benchmark series inventory → feeds the abnormal-return
   model decision (§5).
7. Survivorship: how far back the corpus includes delisted/suspended names.

**Exit gate:** a data-reality report; go/no-go on estimators #5 and #7 for
v1; abnormal-return model options laid out.

## 12. Staged execution plan

Critical path: **Stage 0 → 1 → 4 → 5**; everything else can flex or drop
(§7 degradation path). Every stage ends with something true and checkable —
a coverage number, a labeled dataset, a passing leakage audit, a frozen
document — not "progress."

| Stage | What | Key steps | Exit gate / approval |
|-------|------|-----------|----------------------|
| **0** | Ground-truth verification (1–2 days, read-only) | the seven §11 items | data-reality report; #5/#7 go/no-go. No approvals. |
| **1** | Guidance-fulfillment labeling (M1a) | outcome schema + matching rules (beat/met/missed/unverifiable/withdrawn; prefer unverifiable over wrong); labeler = deterministic matching + LLM assist + arithmetic cross-checks; FY26 backfill; QA on stratified 100-sample ≥95% | **migration approval** (`guidance_outcomes` extension); gate: ≥60% of T1 labeled. Output = Paper A dataset core. |
| **2** | New expectation sources (M1b, parallel) | business-update detector + parser → `business_updates`; sector-map build (unblocks estimator #4); **Track A IV capture job** (Angel One optionGreek snapshots for names with scheduled results — small, clock starts at deploy) | **migration approvals**; coverage report per source |
| **2b** | IV history reconstruction (Track B — POST-M3 only) | nsearchives F&O bhavcopy 2020→present (UDiFF verified reachable) → near-ATM stock-option settlements → BS-inverted IV → pre-event implied move + skew; stale-strike filters + QA | only if M3 passes; upgrades #5 into backfill validation for the F&O subset |
| **3** | Statistical baseline (M1c, small) | seasonal panel model, fixed seed; backtest 2019→2026 | must beat seasonal-naive or E1's floor is broken |
| **4** | E1 composite + `expectations` table (M2, → Sep 20) | schema **migration approval**; precedence/weights, bands, tiers T1–T4; ≥8-quarter PIT backfill via as-of joins; **automated leakage audit** (every input timestamp < announcement) | leakage audit passes |
| **5** | E1 validation — the go/no-go (M3, → Oct 3) | metrics/thresholds frozen BEFORE running; **within-company** test (§5); regime + tier slices; optional ACE calibration benchmark (provenance policy: benchmark only, never input) | **founder decision:** proceed / stop-and-publish-negative |
| **6** | E2/E3/E4/E5 build (M4a) | surprise vector (margin surprise, guidance-delta); E3 three blocks incl. price-state + macro-state, feature budget ≥30 events/feature, ablation report; E4 walk-forward + calibration; E5 thresholds from validation folds only | ablation + calibration report |
| **7** | Prereg + wiring (M4b, **frozen Oct 9 — hard date**) | `PREREG_E_SERIES` per §6 (single primary endpoint, parity rule, ADV-bucketed costs, success commitments); wire `esurprise_long/short` into hardened pilot (shadow + gated) | **commit/deploy approval**; prereg committed |
| **8** | Q2 season run (Oct 13 – Nov 14) | shadow-score every results event; weekly health readouts (data_quality block); zero mid-season changes; incidents logged, not patched | — |
| **9** | Readout + decision (→ Dec 5) | analysis strictly vs prereg: primary endpoint, realized coverage–accuracy, calibration drift, regime slices | **founder decision:** iterate / scale / kill — criteria pre-written |
| **10** | Papers (Dec–Jan) | Paper A: dataset freeze → exchange-terms check → write → submit (with/without-#7 results per §8); Paper B only on a clean pre-registered season result, either direction | compliance pass (§9) before submission |
