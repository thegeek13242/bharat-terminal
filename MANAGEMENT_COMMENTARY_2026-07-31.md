# Management Commentary Integration — Plan & Brainstorm
2026-07-31. Internal only. Status: §0-§3 evidence FINAL; C1a/C1b/C0 BUILT
2026-07-31 evening (see §7 build log) — C0 validation replay + deploy pending.
Evidence gathered 2026-07-31 by a 4-agent read-only research sweep (HYUNDAI forensic,
source inventory, failure-mode quantification, external literature). All DB numbers
verified by query on that date; queries preserved in the session workflow journal.

---

## 0. Founding exhibit: HYUNDAI Q1 FY27 (2026-07-30)

- 15:25:31 IST: press release filed. Trailing numbers bad — revenue −0.5% YoY,
  EBITDA −30.8% (margin 9.3% vs 13.3%), PAT −35.1%. **Same document**, verbatim:
  MD & CEO Tarun Garg — *"With 100% normalization of production … recovery is
  likely to gain pace from Q2 onwards … we remain committed to achieving our
  stated guidance of 8-10% (YoY) volume growth … as well as 11-14% EBITDA
  margin in FY27."*
- Our classifier (impact 8f7d0b29): sentiment=negative, **magnitude=4**. Its own
  rationale ends *"…despite management's expectation of recovery from Q2
  onwards"* — it read the guidance, verbalized it, and did not let it move the score.
- headwind_short fired 110s after filing: SHORT 1 FUT lot (275) @ 2025.77,
  3 min before close.
- Next morning the market priced the guidance, not the quarter: open 2099 (+4%),
  ~2191 (+8.6%) by 12:45. Trail stop gapped through; exit 2158.20; **pnl −₹36,418**.

**Diagnosis: input-complete, weighting-wrong.** Not a data gap, not a latency gap,
not a prompt-context gap. The model treats quantified forward guidance as a
subordinate clause; the market treats it as the headline. A second, subtler defect:
the vivid press-release narrative scored magnitude 4 while the numbers-only
results filing scored 3 — rich text amplified the negative reading even while
containing the offset.

---

## 1. The measured prize (120d, hop-0, mag≥3, EARNINGS-theme filings; n=855 priced events)

Raw close-to-close, ±3% decisive threshold (convention: filed <15:30 IST → same-day
close vs prev close; after → next session):

| our call | n | confirmed | flipped (wrong) | flat |
|---|---|---|---|---|
| negative | 117 | 40 (34.2%) | **8 (6.8%)** | 69 |
| positive | 738 | 145 (19.6%) | **128 (17.3%)** | 465 |

- **P(wrong direction | decisive ≥3% move) = 42.4%.**
- **Magnitude-5 positives are a coin flip: 25 up ≥3% vs 25 down ≤3%.**
- The HYUNDAI shape (negative → rip) is real but rare: 8 cases (FINOPB +19.2%,
  DYCL +15.3%, INFY +4.5%…). The **mirror — we say positive, stock dumps — is 16×
  bigger (128 cases)** and is where commentary integration pays.
- Exemplar deep-dives (8 largest flips, filing bodies read): **5/8 were decidable
  at t0 from documents we already had** — missed one-off attribution (GOCOLORS
  ₹6.5 Cr write-off), missed easing-degrowth language (FINOPB), profit-warning
  language outweighed by a recovery paragraph (KPITTECH 30-Jun, the inverse
  error), missed hedging (ADFFOODS "cautiously optimistic", shipping constraints).
  1/8 (DYCL) the commentary press release arrived 71 min after the results table
  and **was never scored at all** (pipeline gap, see C1). 2/8 (TRENT, SPANDANA)
  were pure street-expectations gaps invisible in filing text — commentary work
  will NOT fix those; they need a consensus/expectations anchor (parked:
  licensing).

Caveats: raw returns (not market-adjusted), one earnings-season-dominated window,
BSE-only names underrepresented (185 events dropped for missing prices).

---

## 2. What we already have (verified inventory — more than expected)

