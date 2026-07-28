# Specialist Decision Tree — Filing Impact & Direction Classification

> **STATUS: SUPERSEDED IN PART — see §8 "SOTA verdict" (2026-07-28,
> post adversarial review).** The tree skeleton (§2 router/contract, §4
> eval discipline, §6 data unlocks) survives review; the node design and
> promotion protocol are revised by §8 before any build starts.

Designed 2026-07-28 from a three-agent reconnaissance of the live pipeline,
the data landscape, and a quantified failure map (all numbers below are
measured, read-only, seed-42 where sampled; sources: pipeline code,
exports/materiality_lab/, sync_log nightly evals, dataset_400d_v2 labels).

## 1. Thesis, and the two facts that constrain the design

**Why specialize:** the general classifier's quality is violently
type-skewed. Materiality-gate AUC by filing type runs from 0.925 (rating)
down to 0.487 (fund_raise — a coin flip); direction runs from +32.2pp over
drift (results-negative) to −13.9pp (procedural-positive). One prompt
cannot hold rubrics for bank NPAs, order-book math, dilution mechanics and
ratings watchlists at once — and measurably doesn't.

**Constraint 1 — specialize prompts, not models.** E7 (2026-07-28): a
results-only retrained ML specialist scored AUC 0.6138 vs the global
model's 0.6749 *on the same rows*. At ~150 labeled events/day (label
history begins 2026-05-10 — only ~77 days exist), per-cell ML starves.
Therefore: nodes are **specialized prompts + specialized grounding bundles
+ node-specific output extensions**, running through the SAME stage-3
call and the SAME global materiality gate. Per-cell ML revisits
post-November when labels roughly double.

**Constraint 2 — sector routing is data-limited to financials today.**
`companies.sector_nse` covers 18.8% of actives; 58% of filing flow is
NULL-sector. Only Financial Services cells clear 100 events/60d — and
financials have a sector_nse-INDEPENDENT router key:
`financial_results.taxonomy` (42 banks / 348 NBFCs / 11 insurers,
exchange-derived, current). So L2 launches with ONE sector overlay
(financials), and the §6 sector backfill unlocks the rest (~2.4× every
named cell).

## 2. The tree

```
filing ──► L0 ROUTER (deterministic-first, auditable)
             ├─ regex taxonomy (22 buckets, extends detect_scenario —
             │   first-match-wins, zero cost, already 81%-accurate seam)
             ├─ CONTENT SNIFF: P&L-table / results-content detector that
             │   RE-ROUTES investor_presentation / press_release /
             │   concall / business_update filings carrying results
             │   content to the results node (these cells hide 15-28%
             │   material rates at LOW magnitude today)
             └─ LLM micro-router (flash, cached) ONLY for residual
                 'other' (~16% of flow) — never for a matched bucket
                       │
             L1 CATEGORY NODE (prompt + grounding + extensions)
              results │ order_win │ fund_raise │ rating │ acq_ma │
              litigation │ corp_action │ mgmt_change │ generic
                       │
             L2 SECTOR OVERLAY (composable addendum, not cross-product)
              financials (banking / nbfc / insurance via
              financial_results.taxonomy) — generalizes the existing
              lender_addendum; IT/pharma/industrials overlays gated on
              the sector backfill
                       │
             GLOBAL LAYER (unchanged, protects everything)
              v4.1 materiality gate │ priced_in downgrade │ stage-4/5
```

**Where it plugs in (minimal surgery, proven seam):** stage-3's
`{scenario_block}` injection point (stage3_impact.py:221) and
`scenario_rubrics.py` — today 4 rubrics cover 18.7% of filings; the tree
is that mechanism grown into a registry with routing, grounding fetchers,
output extensions, and versioning. Stage-2's `macro_theme` +
`affected_sectors` are computed before stage-3 and feed the router free.

**Node contract (every node, uniform):**
- `route(filing) -> node_id` — deterministic first, logged.
- `ground(symbol, node) -> context block` — node-specific DB fetches via
  the existing KB service (analysis stays DB-less).
