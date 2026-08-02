# Production Code Review + Test Suite Design — 2026-07-30

Scope: everything currently heading into / running in production —
the papertrade pilot (12 commits, live since 2026-07-28), router v2
(f5201ee), analyst-clone Phase 1 (e47dd52), and the 4 uncommitted
working-tree files (nightly dossier auto-refresh + LLM cost
instrumentation). Method: four parallel deep reviews with live-DB
verification (read-only), per CLAUDE.md every claim below is backed by
code reads, `git show`, psql output, or container logs. Nothing was
modified; no service was touched.

**Repo test state (verified): zero test files, zero test tooling.**
No `tests/`, no `conftest.py`, no pytest in any `requirements/*.txt`,
no test job in CI. The only quasi-tests are the statistical harnesses
(Tier-B replay, nightly 19:05 classification eval, materiality lab).

---

# PART 1 — CODE REVIEW FINDINGS

## 1A. Papertrade engine — CRITICAL (pilot-integrity bugs)

These three corrupt the pre-registered pilot's numbers. All are
live-confirmed or code-verified, not hypothesized.

### C1. Queued fills execute at 09:00:02 IST — before the pre-open auction
`analysis/market_hours.py:10` opens at 09:00; the 5s `mtm_loop` fires
`fill_pending` on its first post-09:00 cycle. **Live-confirmed:**
today's trades #40–#47 (`filled_from_pending=true`) all have
`entry_at = 09:00:02`. The auction-open override (b5ba843) does not
fix this: at 09:00:02 the quote's `open` is absent/0 (auction matches
~09:07), the `q.get("open")` guard at `papertrade/main.py:249` fails
falsy, and the fill silently falls back to the stale pre-auction LTP
(≈ previous close) at `main.py:243`. Overnight-queued signals — the
majority, since results land after 15:30 — therefore fill at
yesterday's price and capture the gap the queue exists to exclude.
The same 09:00–09:15 window pollutes marks, trailing-stop and
squareoff evaluation.
**Fix direction:** don't fill until a real auction/continuous price
exists (e.g. gate fills on `open > 0`, or on `now >= 09:15` with the
open-price override, and exclude 09:00–09:15 from mark/exit logic).

### C2. Hold periods count calendar days; the validated PEAD edge is 20 TRADING days
`engine.py:509-552` — `held_days = (now.date() - entry.date()).days`
vs `PAPER_PEAD_HOLD_DAYS=20`. 20 calendar days ≈ 13–14 sessions, ~⅓
short of the pre-registered +2.11%@20-trading-day benchmark
(`research/pead_signals.py:4,55`). `orderwin_long` (5 "sessions") and
headwind holds are distorted the same way. The pilot's headline
comparison is currently apples-to-oranges, biased against the
strategy.
**Fix direction:** count trading days (distinct `price_points` dates
or a session calendar) — and note the change date in the pilot log.

### C3. Gross-exposure cap bypass up to ~19× in the pending-fill path
`engine.py:351-373` — `fill_pending` checks the gross cap on the
cash-sized qty *then* inflates FUT qty to `max(1 lot)` (₹5–19L
contract) with no re-check. Near-cap book + overnight FUT headwind
signal → checked as +1L, actually +19L. `open_position` has the
correct order (lot first :247-257, then gross :266-272); only
`fill_pending` is wrong.
**Fix direction:** quantize first, then cap-check — ideally by
extracting one shared `size_order()` used by both paths (see Part 2).

## 1B. Papertrade engine — MAJOR