| arrival | source | volume 60d | latency vs results filing | commentary content |
|---|---|---|---|---|
| t0 | results PDF | 1,362 | — | mgmt quote in only ~17-20% |
| **t0** | **press release** | **979** | **median +7 min; 60% within ±30 min** | **77% carry commentary; 67% direct MD/CEO quote** |
| t0→t+6h | investor presentation | 494 | median +38 min; 72% ≤6h | guidance slides typical |
| t+1d | call-audio link | ≥76 (floor) | 82% ≤1 day (SEBI 24h rule holds) | link only — audio not parsed |
| t+4-7d | **concall transcript** | 435 | median 5.85d, p75 6.99d | fully parsed |

Already-built transcript stack (live, populated, verified): `concall_transcripts`
528 rows / 503 symbols since 2026-05-10 (one season of history),
`transcript_chunks` 16,164 embedded chunks, **`transcript_guidance` 2,524
structured guidance rows** (metric, period, value_low/high, quote offsets,
prior_guidance_id, change_verdict) fed by the live transcript-watcher container.

**The structural insight:** the information-rich, already-structured source
(transcript guidance) arrives ~6 days after the price-relevant moment; the
price-relevant-moment source (press release) is already in `news_items.body` at
t0 with a 77% commentary rate — it is simply not being *extracted*, only prose-read.

---

## 3. What the external evidence says works (practice-ranked)

1. **Guidance surprise** (guidance vs prior guidance/consensus) — largest documented
   price contribution of any accounting disclosure (Preussner & Hitz 2022, peer-reviewed).
2. **Tone DELTA vs same company's prior quarter** — not tone level (Druz et al.,
   FAJ 2020). Level tone is the crowded, decayed factor.
3. **Call tone as a conditioner on PEAD** (Price et al., JBF 2012; Philly Fed
   PEAD.txt) — text tone predicted drift better than SUE. Directly marries our
   validated PEAD sleeve.
4. **Q&A section > prepared remarks** (~34% more informative; Matsumoto et al.,
   TAR 2011) — score them separately, weight Q&A.
5. **Management credibility as a multiplier on guidance**, not a standalone factor
   (EAR 2024) — did they deliver what they guided last quarter?
6. **Hedging/uncertainty/deception language as a short-side screen**
   (Larcker & Zakolyukina, JAR 2012).

LLM-era cautions: use LLMs for **extraction** (numerics, one-off attribution,
promises), not direct return-scoring — direct-scoring papers are preprint-grade
with profitability-factor overlap; and pretrained LLMs **recognize companies in
historical transcripts** (arXiv 2412.18029), a look-ahead channel that
contaminates any LLM-scored backtest. Extraction + deterministic rules sidesteps
both. India note: no peer-reviewed India replication of call-NLP alpha exists;
sectoral guidance norms differ (IT: formal bands; autos: monthly volumes;
banks: verbal NIM guidance on calls; rest: qualitative outlook quotes).

This ranking maps exactly onto the analyst-clone thesis: **LLM extracts,
calibrated deterministic layer decides.**

---

## 4. Build plan

### C0 — t0 commentary extraction + signal-side veto (the HYUNDAI fix). SMALL.
On results-cluster filings, a second extraction pass (or added tool-schema fields)
over the press-release text emitting STRUCTURED fields:

```
guidance: [{metric, period, low, high, verdict: reaffirmed|raised|cut|withdrawn|new}]
one_off_attribution: {present, items[], quantified_cr}
recovery_narrative: {present, quantified, drivers[], horizon}
warning_language: {present, quotes[]}        # "lower than expected", "constraints limiting"
hedging_score: low|med|high                  # "cautiously optimistic", uncertainty markers
mgmt_quote_present: bool
```

Consumed at the **signal gate, not the classifier** (deliberately NOT a Tier-B-style
rubric rewrite — the replay harness already falsified one of those, RB-1):
- `headwind_short` veto when `guidance.verdict ∈ {reaffirmed, raised}` with
  quantified numbers, or quantified one-off attribution covering the miss
  (HYUNDAI, GOCOLORS, FINOPB all caught).
- long-side veto when `warning_language.present` or `hedging_score=high`
  (KPITTECH 30-Jun, ADFFOODS caught).
- Fields land in `router_meta`/provenance for audit; classifier sentiment/magnitude
  untouched.

Validation, pre-registered before build: replay the extractor over the 855-event
window; score veto precision on the 136 wrong-direction events vs false-veto rate
on the 185 right-direction events. Ship only if wrong-direction events vetoed at
≥3× the rate of right-direction events (the no-improvement rule applies).

