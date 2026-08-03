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

## 7. POST-DEPLOY VERIFICATION — and a correction to §2/§5

Deployed 2026-08-03 17:07 IST. First 15 post-deploy filings measured BSE
detect p50 195.8s / NSE 78.1s — *worse* than the §2 baseline. Neither number
means what it looks like, and chasing it exposed an error in §2.

**Cause of the apparent regression:** the sample was entirely OFF-HOURS
(`_is_market_hours() = False` after 15:30 IST), so adapters were on the SLOW
intervals — BSE 300s, NSE 120s — which this commit did not change. Pre-deploy
off-hours was BSE 181.6s / NSE 58.1s, so post-deploy off-hours (n=10/5) is
statistically the same. **No regression; the fast path simply cannot be
measured outside market hours.**

**The error in §2:** those headline figures (BSE 109.4s, NSE 44.7s) blended
market-hours and off-hours filings. Split properly over the same 7 days:

| Source | market-hours (09:15-15:30) | off-hours |
|---|---|---|
| BSE | **61.6s** (n=1413) | 181.6s (n=2146) |
| NSE | **26.0s** (n=918) | 58.1s (n=1953) |

The trading path only ever sees the market-hours column. Off-hours filings
queue to the next open anyway (PENDING → auction fill), so their latency is
nearly irrelevant — and they were inflating the baseline that justified the
poll-interval change.

**Consequences:**
1. BSE market-hours detection (61.6s) sits almost exactly on the ~60s BSE
   listing floor measured in §2. Either our own added latency was already
   ~nil during market hours, or BSE lists faster intra-session than the
   16:35 probe showed. Unresolved — the probe must be repeated DURING market
   hours to split these.
2. Realistic gain from the poll change is therefore much smaller than §5
   claimed: BSE avg wait 15s→7.5s and NSE 7.5s→4s, i.e. **BSE ~61.6s→~54s,
   NSE ~26s→~22s**, not "125s→75-85s". §5's projection is withdrawn — it was
   computed off the blended number.
3. The **batch-barrier removal is the part that still clearly earns its
   keep**, and it is independent of poll interval: it only bites during
   bursts, which is exactly results-day market hours (verified 0.69s vs
   4.53s to first item).
4. **The pipeline is now the bigger share of the market-hours path**
   (BSE: 61.6s detect + 16.2s pipeline). That RAISES the value of the two
   deferred pipeline items in §4 — stage-5 (~8s, papertrade ignores its
   output) and stage-2 decoupling (~7s) — relative to any further detection
   work.

Honest status: the deployed changes are safe and directionally right, but the
size of the win is unproven and smaller than first claimed. Verification is a
market-hours re-run of the §2 queries plus a market-hours BSE listing probe.

## 8. Stage 5 paused during market hours (founder decision 2026-08-03)

**What stage 5 is:** the last LLM stage — turns scored impacts into trade
signals (direction, instrument, position size, conviction, rationale). Runs on
the PRO model (`SIGNALS_MODEL=gemini-3.1-pro-preview`): **p50 8.5s, p90 11.9s**,
~$0.002/call. Fires on 1,423 of 4,673 relevant reports (30%) in 7d — it
short-circuits for derivative headlines, stale articles, and anything without
a hop-0 magnitude>=3 impact.

**Why it was pure latency:** it sits between stage 4 and the Kafka publish, so
it delays the papertrade consumer on every filing it touches — yet nothing on
the trading path reads it. Papertrade trades `company_impacts` (zero
references to `trade_signals` in the package); customers never see signals
(`kafka_relay.py:146` strips them from the WS broadcast as SEBI RA scope,
`impact.py` lists them in `_INTERNAL_KEYS`, `starred.py` omits them).

**It is NOT consumer-free** (the premise checked before pausing): the nightly
`daily_signal_eval` (19:00 IST weekdays, gated on the price sync) explodes
`impact_reports.trade_signals` into `signal_outcomes` via
`research/score_signals`, scores 1/5/10/20d forward returns, and feeds the
internal `/signal-accuracy` API plus `research/gate_backtest.py`. Verified
live: 2,688 outcomes (1,082 complete with full 20d returns, 1,421 partial),
1,569 in the last 30 days, newest signal stamped 2026-08-03.

**Decision — option 2 of three:** skip stage 5 during market hours only
(`stage5_node`, flag `ANALYSIS_STAGE5_MARKET_HOURS=1` restores always-on).
Removes 100% of the trading-path cost; off-hours filings (the majority) keep
feeding the eval, so the measurement series survives.

**CAVEAT — recorded, not silent:** from 2026-08-03 the signal-accuracy series
is **off-hours only**. Evening/results filings differ in event mix from
intraday ones, so post-cutoff accuracy stats are NOT strictly comparable with
the historical all-hours baseline (incl. the 2026-07-04 coin-flip audit). Any
future signal-quality claim must either split at this date or restore the flag
for a clean window. The full-coverage fix without latency cost is §4's
publish-then-signal design (second Kafka message + relay UPDATE), unbuilt.

Verified: 4/4 gate cases (market-hours skip / off-hours run / override in both
regimes); suite 90 passed / 1 skipped / 4 xfailed.

## 6. Not addressed by latency work (but bigger on BLUEJET)
Our marks: t0 597.05 → t+5m 585.65 → **t+15m 581.20 (-2.7% in our favour)** →
t+30m 599.00 → now 593.60. There was another 2.7% available AFTER entry that
the exit policy gave back. On this trade the exit rules cost more than the
84s entry delay. Candidate: tighter trailing stop inside the first 30 minutes
of a mag>=4 event. Not implemented, not validated.