| # | Location | Finding |
|---|----------|---------|
| M1 | `engine.py:226-229` + shared `ON CONFLICT ... DO NOTHING` key | **Transient quote failure → permanent unretryable reject.** A kb-service blip during session inserts a `no_ltp` REJECTED row under the same `(impact_id,symbol,strategy)` key an open would use; every later attempt hits DO NOTHING. One such row already exists live. A 10s outage during a results burst permanently deletes that evening's during-session signals from the pilot. |
| M2 | `engine.py:328-339` | FUT pending fills of high-priced symbols (notional > queue size) rejected `zero_qty` before lot logic runs — inconsistent with `open_position`, and the reject is permanent per M1. |
| M3 | `market_hours.py:21-24` | **No NSE holiday calendar** (Mon–Fri only). On a holiday (Aug 15 upcoming), fills/exits/marks may run against frozen prices and `held_days` accrues. Angel One holiday quote behavior unverified; the missing guard is verified. |
| M4 | `engine.py:266-272`, `rules.py:196-200,436-450`, 4 writer loops | Gross cap and concentration guard are **non-transactional check-then-act** across concurrent writers (consume_loop / mtm_loop / pead_scan_loop / hedge_loop). Kafka-pead and scan-pead use *different* impact_id keys for the same (symbol,strategy) → the unique constraint does not prevent 2× notional on one name. Sub-second windows; nonzero on filing-burst evenings. |
| M5 | `main.py:359-418` | **Hedge failure = silent unhedged book.** Gross-cap-rejected hedge retries every 300s, each retry inserting a new REJECTED row (unbounded spam) with no alert; missing NIFTYBEES LTP → WARNING and continue. Also notional-matched (β=1), not a true beta hedge — restate in pilot readouts. |
| M6 | `main.py:257-271`, `engine.py:468-505` | **Fail-loud LTP guard covers only the all-symbols-dry case.** One symbol missing from the token map is silently never marked, never stop-evaluated, feeds stale `mtm_price` to the dashboard and gross calc — zero log signal until a time-exit is due. A cash short in this state violates the intraday-only rule indefinitely. |

## 1C. Papertrade engine — MINOR
- Exits carry no slippage haircut (entries take 10bps) — every closed
  trade flattered by half a round-trip (`engine.py:143-151`,
  `api/routes/papertrading.py:389-400`).
- Gap guard computes vs `price_points` close; a stalled EOD ingest →
  false (and per-M1 permanent) `gap_guard` rejects; ignores the
  `prev_close` already in the Angel quote payload.
- `_VOL60_SQL` has no minimum sample count — a 3-row listing yields a
  noise vol60 → near-zero or huge trailing distance.
- At-most-once Kafka consumption (`enable_auto_commit=True`, 1s);
  crash between commit and insert loses the signal; bare `async for`
  → an unexpected consumer exception tears down the whole service
  (docker restarts it, at the cost of the known rebalance storm).
- `filled` counter increments even on 0-row status-guarded UPDATE.
- **16 live `pead_long` rows have `exit_reason='reset'` — a value no
  code path emits.** A manual DB write inside the pre-registered
  dataset with no provenance stamp. Needs a documented convention
  (or a `manual_*` prefix rule) before the pilot readout.
- Symbol-case coupling: quotes keyed by `sym.upper()`, lookup by raw
  `ci.symbol` → a lowercase symbol from an ImpactReport is a silent
  permanent `no_ltp` reject.

## 1D. Papertrade — verified sound (no action)
Idempotency key always non-NULL and effective; PnL sign math correct
both sides; mark-then-exit ordering re-reads rows (fresh trail
anchors); a new extreme can never self-trigger the trail; boot
recovery genuinely reloads OPEN+PENDING from DB; manual-exit races
settled by status-guarded UPDATEs; dashboard null-honest stats;
snapshot equity computed in one statement.

## 1E. Router v2 / analyst-clone

Deployment state verified: migration 033 applied; `company_type`
populated 3,998/3,998; router v2 live (417 log lines/8h, suppressions
firing correctly); MAHSCOOTER end-to-end returns holdco 0.85 + fresh
market state. Determinism verified clean (pure regex/dict, no
randomness, no set-iteration in decisions).

