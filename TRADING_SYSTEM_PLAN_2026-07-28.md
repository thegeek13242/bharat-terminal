# Internal Automated Trading System — Build Plan

Drafted 2026-07-28. INTERNAL USE ONLY. Nothing in this plan is customer-
facing; the November confirmation gate and RA compliance Lane A are
unchanged. This plan sequences everything required before the first real
order; each phase has an explicit exit gate.

## 0. Verified starting position (audited 2026-07-28)

**Assets we actually have:**
- Latency stack: dual-exchange first-wins filings (BSE leads NSE 2–50 min),
  analysis pipeline processes a filing in ~60s, per-adapter liveness
  watchdogs, retry-not-lose ordering. This is the moat: minutes-scale
  event reaction while the tape is still absorbing.
- Signals with evidence:
  - **PEAD composite-SUE** (validated 2026-07-04: D10−D1 +2.11% @20d,
    t=4.7, 51 cohorts) — gross, pre-cost, current-universe.
  - **Materiality v4.1** (2026-07-28 lab): AUC 0.7242, top-decile 25.9%
    observed at 2.3× lift — the "worth acting at all" gate.
  - **Directional asymmetry** (2026-07-28, 3-day window): headwind
    classifications carry real signal (mag≥4: 75% down, median −2.61%;
    +18pp vs drift base); tailwind = market drift, zero directional edge.
    Needs a down-tape regime confirmation.
- Data quality infra: freshness monitoring, XBRL cross-validation, dedup,
  content-verified corpus; Angel One session management (LTP only).

**Gaps we verified today (no wishful thinking):**
- `fno_engine/` is EMPTY (scaffold only). No order-placement code exists
  anywhere in the repo. No live paper-trading system (`paper_trading.py`
  was a one-off study; no `paper_trades` table exists).
- `universe_membership`: 0 rows → every backtest so far has survivorship
  bias. Must be fixed before any strategy claim is trusted.
- LLM *directional* signals = coin flip (2026-07-04 audit; gates parked).
  That finding stands — direction comes from PEAD/headwind structure, not
  from the LLM.
- v4.1 is ~5pp overconfident in its top decile — fine for ranking, NOT
  usable for position sizing until calibrated.
- Single GCP VM, no failover; its history includes self-termination
  incidents. Acceptable for research; a dead-man problem for live money.
- SEBI/broker/algo compliance: entirely unresolved.

## 1. Non-negotiable principles (from this project's own scar tissue)

1. **Fail closed on staleness.** The system's characteristic failure mode
   is stale data producing confident output. With money attached that is
   the one unforgivable bug: any stale upstream heartbeat, any filing older
   than a threshold, any price feed gap → NO ORDER. Silence over action.
2. **Exactly-once order semantics.** The mark-seen-after-publish lesson,
   applied to orders: idempotency keys on every order intent, place-then-
   record with broker reconciliation on every startup and on a timer.
   A crashed process must never double-order or orphan a position.
3. **Pre-registered evaluation.** Success criteria for every strategy are
   written down BEFORE the test runs (this document, §C3/§E). No moving
   goalposts, no p-hacked strategies. The no-improvement rule extended:
   a strategy ships only if it beats doing nothing, net of ALL costs,
   out-of-sample, at pre-declared significance.
4. **Full decision provenance.** Every order traceable to (filing id,
   model artifact version, feature vector, quote at decision, rule that
   fired). Also a SEBI algo-audit requirement, not just hygiene.
5. **Research/production separation.** Production trades run FROZEN model
   artifacts with version stamps. The lab retrains; production upgrades
   only through the champion protocol + a paper period.

## 2. Phase A — Compliance & founder decisions (BLOCKING; start now)

- **A1. Counsel engagement (blocking for Phase E, start immediately):**
  (a) SEBI retail algorithmic-trading framework — broker algo approval,
  algo registration/ID, static IP, order-rate thresholds, audit-trail
  obligations; (b) conflict analysis: trading the internal book while
  pursuing RA registration (front-running exposure, disclosure duties,
  restricted-list mechanics if/when customers exist). Deliverable: written
  constraints memo. NOTE: "internal use" does NOT exempt algo orders from
  the SEBI/broker framework.
