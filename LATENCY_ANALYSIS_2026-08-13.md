# Pipeline Latency Audit — 2026-08-13 (post-AWS-migration)

Goal given: end-to-end latency under 5 seconds without compromising accuracy.
This doc records what was measured, why the target as literally stated is not
achievable, what <5s *can* honestly mean, and the plan to get there.
Supersedes the numbers (not the structure) of `LATENCY_ANALYSIS_2026-08-03.md`;
the architecture map there still holds except where noted.

Nothing was changed. All measurements are read-only queries against prod
(2026-08-13, current EC2 stack — analysis container `3d989c4feac7`, up since
~2026-08-12 18:00 UTC).

## 1. Measured decomposition (last 24h, current stack)

Source: `analysis_audit` (detection = `analysis_created_at − processing_latency_ms − published_at`;
pipeline = `processing_latency_ms`; relay = `audited_at − analysis_created_at`).
Filtered to 0 < e2e < 6h. Market hours = published 09:00–15:30 IST.

| Segment | source | n | p50 | p90 |
|---|---|---|---|---|
| Detection (exchange stamp → pipeline start) | NSE, mkt-hrs | 215 | **43s** | 67s |
| Detection | BSE, mkt-hrs | 388 | **46s** | 69s |
| LLM pipeline (all stages) | NSE, mkt-hrs | 215 | **7.9s** | 18.4s |
| LLM pipeline | BSE, mkt-hrs | 388 | **8.1s** | 20.9s |
| Kafka + relay + WebSocket push | both | — | **~0s** | ~0s |

End-to-end p50 ≈ **51–54s**; detection lag is ~85% of it.

Per-stage LLM latency (llm_call_log, live container, last 24h):

| Stage | Model | n | p50 | p90 | avg ptok | avg ctok | cached | err% |
|---|---|---|---|---|---|---|---|---|
| stage2_extraction | claude-haiku-4-5 (Bedrock) | 364 | 2.0s | 3.4s | 5,832 | 178 | **0** | 0.0 |
| filing_summary | claude-sonnet-4-5 (Bedrock) | 305 | 4.7s | 7.4s | 6,310 | 241 | **0** | 0.0 |
| stage3_impact | claude-sonnet-4-6 (Bedrock) | 364 | 9.2s | 18.4s | 7,797 | 448 | **0** | 0.0 |
| stage5_signals | claude-haiku-4-5 (off in mkt hrs) | 4 | 4.9s | 6.1s | 2,548 | 379 | **0** | 0.0 |

Per-item trace (context_id grouping) confirms: stage2 ∥ filing_summary run in
parallel, stage3 is sequential after stage2. Critical path ≈ stage2 (2.0s) +
stage3 (9.2s) ≈ 11s; observed item wall time 5.4–19s. Stage1 relevance is a
local zero-shot model (~0s, no network). Stage4 is KB-HTTP only, no LLM.

Notes on data quality:
- The 37–52% stage2/filing_summary error rates in llm_call_log are from the
  *previous* containers (last call 2026-08-12 17:38 UTC, pre-cutover of
  e66c5d5). The live container shows 0% across all stages.
- `llm_call_log` is flushed via Redis every 15s (`api/flushers.py`), so it
  lags reality by up to 15s — irrelevant to percentiles, relevant if you ever
  join it against wall-clock events.
- No ITEM_TIMEOUT / pacer-throttle alerts in the last 3 days. The
  90s-item-budget vs 135s-LLM-budget conflict flagged in the code review of
  `guardrails.py:44` vs `wrapper.py:184` is real but not currently firing.

## 2. Where the 43–46s detection lag actually comes from

- **Poller cadence is NOT the problem.** Liveness log: `adapter cycles:
  NSE_FILINGS=3s, BSE_FILINGS=12s` (configured fast intervals 8s / 15s,
  `nse_filings.py:93`, `bse_filings.py:72`). Average contribution ≈ 4–7.5s.
- **Exchange-side visibility floor is the dominant term.** The 2026-08-03
  live probe (see LATENCY_ANALYSIS_2026-08-03.md §2) measured ~60s between a
  BSE filing's stated NEWS_DT and its appearance in api.bseindia.com. NSE's
  feed shows a similar ~30–60s lag. This is upstream of us and not fixable
  by polling harder. Dual-exchange first-wins dedup (live 2026-07-23)
  already takes whichever exchange surfaces a filing first.
- Remainder: PDF fetch + text extraction (concurrency 4, 30s timeout) and
  RT_CONCURRENCY=3 head-of-line waiting during bursts.

## 3. Why "<5s end-to-end" as stated is not achievable

From exchange timestamp to user-visible output, the exchanges' own
publication delay (~30–60s+ median) exceeds the entire 5s budget before a
single byte reaches us. No engineering on our side changes that.
**The target must be defined as detection → user-visible.** Against that
definition, <5s is achievable — but not for the *full* stage-3 impact
analysis without accuracy-affecting changes (stage3 alone generates ~448
output tokens ≈ 7–9s of pure token generation on Sonnet 4.6).