### C1 — results-cluster stitching (the DYCL fix). SMALL, high certainty.
Results table, press release, and presentation for the same symbol within a
±2h window are ONE event: classify on the union, or at minimum guarantee the
press release always gets scored and supersedes the bare-table classification.
Today a commentary press release 71 min after the results table can get no
impact report at all. Also folds into the proposed company-state context header.

### C2 — guidance ledger + credibility score. MEDIUM.
Promote `transcript_guidance` (+ C0 press-release guidance) into a per-company
**standing guidance state**: current guided metrics, source, last change. At the
next results t0, the extractor's job becomes *"results vs standing guidance"* —
a real expectations bar that needs no licensed consensus data. Credibility
multiplier = trailing delivery accuracy per company. Constraint: transcript
history starts 2026-05-10, so credibility needs 2-3 more quarters to accrue;
ledger starts now, multiplier stays OFF until then.

### C3 — t+later revision loop. MEDIUM, after C0-C2.
When the presentation (+38 min), audio (t+1d), or transcript (t+6d) lands:
re-evaluate open paper positions and the standing stance; emit a revision event
(analogue of the pre-open confirmation gate, at the position level). Once ≥2
quarters of transcripts exist per symbol: Q&A-only tone delta vs prior quarter
as a PEAD conditioner (literature rank 3) — evaluated in the materiality lab
like any challenger. Live call transcription (Perplexity-style) is feasibility-
proven but out of scope; audio-to-text of the 24h recording is the cheaper
first step if C3 shows the call adds beyond the press release.

### C4 — measurement. Runs with everything above.
Target metric: P(wrong | decisive) from 42.4% down, positive-side flip rate from
17.3% down, on a FRESH window (the 855-event window becomes the dev set the
moment C0 is designed against it). Nightly accuracy eval extended with a
commentary-cell breakdown. Paper-pilot pre-registered criteria unchanged.

Order: C1 → C0 → C2 → C3, with C4 throughout. C1 first because it is
unambiguous plumbing with no model risk and C0's extractor wants the stitched
cluster as its input unit.

---

## 5. Non-goals / honest limits
- Expectations gaps with zero textual trace (TRENT, SPANDANA: ~2/8 of big flips)
  are NOT addressable by commentary — that is the consensus-data problem (parked
  on licensing, per 2026-06 research).
- No direct LLM "will the stock go up" scoring — extraction only (leakage +
  overfit evidence, §3).
- mag-5-positive coin flip: commentary vetoes may help the worst of it, but the
  deeper issue (positives ≤ drift) is the known baseline finding; no long-side
  strategy changes proposed here.

## 6. Build log (2026-07-31 evening; founder-approved "start C1 and C0")

Recon (3-agent read-only sweep) corrected one §4 assumption: the DYCL press
release was NOT unscored — it produced a relevant=true report with EMPTY
company_impacts via the stage-3 exception swallow
(stage3_impact.py:578-590; Gemini MALFORMED_FUNCTION_CALL x3 or Vertex 429).
That failure mode is systematic: 245/11,394 relevant reports (2.2%) in 30d,
42/42 exact log↔DB correlation in the observable window. Cluster facts: table
arrives first in 99.2% of clusters; PR exists in ~37%, median +22 min; 42% of
same-cluster impact pairs materially disagree when scored blind.

Shipped in working tree:
- **C1a** — stage-3 failures now stash `stage3_error` into router_meta
  (stage3_impact.py except-handler) and analysis/main.py schedules ONE
  in-process retry after STAGE3_RETRY_DELAY_S=240s (`STAGE3_RETRY=1`).
  The failed report still publishes; the retried run persists under a new
  report id (kafka_relay inserts are ON CONFLICT DO NOTHING). Known limits:
  retry lost on container restart; the failed row itself carries no DB
  marker (router_meta is not persisted — pre-existing gap, §6).
- **C1b** — sibling-context plumbing, DEFAULT OFF (`STAGE3_SIBLING_CONTEXT=0`):
  kb GET /company/{symbol}/filings/siblings (2h backward window, 4 filings,
  bodies capped) + stage3 fetch + `sibling_section` param interpolated into
  build_impact_prompt (flag-off byte-identical). Enable only after replay
  validation on a fresh window.