- **A2. Broker path:** Angel One algo approval process, order APIs, rate
  limits, static-IP setup; confirm the personal-use licensing boundary
  covers own-account automated orders (LTP usage stays internal-only,
  never stored/broadcast). Fallback brokers if Angel One's algo process is
  unworkable.
- **A3. Founder decisions (everything downstream shapes around these):**
  1. Capital allocation for the pilot (suggest a number you can lose
     without flinching; scaling is gated later anyway).
  2. Instruments: cash-only long (simplest; cannot hold shorts overnight)
     vs F&O (~190-name universe; enables the headwind short — the single
     strongest directional finding — at the cost of margin/rollover/
     liquidity complexity).
  3. Max drawdown tolerance + daily kill-switch threshold.
  4. Ops budget: second VM / failover for the live phase, or accept
     dead-man-switch-only on one VM.

Exit gate A: counsel memo + broker path confirmed + the four decisions.

## 3. Phase B — Backtest foundation (weeks 1–3, parallel with A)

- **B1. Point-in-time universe.** Populate `universe_membership` from
  exchange archives (listings, delistings, suspensions, index membership
  over time). Blocking for honest backtests — currently 0 rows.
- **B2. Cost model.** STT + stamp + brokerage + exchange/SEBI fees +
  slippage model (spread- and ADV-aware), borrow/margin costs if F&O.
  Calibrate slippage initially from quoted spreads, later from Phase C
  measurements.
- **B3. Event-time backtester.** Replays: filing timestamp → signal →
  earliest realistic fill (conservative: next session VWAP for EOD
  strategies; never same-tick fills). Walk-forward evaluation, per-cohort
  reporting (results-season vs off-season regimes differ — proven), seed
  42, deterministic.
- **B4. Re-validate both candidate strategies net-of-cost on B1+B2:**
  - **S1 — PEAD long**: decile-10 composite-SUE longs, ~20d hold. The
    validated gross edge must survive costs + point-in-time universe.
  - **S2 — headwind short**: mag≥4 negative filings gated by v4.1
    materiality; needs (a) the down-tape regime confirmation, (b) the A3
    instrument decision. Backtest both overnight-F&O and intraday-cash
    variants.
  Exit gate B: for each strategy, net expectancy > 0 out-of-sample with
  pre-declared t ≥ 2, else it does not advance. Publish the numbers
  either way.

## 4. Phase C — Live paper trading (weeks 3–10)

- **C1. Paper-trader service** (new container): consumes the live impact
  stream; applies frozen v4.1 + strategy rules; writes order INTENTS with
  full provenance to a new `paper_trades` table; marks to market with
  Angel One LTP. No broker connection at all in this phase.