- `prompt(node, context)` — rubric with magnitude anchors REDEFINED for
  the cell + few-shot negative exemplars mined from our own failures.
- Output: the SAME ImpactReport schema (nothing downstream breaks) plus a
  node extensions dict (e.g. `order_value_cr`, `expectations_verdict`)
  and a `classifier_node: {id, version}` provenance stamp (additive
  migration; the STRUCTURE_VERSION re-annotation pattern applied here).
- Deterministic post-math: arithmetic (order/revenue ratio, dilution %)
  is computed in code from extracted fields, never trusted from the LLM.

## 3. Build order (from the failure map — worst measured cell first)

**P1 — results node v2: the expectations problem.** The single biggest
failure: results-POSITIVE calls are anti-signal (median move −0.30%,
44.6% up-share, −7.7pp vs drift, n=316; nightly eval: 45.8% hard-wrong,
NOT fixed by the 07-16 rubric). The KPITTECH (−17% on "positive
outlook"), CIEINDIA/TIPSMUSIC/SRF ("growth sold") exemplars share one
shape: real growth, market wanted more. The node must reason about
expectations, not arithmetic growth:
- Grounding: last 8 quarters (financial_results — 100% complete recent
  columns), SUE decile + percentile (earnings_surprises, 36K rows),
  pre-event run-up in σ units (priced-in proxy), result_events calendar.
- Prompt: magnitude anchors conditioned on surprise-vs-run-up; a print
  that merely meets a run-up is magnitude ≤2 regardless of YoY%;
  mandatory `expectations_verdict` (beat/inline/miss vs implied bar).
- **Positive-direction governor (cross-cutting):** positive sentiment at
  magnitude ≥4 requires explicit expectations evidence; otherwise cap at
  3. Negatives are untouched — results_negative (+32.2pp) is on the
  protect list.
- L2 financials overlay: banking = NII/PPOP/provisions from
  sector_financials JSONB + GNPA%/NNPA%/deposits/advances extracted from
  stored `raw_xbrl_facts` (§6.2 — extraction task, no re-ingest); nbfc =
  credit-cost + loan-book set (already structured, 348 symbols).
- Evaluable NOW: 710 labeled results events (base rate .183), 473 in the
  pinned test split; the nightly eval extends per-node.

**P2 — fund_raise node.** Gate AUC 0.487; magnitude fully INVERTED
(P(material): low .121 > medium .017 > high .048); positive direction
−8.7pp. Rubric = dilution mechanics: issue size vs mcap (96.6% coverage),
instrument (QIP/preferential/rights/warrants/NCD have different
signatures), discount to market, promoter participation, pledge context
(shareholding_patterns now populated). Magnitude anchored on dilution %
and discount, not deal excitement.

**P3 — the re-route family.** investor_presentation (.171 material at
LOW magnitude — 2× base), concall_invite (.145), business_update,
press_release: results content hiding under procedural wrappers. This is
a ROUTER fix, not a new node — the content sniff sends them to P1.
Cheap, high-yield.

**P4 — order_win node v2** (the founder's example, and the user-named
cashflow questions). The gate already ranks it well (0.843) but the LLM
magnitude overcalls (high-band P(material) .063 vs .084 base) while
direction genuinely works (+11.5pp positive — protect). Node extracts:
`order_value_cr`, duration, client type (govt/PSU/private), one-off vs
framework; code computes order/revenue_ttm (computable for 73% of event
companies) and the rubric maps ratio→magnitude bands + margin/cashflow
timeline commentary. Extensions persist → §6.4 builds an order-book
history from our own flow.

**P5 — rating node.** Keyword rule failed (47.4%); needs
watch/outlook/affirmation vs actual up/downgrades distinction + notch
count + secured/unsecured scope grounding.

**Protect list (no specialist may regress these; non-inferiority gates):
results_negative (+32.2pp), order_win_positive (+11.5pp), mgmt_change
(0.773), regulation_generic (0.764), corp_action gate (0.899).**

## 4. Evaluation & promotion (the part that makes this safe)

- **Replay harness, not live A/B:** every historical filing's full body
  is stored (news_items), and 11.5K events carry next-session outcome
  labels. A node candidate replays its cell's history (research-runner,
  ~$2-5 per hundred filings at flash pricing) and is scored against the
  general model on the SAME events: materiality AUC + magnitude-band
  calibration (P(material) monotone in magnitude), direction-vs-sign(z),
  and hard-wrong rate. Time-split, seed 42, pre-registered thresholds
  per node BEFORE the replay runs.