- **C0** — papertrade/commentary.py: forced-tool extraction (guidance verdicts,
  one-off quantified-cr, recovery narrative, warning language, hedging score)
  + pure-Python veto_decision (7/7 canonical cases unit-tested: HYUNDAI
  guidance_reaffirmed, GOCOLORS one_off_quantified, KPITTECH warning_language,
  qualitative-optimism-never-vetoes, side isolation, None fail-open,
  hedging_high). rules.py hook `_c0_commentary` runs once per results-genre
  report (etype OR router_meta bucket/sniff/scenario keying) on live
  headwind_short/tailwind_long intents; `PAPER_C0_MODE=shadow` default
  (stamp would-veto in provenance.c0, never block), `enforce` only after the
  §C0 bar passes. Fail-open at every layer. requirements/papertrade.txt gains
  the two Vertex SDK pins.
- **Validation harness** — research/commentary_replay.py (frame reproduces the
  §1 population exactly: 855 events, 136/185/534; est. $3.26 for the full
  replay; seed-42 bootstrap CI on the wrong/right veto-rate ratio; ship bar
  ratio >= 3.0 pre-registered in §C0 before the run).

### C0 validation VERDICT (run 2026-07-31, exports/materiality_lab/commentary_replay_2026-07-31.json)

**FAILED the pre-registered bar — decisively.** 746/855 extractions ok; veto
rates: wrong 21.6% (27/125), right 22.7% (39/172), flat 21.4% (96/449).
Ratio 0.95, bootstrap 95% CI [0.60, 1.47] vs bar 3.0. The presence-based
rules fire on ~22% of ALL results filings indiscriminately — they measure the
base rate of commentary prose, not wrongness. Per rule:
- `warning_language` 12W/12R/48F and `hedging_high` 12W/12R/35F — hedged
  language is ubiquitous in Indian filings; presence ≠ signal.
- `one_off_quantified` 2W/12R/9F — **inverted**: genuinely bad quarters also
  carry a quantified exceptional and the market punishes anyway; the rule
  would have vetoed 6x more correct shorts than wrong ones.
- `guidance_reaffirmed` (the HYUNDAI pattern) fired only 7 times in 746
  (1W/3R/3F) — too rare on this window to power any conclusion.
- `quantified_recovery` fired once. The FINOPB shape is rarer than assumed.

**Consequences (per the no-improvement rule):**
1. `enforce` is OFF THE TABLE for these rules. PAPER_C0_MODE stays `shadow` —
   redefined as INSTRUMENTATION: live extraction QA + accruing a fresh
   labeled window (provenance.c0 + realized outcomes) for the next iteration.
2. This 855-event window is now a BURNED dev set. Any refined rules
   (one-off size vs miss size, credibility-conditioned guidance via C2,
   large-flip-only targets) may be developed against it but must pre-register
   and validate on post-2026-07-31 data only.
3. The §1 prize (42.4% wrong-given-decisive) stands, but it is not
   harvestable with blunt textual presence flags. The exemplar deep-dives
   (5/8 of the BIGGEST flips textually decidable) do not generalize down to
   the ±3% tail, where non-textual causes (expectations gaps, market beta)
   evidently dominate.
4. C1a is unaffected (plumbing, no model claim) and ships regardless.
   C1b remains flag-off pending its own fresh-window replay.

## 7. Incidental findings from the sweeps (noted, not fixed)
- Investor-presentation impact e8f60760 mislabels EBIT (−42.4%) as PAT — minor
  classification defect class (metric confusion inside decks).
- `analysis_audit` body_fetch_status='skipped'/NULL model fields — the known open
  audit-recording bug; it blocked exact prompt replay for HYUNDAI. Raises the
  cost of every forensic; worth fixing before C0 ships.
- `impact_reports` has no event_type column (EARNINGS macro_theme is the proxy);
  C0 fields in router_meta partially remedy the schema gap.
- The 09:22:58 fill/exit cluster on 2026-07-31 was the phantom-fill-incident
  hotfix deploy (4d367b3 committed 09:22:28), NOT a systematic execution lag —
  steady-state exits fill on the first 5s mark after 09:15.