| # | Sev | Location | Finding |
|---|-----|----------|---------|
| R1 | HIGH | `grounding/queries.py get_market_state`; `router_v2.py:391-402,493-504`; `kb/api.py:177-189` | **Market-state staleness unchecked by every consumer** — zero `as_of` checks in `analysis/` despite the query docstring demanding one. A symbol that exits surveillance may produce no new row (219/2,917 of today's rows have `price_band IS NULL`), so its last GSM/ASM stage sticks forever; if the (installed-today, never-yet-fired) 08:30 cron rots, all state freezes silently and "GSM stage IV" is injected into prompts as fact indefinitely. |
| R2 | MED-HIGH | `router_v2.py:297-374` | **Para-A floor false-fires on clarifications/denials and housekeeping** — verified by direct test: "Clarification regarding news item on alleged fraud" → floored to mag 3; same for regional-exchange delisting housekeeping and routine SHA amendments. No clarification/denial guard exists. Each false fire is a deterministic magnitude distortion. |
| R3 | MED | `stage3_impact.py:543-557` | Para-A floor applies to **every** scored symbol of a multi-symbol wire item — "X promoter arrested; lenders Y, Z in focus" floors Y and Z to 3. |
| R4 | MED | `pipeline.py:252-253` | `priced_in` can downgrade a floored magnitude after the floor fires, while `para_a_floor_applied=True` stays stamped — provenance contradicts the published number. |
| R5 | MED | `company_type_sync.py:377-389` | **Holdco recall gap outside the nbfc taxonomy**: BBTC (Bombay Burmah) is `operating @ 0.95` — operating-conglomerate holdcos silently get industrial-results treatment on standalone results; residual MAHSCOOTER-pattern exposure. 44.2% of actives have NULL `revenue_ttm_cr`, disabling the mcap/rev leg. |
| R6 | LOW-MED | `stage3_impact.py:393-397,578-590` | `router_meta.scored` lies on LLM failure — meta stamped before the try-block claims symbols scored when the report has zero impacts. |
| R7 | LOW-MED | `stage3_impact.py:102-111` | Suppression fails open when KB is down (2s timeout, swallowed) → fund schemes route to the impact LLM as equities. |
| R8 | LOW | various | Non-dict classification raises pre-try `AttributeError` (item dropped, caught by `_process_rt`, loop safe); `"NoBand%"` prompt cosmetic; legacy substring buckets ("loa"→"loan", "agm"→"fragment") pollute bucket stats only; `scenario_v3` flag-off guard is a bare `assert` — stripped under `python -O`; GSM parser drops the stage if NSE ever rewords away the literal "GSM" token (0 occurrences today). |
| R9 | INFO | cron | Neither analyst-clone cron (market_state 08:30 wkdy, company_type Sun 10:00) has ever fired — installed today, first proof tomorrow 08:30. The commit's claimed "107 asserts green" test file **does not exist in the repo** — that verification is unreproducible today. |

## 1F. Uncommitted working tree (dossier auto-refresh + cost instrumentation)

Reminder: these 4 files are NOT deployed and per the approval-gate
rule must not be until explicitly approved. This change also
**reverses the "dossier generation is manual-only" policy** — nightly
unattended pro-model spend. Stating loudly, per policy.

Verified sound: new `LLMCallRecord` fields are safe (JSONL-only sink,
no reader, all call sites keyword-args); the staleness SQL type-checks
(`date > date`) and runs (153 truly stale today, cap 100/night);
`_next_occurrence` is IST-aware despite the UTC container; all
imports resolve; `gemini-3.5-flash` and the dossier model are both in
`MODEL_PRICING`; token *cost* is not double-counted; the monitor path
correctly interprets `research_company`'s return (no freshness gate
exists inside it — the 90-day gate lives only in `research/main.py`,
so stale-by-quarter/fresh-by-date DOES regenerate, as intended).

Findings:

| # | Sev | Finding |
|---|-----|---------|
| U1 | MAJOR | **`source_quarter` is LLM-supplied** (`dossier_agent.py:625`, coerced in `research/db.py:374-379`). Model emits an old quarter → row re-refreshed **every night forever** (unbounded spend on one symbol); model emits unparseable → NULL → `mx > NULL` is NULL → **never refreshed again**, silently. Fix: set `source_quarter` from `max(financial_results.period_end)` server-side (or clamp/validate the LLM value against it), and add a `generated_at > now()-interval '1 day'` exclusion as a belt-and-braces re-refresh brake. |
| U2 | MAJOR | **Nothing limits spend but an alert.** Guardrail is log/webhook-only (no kill-switch, `HOURLY_COST_BUDGET_USD` default $5 unset in .env), and it's a per-process singleton — the kb-monitor container's window never sees analysis spend and vice versa. First night ≈ $15 burst (153×~$0.10, cap 100). Acceptable only if deliberate; consider a hard per-run USD ceiling computed from the cost log before gathering. |
| U3 | MAJOR | **The cost JSONL in `bharat-kb-monitor` is on the ephemeral container FS** (0 mounts verified) — every restart deletes the cost log the guardrail and any audit depend on. Mount it or point `LLM_LOG_FILE` at a volume. |
| U4 | MINOR | `web_search_calls` double-count confirmed: one search → +1 on the `dossier` row AND a `dossier_grounding` row with `web_search_calls=1`. `SUM()` across stages inflates 2×; the counters also disagree on failure paths in opposite directions (grounding logs only on success; the agent misses a search issued in the submit turn). Document "sum only `dossier_grounding`" or drop one counter. |
| U5 | MINOR | `ORDER BY lf.mx DESC` with no tiebreaker → non-deterministic cut among the many rows sharing one period_end; add `, d.symbol` for reproducibility. The 23:00 late-filing retry has no happens-before guarantee vs 23:30 (benign: missed symbol caught next night — but the docstring overclaims). |
| U6 | INFO | Google Search per-query grounding fee still unpriced in `compute_cost` (known; `web_search_calls` tag is the hook). |