## 4. Plan

### Phase 1 — no accuracy impact (recommended)

1. **Progressive delivery (the big one).** Publish the impact report
   incrementally to `analysed.impact.in` / the WebSocket:
   - t+~1s after detection: skeleton event (headline, symbols, category,
     stage1 relevance) — user sees the filing on the tape in <5s.
   - t+~5s: filing_summary + stage2 extraction merged in.
   - t+~12s: stage3 impact scores merged in (unchanged analysis).
   The analysis itself is untouched — only delivery becomes incremental.
   Touches: pipeline.py (emit-early hooks), kafka_publisher, kafka_relay
   (upsert by report id), frontend impact feed (update-in-place, already
   keyed). This is what makes a truthful "<5s" claim possible.
2. **Bedrock prompt caching.** `cached_tokens=0` on every call today.
   Prompts are 5.8–7.8k tokens; static rubric/instruction prefix should be
   marked with `cache_control: {type: "ephemeral"}`. Minimum cacheable
   prefix: 1024 tokens on Sonnet 4.5/4.6, **4096 on Haiku 4.5** — verify the
   static prefix length per stage at implementation time; Bedrock has no
   automatic caching, blocks must be explicit, and the prefix must be
   byte-stable (no timestamps/ids above the breakpoint). Expected: ~1–2s
   p50 per event off input processing, bigger p90 effect, ~85–90% input-cost
   cut on the cached span. Event arrival (~1.5/min market hours) keeps the
   5-min TTL warm.
3. **Quota headroom for bursts (p90/p99 lever).** Client-side pacer budgets
   are Haiku 5 RPM / Sonnet-4.6 7 RPM / Sonnet-4.5 8 RPM
   (docker-compose:360–364) against a 10 RPM/model account clamp. Results-day
   bursts queue behind this. Options, in order: re-file the AWS service-quota
   case with 30 days of usage evidence (Haiku case was rejected once);
   split stage traffic across regional profiles; or move the hot path to
   Anthropic direct API keys where RPM is tier-based. Raising
   RT_CONCURRENCY beyond 3 only helps *after* this.
4. **Micro-wins.** BSE fast poll 15s→10s (rate limiter 10/min bounds it,
   politeness sleep 1.5s/page retained); NSE 8s→5s (limit 20/min). Fix the
   market-hours definition mismatch (ingestion 08:00–16:00 IST in base.py:23
   vs analysis 09:00–15:30 in market_hours.py:10) so 08:00–09:00 and
   15:30–16:00 non-filing items don't sit in the 300s batch buffer.

### Phase 2 — eval-gated (accuracy rule applies)

5. **stage3 generation time.** 9.2s p50 is ~80% output-token generation.
   Two candidate cuts, BOTH gated on the nightly 19:05 IST classification
   accuracy eval + a side-by-side on a fixed filing set before cutover:
   (a) tighter tool schema (cap rationale verbosity; keep scores + rubric
   fields); (b) trial Haiku 4.5 for stage3 with the scenario rubrics.
   If either degrades accuracy vs the Sonnet 4.6 baseline: don't ship.
   Per the no-improvement rule, park it.

### Expected outcome

| Metric | Today (p50) | After Phase 1 | After Phase 2 (if evals pass) |
|---|---|---|---|
| Detection → first visible on tape | ~12–14s | **<5s** | <5s |
| Detection → full impact scores | ~12–14s | ~8–11s | ~5–7s |
| Exchange stamp → visible | ~51–54s | ~40–50s | unchanged floor (~30–60s upstream) |

## 5. Addendum (same day): market-hours-first framing + today's signals

Founder clarification: latency matters during market hours (competition reacts
to news immediately); off-hours can be slow and MORE thorough; a dedicated GPU
inference instance during market hours is acceptable only as a last resort.

Today's (2026-08-13) actual paper-trade latencies, news publish → entry:

| Window | n | pub→entry | detect→entry |
|---|---|---|---|
| Market hours (filled) | 13 | 26–102s, median ~56s | **10–25s, median ~12s** |
| After 15:30 | 15 | no fill (market closed) — latency irrelevant, exactly the two-regime point |

Split confirms the audit: of the ~56s median news→signal gap, only ~12s is
ours (pipeline + papertrade gate + fill); the rest is exchange-side
visibility. The signal-critical milestone is **stage3 completion** (papertrade
gates on impact magnitude), not the skeleton event — so for signal latency the
levers are caching, stage3 generation time, and burst quota; progressive
delivery mainly serves the human on the tape.

Two-regime design (formalizes what partially exists — stage5 off in market
hours, off-hours batch buffer):