- **Promotion rule (no-improvement rule, node-level):** a node ships only
  if it beats the general prompt within its cell on the primary metric
  and is non-inferior on the protect dimensions. Cells whose labels are
  too thin to judge (business_update n=19 test, litigation n=13,
  insurance-anything) stay on the general prompt — routing to an
  unvalidated node is forbidden.
- **Shadow accrual:** promoted nodes stamp `classifier_node` provenance;
  the nightly 19:05 eval gains a per-node breakdown (today it lives in
  sync_log only — give it a real table in the same migration as the
  provenance stamp). Every node gets the same falsification treatment
  the paper pilot gets: pre-registered, re-scored as labels accrue.
- **Confidence:** `ir.confidence` is 1.0 on every failure exemplar —
  dead signal. Node prompts must emit calibrated confidence per the
  cell's rubric; until then, confidence stays out of routing.

## 5. What this does NOT change

Stage-2 extraction, the global v4.1 materiality gate (it already corrects
much of the magnitude overcalling — AUC 0.84-0.90 on cells whose raw LLM
labels are miscalibrated), priced-in downgrade, stage-4/5, the paper
trader (it consumes the same ImpactReport; better classifications flow
through automatically), and cost (same model tier, same single stage-3
call; the micro-router adds ~$1-2/day on the 'other' residual).

## 6. Data unlocks (ranked by leverage, each its own approval)

1. **sector_nse/industry_nse backfill** for ~3,250 NULL actives (NSE
   master + BSE industry lists). Multiplies every sector cell ~2.4×;
   unlocks IT/pharma/industrials overlays. Prereq for L2 beyond
   financials.
2. **Bank ratio extraction** from stored `raw_xbrl_facts` → structured
   GNPA%/NNPA%/deposits/advances/CAR columns (tags verified present:
   OneD:GrossNonPerformingAssets, OneI:Deposits...). Extraction backfill
   over 1,039 banking rows; feeds the banking overlay. Q1-FY27 rows
   carry fewer tags (142 vs 324) — verify the deposit-tag gap.
3. **Sector index series ingestion** (NSE sectoral indices, archives
   host) — upgrades the LABEL itself to sector-adjusted abnormal
   returns; also feeds priced-in checks. price_points has only 25 ETF
   proxies today.
4. **Order-value persistence**: P4's extracted `order_value_cr` starts
   accruing an order-book history per company from our own flow — the
   dataset for future cashflow-modeling nodes. No external source exists
   to buy; we build it as exhaust.
5. Analyst consensus: schema exists, 0% filled, licensing gate — parked
   (unchanged).

## 7. Rollout

- **Phase A (build):** router module (regex + sniff + micro-router) +
  provenance stamp migration + replay harness + P1 results-v2 node with
  financials overlay. P3 rides the router.
- **Phase B (validate):** P1 replay vs general on 710 labeled results
  events + nightly per-node eval live in shadow provenance. Promote or
  iterate. Then P2, P4, P5 in sequence through the same harness.
- **Phase C (broaden):** sector backfill → IT/pharma/industrials
  overlays; November label-doubling → revisit per-cell ML (E7 rematch)
  and calibrated per-node confidence.
- Every phase: working-tree build → replay evidence → per-instance
  deploy approval. Nothing routes live traffic to a node that hasn't
  beaten the general prompt on replay.

## 8. SOTA verdict (2026-07-28, after a 3-lens adversarial review)

Three independent critics (quant-ML, LLM-systems, market-microstructure)
attacked §1-7 against the raw ledger. Convergent verdict: **the skeleton
is right; the design as written is NOT SOTA, for three structural
reasons — each fixable, two of them cheap.** Full critiques preserved in
the workflow transcript (wf_d53330a0-e71).