## 1G. Operational landmines noticed in passing (triage, not fixed)
- **`papertrade` and `transcript-watcher` are NOT in the systemd unit**
  — the live pilot does not survive a VM reboot. (`/etc/systemd/system/
  bharat-terminal.service` vs `docker ps`.)
- `sentiment/classifier.py:12` references topic
  `raw.sentiment.unclassified.in` which `redpanda-init` never creates.
- `scripts/run_signal_eval.sh` cron expired 2026-07-07 — now a no-op.
- `requirements/fno_engine.txt` orphaned (module deleted in 752081b);
  `fno_engine/` contains only a stale `.pyc`.
- Host-3.14 `__pycache__` artifacts inside the repo tree — someone has
  imported modules with host python; version skew trap.

---

# PART 2 — TEST SUITE DESIGN

## 2.1 Ground rules (from verified constraints)

1. **Tests must run in-container.** No host venv; host python is 3.14
   vs container 3.12; deps live only in images. Vehicle = the idiom
   already in use by the lab/replay harness:
   ```
   docker compose run --rm \
     -v $PWD/bharat_terminal:/app/bharat_terminal:ro \
     -v $PWD/tests:/app/tests:ro \
     --entrypoint python research-runner -m pytest tests -q
   ```
   (research image has the widest dep surface; add
   `requirements/test.txt`: pytest, pytest-asyncio, hypothesis,
   freezegun, respx — and install it in the research image or a
   dedicated `test` compose profile.)
2. **CI alone gates nothing** — prod changes are per-service rebuilds
   on the VM, not PRs. The suite needs BOTH: a CI job (add next to
   `lint-python` in `.github/workflows/ci.yml`, same 3.12 setup) AND a
   one-command pre-deploy script on the VM, e.g. `scripts/test.sh`
   wrapping the compose invocation, to be run before any
   `docker compose up -d --build <svc>`.
3. **Determinism**: fixed seeds visible everywhere; no wall-clock in
   assertions — every time-dependent function already accepts or can
   accept an injected `now` (`is_market_hours(now=)`,
   `_exit_reason(..., now_ist)`).
4. **Complement, don't duplicate, the statistical harnesses.** The
   ledger + pre-registration culture (materiality lab, Tier-B replay,
   nightly eval) stays authoritative for model quality; pytest owns
   fast deterministic logic and plumbing.

## 2.2 Tier 0 — pure unit tests (no network, no DB, <5s total)

The highest-value targets, with the edge cases each must pin.
Every CRITICAL/MAJOR finding above gets a named regression test
(listed in 2.5).

**Money path (write these first):**

| Target | Edge cases to pin |
|--------|-------------------|
| `PaperEngine._exit_reason` (engine.py:508, pure static) | squareoff at exactly 15:15:00 / 15:14:59; overnight-survivor branch; `pead_hedge` exemption; trailing math long+short; `entry_vol60` None/0 → flat-stop fallback; per-strategy holds (pead 20 / orderwin 5 / else 5) **as trading days once C2 is fixed** — write the test to the intended trading-day semantics, currently xfail; pead/orderwin stop-exemption |
| order sizing (extract shared `size_order()` from `open_position` :247-272 + `fill_pending` :328-373) | lot quantization `floor(want/lot)` incl. want<1 → 1 lot; qty rounds to 0 (cash); MRF-class FUT pending (M2); slippage sign by side; **cap-check AFTER lot inflation (C3 regression)**; reject-ladder ordering |
| `_pead_gap_check` (engine.py:384, stub fetchval) | gap exactly 2.00% (strict >); missing/zero prev close fails open; stale prev-close date |
| `RulesEngine.evaluate_report` (rules.py:216, fake pool + fake fno_supplier) | freshness at age exactly 900s; FILING_SOURCES restriction; HEADWIND_MIN_MAG=4 boundary; four enable flags; materiality fail-open provenance; dup rejection |
| `is_market_hours(now=)` (market_hours.py:14) | 08:59:59 / 09:00:00 / 15:30:00 / 15:30:01; Sat/Sun; naive vs aware input; **holiday case — xfail until M3 lands** |
| mtm cycle body (extract from main.py:227-278) | auction-open override at 09:00 (open absent → must NOT fill, C1 regression), 09:08 (open present → fill at open), 09:45; single-symbol-dry detection (M6); dry-cycle counter |
| `plan_hedge` (extract from main.py:359-418) | tolerance band boundaries; unwind overshoot; empty book full unwind; flap-freedom (two consecutive runs → no-op) |