- **Market hours (09:00–15:30 IST):** signal-first. Publish the impact event
  the moment stage3 lands — do NOT wait on the filing_summary join at
  finalize (up to 30s at `pipeline.py:111,209`; summary attaches as an
  update). Prompt caching on. Quota headroom for result-day bursts.
  Detection micro-wins (BSE 15→10s, NSE 8→5s) — WITHDRAWN at implementation,
  see below.

  Poll-interval decision (2026-08-13 implementation): both intervals are
  rate-limit floors, not guesses — BSE 15s = 8 req/min vs a 10/min politeness
  budget (`bse_filings.py:64-74`), NSE 8s = 15 req/min vs 20/min
  (`nse_filings.py:87-95`). Tightening either requires raising the per-source
  request rate on exchanges with documented throttle/block history (NSE 403s
  blocked this system for weeks; BSE session throttling). Expected gain is
  ~1.5–2.5s average against a ~45s exchange-side floor. Not worth the
  block risk — intervals left unchanged.
- **Off-hours:** unchanged latency-wise, and use the freed budget for
  accuracy: stage5 already runs; candidate addition (separate proposal) — an
  overnight second-pass re-scoring of low-confidence/market-hours quick
  calls with grounding, feeding the nightly accuracy eval.

GPU self-hosting assessment (last-resort option): a self-hosted open-weights
model buys freedom from Bedrock RPM quotas, NOT per-token generation speed at
the required quality — a 70B-class model on g6e-class instances generates
~30–60 tok/s, no faster than Bedrock Sonnet, and its accuracy on the scoring
rubric is unverified (likely below Sonnet 4.6, which violates the accuracy
rule); ~$1.5k/mo for market-hours-only 70B serving plus real ops burden.
Every cheaper lever (caching, quota case, direct-API keys, output trim)
attacks the same bottlenecks first. Park unless AWS refuses quotas AND bursts
remain the binding constraint after Phase 1/2.

Market-hours signal-latency targets (detection → signal-eligible impact event):
today ~12s median → Phase 1 (caching + skip summary join) ~8–10s →
Phase 2 (eval-gated stage3 trim) ~5–7s.

## 6. Phase 1 — BUILT (working tree, 2026-08-13 evening; NOT deployed)

All changes are uncommitted working-tree edits, verified but awaiting the
per-instance deploy approval. Summary (13 files, +387/−79):

1. **Progressive delivery + signal-first publish** — `types.py`
   (analysis_status/update_seq fields, defaults keep every old consumer
   unchanged), `pipeline.py` (stable report id per run; market-hours
   skeleton published right after relevance; pipeline-owned publish
   ordering; finalize no longer blocks on the summary join in market hours —
   summary attaches as update_seq=1), `analysis/main.py` (publish callback;
   shutdown drain), `api/kafka_relay.py` (partial-aware upserts:
   news_items summary COALESCE, impact_reports last-write-wins,
   analysis_audit shielded from partials — first complete event wins),
   `papertrade/main.py` (skips partial/update events; entries were already
   idempotent by report id), frontend (feed updates rows in place — merge
   was already id-keyed; "Analysing" pulse with 5-min staleness fallback;
   AssessmentPanel partial state).
2. **LLM wrapper** — SDK client reuse (was: new client + TLS handshake per
   call, 3×/filing; now cached per provider sync / per event loop async,
   weak-keyed so short-lived loops release clients), prompt-cache
   breakpoint support (`cache=True` on the three hot stages +
   cached_tokens now logged to llm_call_log). HONEST CAVEAT: today's
   static prefixes are all below the models' cacheable minimums (stage3
   ~760 tok vs 1024, filing_summary ~530, stage2 ~640 vs 4096), so caching
   is a no-op until the eval-gated Phase-2 prompt reorder — the projected
   caching second moved to Phase 2; client reuse compensates ~0.3-0.9s/item.
3. **Poll intervals** — withdrawn (see §5 addendum).
4. **AWS quota case** — drafted with measured RPM evidence:
   `AWS_QUOTA_CASE_2026-08-13.md` (filing is a founder action).

Verification: 111 unit tests pass in-container (8 new: wrapper cache
semantics, client reuse, report-field compat); real-LangGraph smoke test of
all three publish orderings (market-hours skeleton→complete→summary-update
on one id; off-hours single blocking event; no-callback batch path); tsc
--noEmit passes; both relay upserts EXPLAIN cleanly against the live
schema (read-only txn).

Expected effect (market hours): detection → tape <5s (skeleton);
detection → signal-eligible impact event ~8-11s p50 (summary join off the
critical path cuts the p90/p99 tail by up to 30s; client reuse trims each
stage). Exchange-side floor unchanged.

## 7. Explicitly out of scope / noted in passing

- HOURLY_COST_BUDGET alerts fired 4× in the last 3 days — cost, not latency;
  worth a separate look.
- fno_engine/ is an empty directory; not on any path.
- The 90s vs 135s budget conflict (guardrails vs wrapper) — not firing, but
  a burst + pacer wait could trip it; tighten when touching the pacer.
- 35 top500 dossier failures never retried (pre-existing, U1/U2 open).
