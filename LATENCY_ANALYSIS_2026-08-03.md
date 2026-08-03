# Filing→Trade Latency — Analysis & Fixes
2026-08-03. Internal. Trigger: BLUEJET filed 12:44:45 IST, we traded 12:46:10 —
the stock had already moved ~4.7% from prev close before we saw it.

## 1. BLUEJET waterfall (measured, not modelled)

| # | Hop | Clock IST | Cost |
|---|-----|-----------|------|
| 1 | BSE exchange timestamp (NEWS_DT) | 12:44:45.35 | — |
| 2 | Detected → parsed → Kafka → analysis consume | 12:45:07.81 | **22.5s** |
| 3 | Stage-2 extraction done | 12:45:16.35 | **8.5s** |
| 4 | Context+memory fetch → stage-3 routed | 12:45:18.22 | 1.9s |
| 5 | Stage-3 LLM + stages 4/5 → published | 12:46:09.27 | **51.1s** |
| 6 | Papertrade signal | 12:46:10.18 | 0.9s |
| 7 | Fill @596.45 | 12:46:10.23 | 0.05s |

`decision_latency_ms` = 84,001 (matches). Open 633.00, prev close 626.25,
our first observed price 597.05 → **-4.7% vs prev close already gone**.
BLUEJET's stage-3 (51s) was a tail event: fleet p50 is 9.3s, observed max 70.7s.

## 2. Fleet decomposition — detection dominates, not the LLM

End-to-end decision latency: **p50 112s, p90 237s** (n=97, 7d).

| Component | BSE p50 | NSE p50 | Share |
|---|---|---|---|
| Detection lag (exchange stamp → consume) | **109.4s** | **44.7s** | ~76% |
| Pipeline (all stages) | 16.2s | 17.6s | ~14% |
| Decision + fill | ~1s | ~1s | <1% |

- Only 6% of BSE filings reach us within 30s; the median takes 2-5 min.
- Pipeline time is essentially ALL LLM: stage2 p50 7.2s + stage3 p50 9.3s ≈ 16s
  (stage5 8.2s when it runs, ~27% of items; filing_summary 4.8s runs parallel).
- Body size drives pipeline: <5k chars 13.6s → >120k 36.9s.

### The BSE floor (live probe, 2026-08-03 16:35 IST)
Newest announcement in BSE's own API was **62.0s old** at request time
(next four: 65s, 70s, 72s, 109s). **BSE does not list a filing until ~60s after
its stated NEWS_DT** — an unfixable upstream floor. So of the 109s BSE
detection lag, ~60s is theirs and ~49s was ours.

## 3. Fixes implemented (working tree)

### Fix 2 — detection lag (the big one)
1. **Batch barrier removed** (`gather_pdf_texts_as_completed`, both adapters):
   attachments were gathered for the WHOLE page before any item was yielded,
   so the first-ready filing paid for the slowest PDF in the burst. Now each
   filing publishes as its own attachment lands.
   *Live smoke test: first item at t+0.69s vs t+4.53s for the full batch.*
2. **Poll intervals tightened, sized to rate limits not guesses:**
   - BSE 30s → **15s** (steady state 2 req/cycle = 8 req/min vs limit 10;
     10s would need 12 req/min and get throttled).
   - NSE 15s → **8s**, `rate_limit_per_minute` 10 → 20 (2 req/cycle = 15/min).
3. **Diagnostics:** BSE fetch errors logged `type(e).__name__: repr(e)` —
   `asyncio.TimeoutError` str()s to empty, so the old line read
   `Error fetching 20260803 p1:` with no cause (8 such in 6h).

### Fix 1 — pipeline (scope corrected, see §4)
4. **Company-context prefetch overlaps stage-2** (`prefetch_company_contexts`):
   stage-3's KB profile fetches depend only on the symbol, which exchange
   filings already carry, so they now start when stage-2 starts instead of
   running serially after it. Saves the ~1-2s context-fetch leg.
   **Prompt is byte-identical** — same context objects, only fetched earlier.
   Bounded registry (256) with cancel-on-evict; safe on the C1a retry path.

Verification: 90 passed / 1 skipped / 4 xfailed (unchanged); prefetch registry
3/3 unit groups; as-completed helper 4/4 asserts (no barrier, fail-soft, all
slots); live BSE adapter smoke test.

## 4. What I did NOT do, and why

The original fix-1 pitch was "run stage-2 parallel to stage-3, ~7s saved, zero
accuracy loss." **That claim was wrong.** Stage-3 passes stage-2's
`macro_theme` + `affected_sectors` into `memory_client.fetch_memory_context`,
which ranks the market/sector backdrop injected into the prompt. Decoupling
them changes the retrieved memory → changes the prompt → is exactly the
unvalidated classifier-input change the Tier-B replay taught us to refuse.
Full decoupling therefore needs a replay validation first (harness exists:
`research/replay_harness.py`). Not shipped.

Related, deliberately not touched:
- **Stage-5 signals (p50 8.2s, before publish)**: papertrade ignores
  `trade_signals` entirely and only 6.9% of reports carry any — so this is
  ~8s of pure trading-path latency. Fixing it means publishing the report
  before signals exist (two messages, or a signals-after-publish rewrite)
  which changes the Kafka contract for `sentiment/snapshot_consumer`. Worth
  doing, needs a design decision.
- Trimming stage-2's body budget would cut its 7.2s but violates the standing
  accuracy-over-latency policy and changes stage-2 output.

## 5. Expected effect (honest)

Median BSE path today ≈ 109s detect + 16s pipeline = 125s.
After: BSE floor ~60s + halved poll wait (~7.5s avg vs ~15s) + no batch
barrier + ~1.5s prefetch ⇒ **roughly 75-85s**, i.e. ~40s better, with ~60s
now irreducible without a non-polling BSE feed. NSE improves less in absolute
terms (44.7s → ~30s) because it has no comparable listing floor measured.
These are projections from component measurements — the honest check is
re-running §2's queries a week after deploy.

## 6. Not addressed by latency work (but bigger on BLUEJET)
Our marks: t0 597.05 → t+5m 585.65 → **t+15m 581.20 (-2.7% in our favour)** →
t+30m 599.00 → now 593.60. There was another 2.7% available AFTER entry that
the exit policy gave back. On this trade the exit rules cost more than the
84s entry delay. Candidate: tighter trailing stop inside the first 30 minutes
of a mag>=4 event. Not implemented, not validated.