### 8.1 The label optimizes an untradeable quantity (FATAL as written)
Everything — D-series, E6, the build order, the protect list — is
denominated in close-to-close returns that bundle the overnight gap the
strategy structurally cannot capture (the paper trader fills at next
open; our own decomposition showed medium-band negatives fully consumed
by the gap). The promotion harness as designed can promote nodes that
predict untradeable moves. **Step 0 before any node is built: relabel in
trade-space** using existing OHLC — decompose every labeled event into
gap vs post-open components, market-adjust (universe median now, sector
indices after §6.3), add 3/5/20d CARs — and re-derive the failure map,
build order, and protect list on the CAPTURABLE component. Expect
reordering (order_win_positive likely rises; medium-negative shorts
likely fall). Also: re-score all D-series on a TIME SPLIT — current
numbers are in-sample, single-window, and the protect list enshrines
them; and D-series cells are ENDOGENOUS to the prompt being replaced, so
protection must be defined on the strategy population (replay the
rules.py gate cascade over node outputs, compare entered-trade
expectancy), not per-cell accuracy.

### 8.2 Invert the judge: LLM extracts, a pooled statistical layer decides
The LLM's measured value is reading/extraction; its judgment channels
are the weakest features in the system (magnitude baseline AUC
0.41-0.60, confidence dead at 1.0). Revised node contract: nodes emit
**typed, quote-provenanced extractions** (results: P&L rows, tax delta,
one-offs, expectations_verdict; fund_raise: dilution %, discount,
instrument; order_win: value, duration, counterparty class, firmness;
rating: notches, watch/outlook) — validated on EXTRACTION FIDELITY
against ~100 hand-audited filings per type (deterministic labels, no
outcome noise, unlimited iteration — this is how prompts get optimized
without a gradient; results extractions get free accruing ground truth
from later-arriving XBRL). The extracted fields are WIRED INTO the
materiality/direction/size models as features (not persisted as
exhaust), and per-cell specialization happens by **partial pooling** —
per-cell shrunken logit recalibration (a_c + b_c·logit(p_global)) on top
of the ONE global model. CORRECTION: §1's E7 reading was overstated —
the ledger shows the specialist and global numbers came from DIFFERENT
splits (not "the same rows"), and E7 tested only full per-cell retrains,
never pooling; fund_raise's inverted magnitude curve is a 2-parameter
recalibration fix. The cross-cutting positive-direction governor becomes
CODE over expectations_verdict, not prompt prose. Prompt search (DSPy-
style, scored on the replay harness with train/select/frozen-test
separation) replaces artisanal iteration where outcome-scored prompt
changes are attempted at all.