**Routing / analysis:**

| Target | Edge cases |
|--------|-----------|
| `router_v2.classify_event` | one fixture per bucket + contamination classes (tax orders, ESOP, NCD, results-in-wrapper, 40k body cap); the legacy substring traps ("Voting results for the AGM") as documented-behavior tests |
| `router_v2.detect_para_a` | one per leaf × per guard; **R2 regressions**: clarification/denial headlines, regional-exchange delisting, SHA amendment — xfail until guarded |
| `apply_para_a_floor` | (2,True)→(3,True); (3,True)→(3,False); (5,False) passthrough |
| `route` + `build_router_meta` + `scenario_v3/scenario_block_v3` | golden-file test with the captured real `/company/MAHSCOOTER` payload; flag-off byte-identity vs legacy composition; non-dict classification (R8) must not raise once hardened |
| `scenario_rubrics.detect_scenario` etc. | shared fixture corpus with the nightly eval — a behavior change here silently invalidates both eval baselines, so pin current outputs; `lender_addendum` monotone-additive property |
| `market_state_sync.parse_asm/gsm/esm/sec_list/build_rows` | decoy `gsmStage`; "LTASM I & GSM 0"; stage VI; no-"GSM"-token case (R8); band NULL |
| `company_type_sync.classify_primary/attach_overlays/_mcap_rev` | MAHSCOOTER/BAJAJHLDNG → holdco; WESTLIFE guard; Summit name-vs-dividend precedence; NULL mcap/revenue; BBTC as documented-miss (xfail, R5) |
| `analysis/materiality.py` all 3 fns; `filing_policy.classify`; `signal_gates`; `priced_in.compute_downgrade`; `conviction.compute_conviction` | ₹-scale regexes (the classic bug surface); crores vs lakhs vs million strings; feed-noise |

**Ingestion / KB / data integrity:**