- **C2. Measure the loop reality:** filing→decision latency histogram;
  quote-at-decision vs assumed fill (true slippage); events missed because
  freshness gates tripped (they will — count them, don't hide them).
- **C3. Pre-registered success criteria (fixed NOW):** per strategy,
  ≥100 events, net-of-modeled-cost expectancy > 0 with t ≥ 2, max
  drawdown within the A3 tolerance, realized slippage within 1.5× the B2
  model. Expected duration 6–10 weeks including a non-results-season
  stretch. Falsification is a valid, valuable outcome.

## 5. Phase D — Execution & risk layer (weeks 4–10, parallel with C)

- **D1. Order manager:** state machine INTENDED→PLACED→ACKED→FILLED /
  REJECTED / CANCELLED / UNKNOWN; idempotency keys; startup + periodic
  reconciliation against broker positions/orders; a tested panic-flatten
  routine; UNKNOWN states page a human and halt new orders.
- **D2. Risk engine — hard gates, every one fail-closed:** per-trade
  notional cap; per-symbol cap; gross exposure cap; daily realized-loss
  kill switch; ADV-based sizing cap (≤ a few % of ADV); price-band /
  circuit-limit checks; freshness gates (any stale heartbeat or filing
  age > threshold → no order); trading-calendar/halt awareness.
- **D3. Ops for money:** dedicated alert channel; dead-man switch
  (auto-flatten + page if the trader's own heartbeat stops); append-only
  audit log; daily reconciliation report (positions, P&L, orders vs
  broker statement); resolve the A3 failover decision before live.

## 6. Phase E — Gated live pilot (month ~3, only after A–D gates)

- **E1. Entry gate:** paper criteria met for the strategy; counsel memo
  clear; broker algo approval done; D-layer reconciliation tested against
  real (manually placed) orders.
- **E2. Scope:** ONE strategy — whichever survived Phase C strongest
  (S1 long-only cash is the least complex candidate); pilot capital from
  A3; manual arm/disarm daily to start; per-order notification week 1–2.
- **E3. Tri-level reconciliation:** live vs paper vs backtest for the
  same events — divergence localizes the failure (model vs execution vs
  cost assumptions).
- **E4. Scaling gates:** each capital step-up requires another
  pre-declared batch of trades within expectations. No discretionary
  "it feels like it's working" scaling.

## 7. Phase F — Later (each item has its own gate)

- Headwind short via F&O (if not chosen in A3 initially).
- Probability-scaled sizing — REQUIRES fixing v4.1 top-decile
  calibration first.
- Specialist models + Nov confirmation re-run (existing gate).
- Intraday reaction strategies (needs tick storage + a faster decision
  loop — materially bigger infra; not before E is boring and stable).
- Commodity-shock and transcript-guidance triggers as event sources.

## 8. Explicitly out of scope

- Anything customer-facing (RA Lane A + Nov gate unchanged).
- LLM directional signals as triggers (coin flip; gate stands).
- Latency arms-race trading (we are minutes-scale event traders).

## 9. Immediate next actions

1. Founder: the four A3 decisions + start A1 counsel + A2 broker process.
2. Engineering (no approvals needed, research-only): B1 universe history
   backfill design, B2 cost model, B3 backtester skeleton — all offline.
3. Commit the materiality lab (harness + doc + ledger) as the research
   baseline this plan builds on.

Realistic overall timeline: ~3 months to a small, gated live pilot —
dominated by paper-trading duration and compliance lead times, not code.

---

## Build status — 2026-07-28 (working tree, NOT deployed)

Founder decisions received: cash + F&O; shorts via F&O and intraday cash.

**Research shipped (see MATERIALITY_LAB_2026-07-28.md #4):** D-series
directional baselines per band (drift-adjusted, 400d) + per-type map;
materiality gate refreshed (v4.1 tax22 artifact, fit through 2026-07-27).
Rule-relevant findings: headwind edge extends to mag≥3; results_negative
79.5% hit; order_win_positive is the only long-side class with edge;
tailwind stays OFF; rating keyword rule dead; next-day SUE-sign dead
(PEAD stays 20d composite-decile).

**Phase C build complete in working tree:**
- Migration 030 (`paper_trades` + `paper_equity_snapshots`) — NOT applied.
- `bharat_terminal/papertrade/` service: Kafka consumer (group `papertrade`
  on analysed.impact.in, offset=latest), gate-cascade rules with full
  provenance, idempotent positions (ON CONFLICT DO NOTHING), slippage-
  haircut fills via kb-service LTP, F&O universe from scrip master (Redis-
  cached, slow-CDN-safe timeouts), 60s MTM + exits (15:15 IST cash-short
  squareoff, 5% short stop, per-strategy holds), 300s equity snapshots,
  boot reconciliation. Strategies: headwind_short (mag≥3, materiality-
  gated ≥0.10, FUT-else-intraday-CASH), pead_long (results + trailing-P90
  SUE, 20d hold), orderwin_long (D0T finding, 5d hold, materiality
  recorded-not-gated), tailwind_long (built, OFF).
- API `/papertrading/{positions,summary,trades/{id}}` — internal-gated;
  summary = equity curve, max drawdown, Sharpe (null-honest), hit rate,
  profit factor, per-strategy stats.
- Dashboard `frontend/src/pages/PaperTrading.tsx` — tiles, equity curve,
  Open/Closed/Rejected tabs, expandable provenance audit view; internal
  nav gating mirrors Research.
- Verification: py_compile all green; compose config valid; tsc --noEmit
  zero diagnostics; SQL validated read-only against prod (vol60/SUE-P90/
  drawdown/downsample queries); image dependency build verified.
- PAPER stance documented in-code: materiality gate FAIL-OPEN with
  provenance flag (live must flip to fail-closed).

**Deploy batch awaiting approval:** commit all of the above → migration
030 via kb-migrate → build papertrade image from clean worktree → start
bharat-papertrade → rebuild/restart api + frontend for the dashboard.