### 8.3 Manage the tree↔gate coupling; fix the power problem; PIT discipline
- v4.1 consumes magnitude/sentiment as features; nodes deliberately
  shift those distributions. "Global layer unchanged" is therefore NOT a
  safety property — it is silent miscalibration. Every promotion scores
  node+gate JOINTLY (gate refit on the node's counterfactual outputs);
  classifier_node becomes a gate feature; the paper trader pins to a
  frozen classifier version per evaluation window.
- Power: per-cell AUC deltas of plausible size are inside the noise
  floor at current n (fund_raise test set contains ~6 positives; even
  results has SE(AUC)≈0.03). Promotions need paired-bootstrap CIs on
  identical events, replicate replays, a minimum-positives floor
  (fund_raise joins business_update/litigation in the too-thin
  quarantine until labels accrue), and exactly ONE confirmatory shot on
  a frozen recent window per candidate.
- PIT bugs found by review, to fix immediately: (a) calibrator SUE join
  uses announced_at ≤ created_at + 1 day — a look-ahead that inflates
  the results cell and creates train/serve skew (live sees has_sue=0
  where training saw 1); (b) replays must reconstruct grounding AS-OF
  event time (company profiles were backfilled 2026-06-26 — live-KB
  replays leak); going forward the pipeline logs its grounding bundle at
  decision time so the exhaust becomes the PIT store. Few-shot exemplar
  provenance: strictly pre-split-cutoff, frozen before thresholds.

### 8.4 The expectations bar and the execution layer (from the
microstructure lens)
- Licensing-free expectations sources for P1, in value order: own
  guidance from stored concall transcripts (transcript_guidance exists —
  it was missing from P1's grounding list); within-season peer-SUE
  spillover from earnings_surprises; calendar-aware run-up (run-up into
  a KNOWN result date ≠ generic momentum); T-1 option-chain implied-move
  snapshots via the existing broker session for F&O names (historical
  options data is a licensing gap; FORWARD accrual is not). Without a
  bar, results_positive stays suppressed and P1's honest success metric
  is hard-wrong/magnitude hygiene, not long-side alpha.
- Execution layer joins the plan as first-class: NSE daily price-band
  file ingestion with fail-closed band-lock handling (an LTP at a locked
  band is not a fill), liquidity-tiered cost model replacing flat 10bps
  (ADV impact + STT + fees — first-order at 0.3-1.1% median cell moves),
  edge sliced by F&O membership, and paper P&L reported gap vs
  post-entry per cell. Deep point accepted: where direction is
  predictable here is exactly where limits-to-arbitrage prevent
  harvesting — the alpha and the constraint are the same phenomenon, so
  the system must measure itself end-to-end net-of-cost from day one.
- New exhaust (zero licensing cost, highest long-run value): snapshot
  decision-time + next-open LTP for EVERY hop-0 filing impact (not just
  traded ones) — one results season of this permanently settles
  gap-vs-drift per cell. Rating-agency site crawl (CRISIL/ICRA/CARE
  rationales publish before exchange filings) is a pure latency edge
  for the one cell where direction provably works.

### 8.5 Revised sequencing
Step 0 relabel + D-series time-split re-score + PIT fixes (days, no new
data) → re-derived build order → router + provenance + extraction-first
P1 with expectations grounding incl. transcript guidance → pooled
decision layer with per-cell recalibration → execution layer + band/cost
model → prompt-search optimization where labels permit → November: label
doubling, per-cell rematch UNDER POOLING, season confirmation.

## 9. Step-0 results (2026-07-29) — the re-derived build order

S0 (trade-space relabel, PIT-corrected, time-split; lab iteration #5)
executed §8.1. Measured consequences:

1. **Honest baselines**: the v4.1 headline AUC 0.7242 carried ~0.05 of
   SUE look-ahead — PIT-correct cc AUC is 0.674; capturable-label AUC
   0.665. All node-promotion thresholds re-anchor to these.
2. **SUE is dead as a live feature** (3.7% coverage at decision time —
   XBRL lags the press release). The expectations bar must come from
   transcript guidance, within-season peer spillover, and forward
   implied-move accrual (§8.4) — and pead_long's ENTRY must move from
   filing time to surprise-row arrival.
3. **Re-derived priorities (capturable space):**
   - P1 stays results — highest capturable materiality (.127) + the
     strongest multi-day drift (|CAR z₅| 1.42) + the surviving
     results_negative edge (+8.7pp) + the persisting results_positive
     anti-signal (−8.8pp). Node objective: materiality + hard-wrong
     hygiene + horizon, NOT long-side direction.
   - NEW P2: the **fade family** — positive-classified
     investor_presentation (−8.9pp), procedural (−10.7pp),
     regulation_generic (−6.9pp) in capturable space: a short-the-spin
     strategy candidate, F&O-constrained, needs a down-tape regime check.
   - Order_win and rating DEMOTE (capturable materiality .030/.036 —
     gap-dominated; order_win_positive edge halves to +4.8pp; orderwin
     holds ride the 5d drift, which S0 supports keeping).
   - Trading knob, immediate: HEADWIND_MIN_MAG reverts 3→4
     (medium-negative capturable edge +1.6pp ≈ zero, as the gap
     decomposition predicted).
4. **Horizon beats latency for most cells** (h5 drift ≫ day-0 capturable
   everywhere): strategy holds extend; the latency case narrows to
   during-session high-band events — exactly what the absorption-curve
   exhaust (migration 032) is now measuring.