| Target | Edge cases |
|--------|-----------|
| `nse_financial_results._rounding_divisor` + `classify_taxonomy` + `parse_xbrl` + `_extract_financials` + `filter_consolidated_quarterly` | fixture XBRL per taxonomy (use official .xlsm-derived samples); lakh/million/absent units; **the 2026-06-25 scale-bug corpus as regression fixtures**; consolidated-vs-standalone selection |
| `ingestion/dedup.py` full module | tracking-param strip; query-sort; symbol-scoped `_content_key` (ACC/AMBUJACEM same concall); simhash hamming at exactly threshold 3; `DedupFilter` with fake redis |
| `research/pead_signals.shift_quarter/compute_sue/pead_signal_id` | quarter arithmetic across year-end; <4 history points; zero-variance history; id determinism |
| `commodity/signal_generator` 5 pure fns | sign tables both directions; zero beta; z at threshold |
| `kb/dcf.py` | golden-value run; negative FCF; zero growth |
| `llm/wrapper.py` pure layer: `_gemini_translate`, `_wrap_gemini_response`, `_wrap_anthropic_response`, `assistant_content` (thought-signature replay), `provider_for_model`, `_is_rate_limited`, `_extract_sources` | usage-field absence (thinking/cached None→0); malformed toolcall; U4 documented-behavior test on the double-count |
| `analysis/llm_logger.compute_cost` + contextvar buckets | unknown-model fallback ($1/$3 — pin it so it can't silently change); real `_pilot*.jsonl` records as fixtures; child-task shared-bucket invariant |
| `types.py` pydantic schemas | round-trip serialize/deserialize for every Kafka-topic payload — one test protects all 8 topics |
| monitor staleness SQL (as a string constant + logic test) | NULL `source_quarter` exclusion (U1 documented behavior); deterministic ORDER BY (U5, after fix) |

## 2.3 Tier 1 — component tests with fakes (no real services, <30s)

- **PaperEngine end-to-end with a fake asyncpg pool** (in-memory dict
  of rows implementing fetch/fetchrow/fetchval/execute): open →
  mark → trail → exit lifecycle; fill_pending lifecycle incl. C1/C3
  scenarios replayed from a recorded morning quote snapshot;
  boot `reconcile_on_boot`; M1 retryability semantics (once fixed:
  transient no_ltp must NOT consume the idempotency key).
- **Guard concurrency (M4):** two concurrent `open_position` tasks
  against the fake pool asserting the cap holds once the fix
  (transaction/advisory lock) lands — xfail today, proves the race.
- **`marks.get_quotes` via httpx.MockTransport:** 16-symbol batching,
  partial-batch failure, case normalization, absent symbols.
- **`live_price.get_current_ltps` with fake redis** (already
  injected): token-cache refresh, fail-open path.
- **Kafka serialization only** (no broker): producer payload →
  consumer parse for each topic pair, using `types.py` fixtures.
- **LLM wrapper with patched `_gemini_client`/`_anthropic_client`
  factories** (patch module attributes, not env — config is read at
  import): retry ladder, timeout degrade of `grounded_search`
  (asserts no cost record on failure — U4), malformed-toolcall nudge.

## 2.4 Tier 2 — integration against ephemeral infra (minutes, pre-deploy + CI)

Compose already defines everything needed; add a `test` profile with
a throwaway postgres (tmpfs) + `alembic upgrade head`:
- Migration smoke: `alembic upgrade head` from empty → matches
  `\d` snapshot of prod-critical tables (`paper_trades`,
  `market_state`, `company_dossiers`, `financial_results`).
- Real-SQL checks that fake pools can't cover: `_GROSS_SQL`,
  `_OPEN_SQL` ON CONFLICT semantics across the two pead idempotency
  keys (M4), `_MARK/_CLOSE_SQL` sign math, the dossier staleness
  query against seeded rows (incl. NULL source_quarter).
- `results_sentiment_eval(dry_run=True)` against seeded fixtures —
  the ready-made seam; asserts the confusion-matrix math.
- API contract: FastAPI TestClient on `api/routes/papertrading.py`
  (null-honest stats, manual exit status guard).

## 2.5 Regression tests pinned to findings (the contract)

Every finding above ships with a named test, xfail until its fix
lands, then flipped: `test_c1_no_fill_before_auction_open`,
`test_c2_holds_count_trading_days`,
`test_c3_gross_cap_after_lot_quantization`,
`test_m1_transient_no_ltp_is_retryable`,
`test_m2_fut_pending_min_one_lot`, `test_m3_holiday_no_session`,
`test_m4_concurrent_opens_respect_cap`,
`test_m6_single_symbol_dry_alerts`,
`test_r1_market_state_staleness_gate`,
`test_r2_para_a_ignores_clarifications`,
`test_r3_para_a_floor_single_symbol`,
`test_u1_source_quarter_server_side`, `test_u5_deterministic_order`.
This converts the review into a durable spec instead of a one-off
document.

## 2.6 Statistical harness hardening (Tier 3 — keep, don't replace)

- Tier-B replay: persist SHA-256 of each generated prompt (or the
  prompts) in the output JSON — currently a rerun cannot reconstruct
  what produced the ledgered deltas; scope the "deterministic" claim
  to sampling/scoring (temp-0 LLM ≠ deterministic).
- Nightly eval + replay both depend on `detect_scenario` — the shared
  fixture corpus in Tier 0 is what makes changing it safe.
- Keep `COST_ABORT_USD`-style ceilings; add one to the dossier
  refresh (U2).

## 2.7 Rollout order and gates

1. **Week 1 — stop the bleeding (no test infra needed):** fix C1, C2,
   C3 + M1 (they're small, localized diffs); land
   `requirements/test.txt` + `scripts/test.sh` + `tests/` with the
   money-path Tier 0 tests and the C1–C3 regressions green.
2. **Week 2:** remaining Tier 0 (routing, XBRL, dedup, wrapper) +
   Tier 1 fake-pool engine lifecycle; CI job added; adopt the rule
   "no `docker compose up -d --build` without `scripts/test.sh`
   green".
3. **Week 3:** Tier 2 ephemeral-postgres profile in CI; guard-race
   fix + M4 test flipped; R1 staleness gate + U1 server-side
   source_quarter with their tests.
- Coverage target: not a % — the gate is "every CRITICAL/MAJOR
  finding has a green (non-xfail) regression test, and every pure
  function in 2.2 has at least its boundary cases".

## 2.8 Explicitly out of scope for pytest
LLM output quality (owned by the ledger/pre-registration harnesses),
NSE/BSE live-endpoint behavior (owned by the sync monitors +
`sync_log`), Grafana/guardrail alerting (observability, not tests),
frontend (no test tooling exists; separate decision).

---

*Prepared 2026-07-30. Read-only review; no code, config, DB, or
service was modified. The 4 uncommitted files remain undeployed and
gated on explicit approval.*
