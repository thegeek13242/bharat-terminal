# Research Factory — Specification v2 (Bharat Terminal adaptation)

**Date:** 2026-09-12 · **Status:** DRAFT for founder sign-off — design approved 2026-09-11;
**nothing in this document is built or deployed yet.**
**Supersedes:** `/home/ec2-user/autonomous_ai_quant_research_factory_spec.md` (v1, generic, US-flavoured).
**Internal only** (RA Lane A). No factory output is customer-facing.

Tags used throughout, per CLAUDE.md "claims must match reality":
- **[VERIFIED]** — checked on 2026-09-11/12 by query, file read, or official doc (source named).
- **[DESIGN]** — what we will build. Not evidence that anything works.
- **[ASSUMPTION]** — a number we are using until measured. Each has a verification owner in §27.
- **[ILLUSTRATIVE]** — example payload values. **Not results.**

---

## 0. Founder decisions (2026-09-11)

| Decision | Choice |
|---|---|
| Agent host | AWS Bedrock AgentCore: Runtime + Gateway + Policy (Cedar) + Evaluations + Observability. Code Interpreter = later. |
| Models | Bedrock **Opus 4.6** and **Opus 4.5** pools only (both unused by production). Enforced by IAM model-ARN allowlist on agent roles. |
| Git | Agents may open PRs; the founder merges. Autonomy covers ledger *data* only. |
| Budget | $3,000 = 90-day runway of **new** spend. Existing production LLM bill excluded. |
| Standing rule | Every commit, push, deploy, migration, production write, IAM or AgentCore change needs per-instance founder approval. |

## 1. Verified starting position

### 1.1 Data [VERIFIED 2026-09-11/12, read-only psql]

| Source | History window | Notes | Tier (§7.1) |
|---|---|---|---|
| `price_points` | 2017-11-28 → 2026-09-11 (3,672,681 rows, 3,923 symbols) | Unadjusted closes; ~3,740 symbols/day | H |
| `earnings_surprises` | 2020 → 2026-09-11 (43,082) | `study_version=pead_v2.1_2026_07`; liquidity_pass 36–68% by year | H |
| `financial_results.announced_at` | 2018-05-21 → (58,158 of 106,823 filled) | Rows revised in place → not point-in-time | H (restricted) |
| `expectations` | as_of 2023-04-06 → 2026-08-31 (134,087) | Append-only, `inputs_max_ts` | H |
| `concall_transcripts` / `transcript_guidance` | announced 2024-04-05 → | Two-model structuring corpus (Haiku + Sonnet) | H (short) |
| `shareholding_patterns` / `investor_holdings` | quarter_end 2011-06 / 2016-12 → | Coverage of recent quarters incomplete | H (coverage caution) |
| `corporate_actions` | 2001 → 2026 (20,339) | One bogus `0024-09-20` ex_date. CLAUDE.md "empty" landmine is stale. | H |
| `news_items` BSE_FILINGS | 2024 (2,817) · 2025 (4,021) · 2026 (32,474) | Thin before 2026 | F |
| `news_items` NSE_FILINGS | 2026 only (36,268) | NSE subcategory 0% filled; BSE ~99.6% | F |
| `impact_reports` | **2026-05-10 →** (91,678) | No `event_type` column; `router_meta`/`event_scope` not persisted | F |
| `analysis_audit` | 2026-06-27 → | | F |
| `market_deals` | 2026-06-15 → | | F |
| `market_state` (ASM/GSM/ESM, bands) | **2026-07-30 →** | | F |
| `participant_oi` | 2026-05-13 → | | F |
| `paper_signal_marks` | 2026-08-13 → (27,246) | t0/5/15/30/60-min absorption marks | F |
| `universe_membership` | **0 rows** | Only 25 of 3,923 symbols ever stop pricing → survivorship bias | — |

**Timestamp integrity [VERIFIED].** `earnings_surprises.announced_at` was compared with the
earliest RESULT `news_items.timestamp_utc` for the same symbol over 45 days (503 matched rows).
Most gaps sit at 0–0.5h, but there is a **sharp spike at +5.5h (99 rows)**. That pattern is IST
wall-clock time stored as UTC on a subset of rows. A mislabelled timestamp is *later* than the
truth, so it biases next-session entries late (conservative). It makes intraday entry rules
wrong.

**Calendar [VERIFIED].** `analysis/market_hours.py` only covers 2026 (`_CALENDAR_YEARS={2026}`).
Historical backtests therefore take their trading calendar from the dates present in
`price_points`.

**Entry-logic hazard [VERIFIED, code read].** `grounding/queries.get_forward_returns` enters at
"first close with date >= from_date". For evening filings that close is *before* the filing, so
it cannot be traded. The factory backtester does not reuse this entry logic.

### 1.2 Paper trading [VERIFIED]

Ledger was reset 2026-08-13. Strategies are hardcoded in `papertrade/rules.py`. The cost model is
10 bps slippage only. There is no drawdown kill switch.

| Strategy | Closed | Mean net/trade | SE | t | Hit | By exit |
|---|---|---|---|---|---|---|
| orderwin_long | 134 | −1.43% | 0.37% | ≈ −3.9 | 29% | trail_stop 97 @ −2.11%; time_exit 37 @ +0.33% |
| headwind_short | 19 | −0.35% | 0.88% | — | 47% | squareoff 16 @ +0.79%; trail_stop 3 @ −6.46% |
| pead_long | disabled | | | | | |

`orderwin_long` fails its pre-registered bar (≥100 events, net > 0 at t ≥ 2). The D0T research
spec behind it had a 5-day hold with no stop; the paper engine applies trailing stops. Method:
return = pnl / (entry_price × qty); lot-multiplier effects not re-verified.

### 1.3 Platform, capacity and vendors [VERIFIED]

- **LLM spend:** `llm_call_log` $1,580.71 since 2026-08-02; $18–95/day over the last 10 days.
  `cached_tokens` = 0 on hot paths. Recorded failure rates 8–16% (cause unverified).
  The `service` column holds container hostnames.
- **Bedrock quotas (ap-south-1; us-east-1 shows identical numbers — whether it is a separate pool
  is unverified):**
  - Sonnet 4.6: 10 RPM, fully allocated to production.
  - Haiku 4.5: 10 RPM (production).
  - Sonnet 4.5: 10 RPM (production uses about 8).
  - **Opus 4.6: 5 RPM, 2.59M tokens/day — unused.**
  - **Opus 4.5: 5 RPM, 13.5M tokens/day — unused.**
  - Opus 5 / Sonnet 5 / Fable: 0.
- **Platform:**
  - No workflow engine: kb-monitor asyncio loops with in-memory state, plus 3 host crons.
  - LLM pacer is per process.
  - Email goes only through Power Automate FLOW 1. Its documented `Response` step comes *after*
    Send, so HTTP 200 `{"ok":true}` means Outlook accepted the mail. Unable to verify the live
    flow matches that document.
  - Alert webhook works (183 alerts / 30 days).
  - Prometheus scraping broken; Grafana up.
  - **CI red:** 2,505 ruff errors, no pytest job.
  - Host: t3.xlarge, 4 vCPU, 15 GiB, no swap, 95 GB disk free.
  - Postgres: `max_connections=100`, 29 in use; `bharat` role is superuser.
  - A stray `papertrade-run` container has used about 100% CPU since 2026-08-26.
- **AgentCore** ([regions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html),
  [pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)):
  - ap-south-1 supports Runtime, Gateway, Identity, Policy, Evaluations, Observability, Memory and
    built-in tools. The Web Search tool is not available in Mumbai.
  - Runtime: $0.0895 per vCPU-hour + $0.00945 per GB-hour; I/O wait is free.
  - Gateway: $0.005 per 1k invocations. Policy: $0.000025 per request.
  - Evaluations: built-in $0.0024 per 1k input tokens + $0.012 per 1k output; custom $1.50 per 1k
    evaluations. Dataset evaluation is in public preview.
  - Evaluations supports Strands and LangGraph via OTel/OpenInference.
  - Gateway→Lambda passes the tool name in `context.client_context.custom["bedrockAgentCoreToolName"]`
    as `Target___tool`.
  - The EC2 role today has **no** `bedrock-agentcore`, `pricing` or `ce` permissions.
- **DBOS Transact** ([repo](https://github.com/dbos-inc/dbos-transact-py)): MIT licence. Durable
  workflows and steps stored in Postgres, queues with concurrency and rate limits, cron, idempotent
  workflow IDs. No extra server.
- **Trading costs** (secondary sources; NSE's own page timed out → [ASSUMPTION] until verified):
  - STT: delivery 0.1% on buy and sell; intraday 0.025% on sell; futures 0.05% on sell; options
    0.15% on premium (F&O rates effective 2026-04-01).
  - Stamp duty on buy: 0.015% delivery, 0.003% intraday.
  - NSE transaction charge 0.00297%; SEBI fee ₹10 per crore; GST 18% on brokerage + exchange +
    SEBI charges.

### 1.4 Reusable code [VERIFIED, file reads]

| Code | Reused for |
|---|---|
| `research/pead_signals.py` | Incumbent composite-SUE recipe → EXP-000001 |
| `research/pead_expectations_ab.py` | Cohort D10−D1 method reference |
| `research/seasonal_baseline.py` | Walk-forward reference |
| `research/e1_composite.leakage_audit` | PIT audit pattern |
| `research/replay_harness._paired_bootstrap` | Seeded bootstrap pattern (seed 42) |
| `research/materiality_lab.event_type_v2` | 22-bucket event taxonomy |
| `exports/materiality_lab/ledger.jsonl` | 138 historical experiment rows (memory import) |
| `analysis/market_hours` (`IST`, `SESSION_FLOOR`, `is_trading_day`, `is_session_live`, `trading_days_between`) | Live session rules |
| `llm/embeddings.embed_texts` (Bedrock Cohere v4 @1024) | Memory embeddings |
| `kb/memory/retrieve.retrieve_memory` | Pattern only; factory memory is separate |
| `outreach/checks.split_sentences`, `reconcile_sentences` | Claim-reconciliation pattern |
| `analysis/guardrails.get_monitor` | Existing alert webhook |
| `research/db.py` engine factory (`command_timeout`) | Connection pattern |
| `scripts/test.sh` | In-container test runner |

**Missing:** FDR, deflated Sharpe, clustered/HAC standard errors, permutation/placebo, purged CV,
cost model, point-in-time universe, experiment registry. The 11 result JSONs dated 1–3 Sep in
`exports/materiality_lab/` have no producing script and cannot be reproduced.

---

## 2. Architecture decision: workflow-centric, not multi-agent-centric

A classic multi-agent design (eight peers negotiating) is **inferior here**, for four reasons:
- Every decision that matters (validity, promotion, capital) must be deterministic.
- Model capacity is 10 RPM total.
- Research is gated more by data integrity and sample accrual than by idea generation.
- One engineer must operate it.

The factory is therefore a durable workflow engine running deterministic steps. **Four LLM
agents** do bounded tasks inside those steps:

| Spec v1 agent | v2 disposition |
|---|---|
| Event Intelligence | **Existing analysis pipeline** (stages 1–5) + deterministic snapshot materialization. Not rebuilt. |
| Market Regime | Deterministic regime module (§7.6). No LLM. |
| Research Scientist + Experiment Designer | **Scientist agent** (proposes DSL). The compiler is deterministic. |
| Quant Analyst / Statistician | Deterministic stats engine. Interpretation goes into Adversary memos and report narrative. |
| Adversarial Research | **Adversary agent** (plans attacks, writes kill memo) + deterministic battery (executes). |
| Market Structure / Execution | Deterministic cost, liquidity and tradability module (§7.4). |
| Research Memory | Retrieval *tool* over the ledger + pgvector. Not an agent. |
| Chief Scientist | **Chief Scientist agent** (allocation memo). The gate engine enforces caps. |
| (new) Report narrative | **Reporter agent**, constrained to a facts payload, with claim reconciliation. |

## 3. Architecture

```
EC2 ap-south-1 (existing host)                                     AWS managed, ap-south-1
┌────────────────────────────────────────────────────────────┐    ┌───────────────────────────────┐
│ prod Postgres ──ro role──► snapshotter ──► parquet snapshots │    │ AgentCore Runtime (Strands)   │
│   (public schema)            (off-market, COPY, sha256)      │    │  scientist@N   adversary@N    │
│                                                              │    │  chief@N       reporter@N     │
│ factory schema (ledger) ◄── orchestrator (DBOS workflows) ───┼Invoke►  (each: own IAM role,      │
│                               gates · budgets · frontier     │    │   Opus 4.5/4.6 ARNs only)     │
│ backtest-worker (DuckDB/pandas, cpus≤1.5, off-market heavy)  │    └───────────────┬───────────────┘
│                                                              │                    │ MCP tool calls
│ research-tools API (FastAPI, private IP only) ◄── Lambda proxy (VPC) ◄── AgentCore Gateway
│                                                              │                 + Policy (Cedar, default-deny)
│ papertrade (+ generic dsl_strategy executor: shadow|paper)   │
│ report-worker ──► Power Automate FLOW 1 (email)              │   OTel/ADOT spans ─► CloudWatch
│             └──► PutMetricData ─► CW alarm ─► SNS email      │   Transaction Search ◄─ AgentCore
│ Grafana (existing) ◄── factory schema                        │   Observability ─► Evaluations
└────────────────────────────────────────────────────────────┘
```

**Deterministic, never LLM:**
- snapshotting and integrity gates
- DSL compile
- backtests, statistics, cost and liquidity model
- battery execution
- all status transitions, budgets, kill rules
- holdout access, embargo
- paper/shadow execution
- report numbers and the ACTION REQUIRED flag

**LLM:**
- hypothesis proposal and mutation
- attack planning and kill memos
- allocation memos
- report narrative
- later: feature-primitive PRs

## 4. Agents on AgentCore

### 4.1 Definitions

| Agent | Model (Bedrock) | Invoked by | Budget / task | Reads (tools) | Writes (one tool) | Must never |
|---|---|---|---|---|---|---|
| **Scientist** | Opus 4.5 (13.5M TPD) | `frontier_daily` slots | ≤8 turns, ≤$0.60 | search_memory, get_family_state, get_hypothesis, list_features, get_dataset_catalog, get_run_results, get_paper_feedback | `submit_hypotheses` (≤5 per call; compiler returns errors, ≤2 repair attempts) | run, score, promote, pick sweep winners |
| **Adversary** | Opus 4.6 (2.59M TPD) | candidate at SIMULATED gate; every OOS pass | ≤10 turns, ≤$1.00 | get_run_results, get_battery_catalog, search_memory, get_hypothesis, get_dataset_catalog | `submit_attack_plan` (≤8 catalogue attacks + kill memo citing run IDs) | change results, gates, thresholds |
| **Chief Scientist** | Opus 4.6 | daily 06:30 IST + weekly Sunday | ≤12 turns, ≤$1.50 | get_frontier, get_family_state (all), search_memory, get_paper_feedback, get_budget | `submit_allocation` (family budgets, kills, revisits with required new-evidence refs) | exceed global caps, lift embargo, change gates |
| **Reporter** | Opus 4.5 | `report_daily` 07:00 IST | ≤4 turns, ≤$0.40 | get_report_facts | `submit_report_narrative` | introduce any number, ID or claim absent from facts |

**Thinking and effort [DESIGN; API facts from the claude-api reference]:**
- Opus 4.6 uses adaptive thinking with `effort` high.
- Opus 4.5 is pre-4.6, so it uses `thinking.budget_tokens` (≥1024, < max_tokens), with `effort`
  medium/high.

**Escalation:** Bedrock offers no model above these, so escalation raises turns and effort. A
Scientist task goes to Opus 4.6 only if its family ranks top-3 and Opus 4.6 has >40% of its daily
token budget left.

**Throttling:** tasks wait in their queue. They are **never** rerouted to production pools, which
IAM makes impossible anyway.

### 4.2 Cedar permission matrix (Gateway policy engine, default-deny) [DESIGN]

| Tool (`research___*`) | scientist | adversary | chief | reporter |
|---|:--:|:--:|:--:|:--:|
| search_memory, get_hypothesis, get_family_state | ✓ | ✓ | ✓ | |
| list_features, get_dataset_catalog | ✓ | ✓ | | |
| get_run_results | ✓ | ✓ | ✓ | |
| get_battery_catalog | | ✓ | | |
| get_paper_feedback | ✓ | | ✓ | |
| get_frontier, get_budget | | | ✓ | |
| get_report_facts | | | | ✓ |
| submit_hypotheses | ✓ | | | |
| submit_attack_plan | | ✓ | | |
| submit_allocation | | | ✓ | |
| submit_report_narrative | | | | ✓ |

There is **no** tool for promotion, paper enrolment, orders, git, `.env`, production tables,
gate thresholds, embargo or budget caps. CI contains deny tests, for example "scientist →
submit_allocation is DENIED". Exact Cedar entity names will be confirmed at the Day 1–3 spike.

### 4.3 Agent version control

- An **agent version** = (model id, system-prompt sha256, tool-schema sha256, framework version,
  runtime endpoint). A change to any of these makes a new `agent_versions` row with status
  `challenger`.
- Prompts live in the repo (`bharat_terminal/factory/agents/<agent>/prompt.md`), so changing a
  prompt means a PR and a founder merge.
- A challenger becomes champion only if E1 is non-inferior and the null-world smoke test passes
  (§14). Until then it serves only a shadow 10% of tasks, and its outputs are recorded but never
  acted on.

## 5. Workflows and state machines

### 5.1 DBOS workflows [DESIGN]

| Workflow | Trigger | Steps (each a checkpointed `@step`) | Idempotency key |
|---|---|---|---|
| `snapshot_nightly` | cron 01:30 IST (off-market) | export tables → sha256 manifest → integrity gates → register dataset_version | `snap:<date>` |
| `frontier_daily` | cron 06:30 IST | deterministic ranking → Chief Scientist memo → caps check → create Scientist slots | `frontier:<date>` |
| `research_cycle` | per submitted hypothesis | compile → dedup → integrity/tier → backtest sweep → stats → battery → Adversary → **gate A** → [holdout OOS → **gate B**] → [shadow/paper enrol] | `cycle:<hyp_id>@<ver>` |
| `paper_feedback_daily` | cron 16:30 IST (after close) | pull paper/shadow observations → envelope compare → classify divergence → create follow-up research questions | `feedback:<date>` |
| `report_daily` | cron 07:00 IST, **own process and queue** | facts → render → Reporter → claim reconciliation → validate → store → send → metric | `report:<date>` |
| `eval_online_sampler` | hourly | sample agent sessions → AgentCore online evaluation → `eval_results` | `evalon:<hour>` |
| `repro_nightly` | cron 03:00 IST | re-execute random 5% of completed runs → compare result_hash | `repro:<date>` |
| `null_world_weekly` | Saturday 02:00 IST | permuted-return snapshot → full factory loop in a sandbox ledger namespace → FDR / power | `nullw:<iso-week>` |

**Queues and connections:**
- `agents_opus45` and `agents_opus46`, each concurrency 1.
- `backtest` concurrency 1 during market hours (09:00–15:45 IST), 2 otherwise.
- `report` isolated.
- DBOS system-DB pool ≤5 and app pool ≤5 (production uses 29 of 100 connections).
- Every agent step first checks **budget** (§18) and **snapshot freshness** (<26h, otherwise fail
  closed).

### 5.2 Hypothesis status machine [DESIGN]

Two tracks, chosen by data tier (§7.1). Transitions are written **only** by the gate engine role.

```
Tier H:  IDEA → SIMULATED → ROBUST → OUT_OF_SAMPLE → PAPER → PAPER_VERIFIED → PRODUCTION_CANDIDATE
Tier F:  IDEA → SIMULATED → SHADOW → SHADOW_VERIFIED → PAPER → PAPER_VERIFIED → PRODUCTION_CANDIDATE
Terminal from any state: REJECTED · INCONCLUSIVE (insufficient data; revisit when data accrues)
                         · QUARANTINED (integrity/leakage finding) · RETIRED (superseded by simpler child)
PRODUCTION: human-only, and blocked until Phase A (SEBI algo framework, broker approval) closes.
```

`SHADOW` = frozen spec, signals logged with marks and simulated fills. No capital, no position
limits, many strategies can run at once. It gives Tier F hypotheses a genuine forward
out-of-sample record. Report status labels map as: SIMULATED with a passing gate A → "PROMISING";
OUT_OF_SAMPLE → "OOS VERIFIED".

## 6. Hypothesis schema and experiment DSL

### 6.1 Hypothesis (YAML → pydantic `HypothesisSpec`) [DESIGN] — values [ILLUSTRATIVE]

```yaml
dsl_version: 1
id: H-000031
version: 1
parents: [{id: H-000012, version: 2, op: mutation}]   # mutation|combination|inversion|generalization|simplification|contradiction|followup
family: pead_liquidity
question: RQ-000007
horizon: swing                         # intraday | swing | medium
claim:
  description: "Top-decile composite-SUE surprises in liquid names drift upward over 5–20 sessions after next-session entry."
  mechanism: "Investors anchor on the naive headline; liquid names still under-react to revenue-conditioned surprise."
  falsified_if: "net abnormal return ≤ 0 or cohort t < 3 pre-holdout, or OOS sign flip."
universe:
  base: listed_equity_nse_bse
  min_median_traded_value_cr: 5.0      # 20-session median of close×volume, point-in-time
  exclude: [fund_scheme, suspended]
event:
  source: earnings_surprises
  clock: announcement_date             # date-level clock: announced_at IST date; intraday rules forbidden
  filters:
    - {feature: sue_composite@2.1, op: decile_ge, value: 10, reference: trailing_365d}
signal: {feature: sue_composite@2.1, direction: long}
entry: {rule: next_session_open, delay_minutes: 0}
exit:  {holding_days: [5, 10, 20], stop: none}
benchmark: liquidity_matched_etf       # NIFTYBEES | JUNIORBEES | MID150BEES (≥2022-05) | liquidity_matched_etf | universe_median
costs: {model: india_cash_v1, slippage: adv_bucket_v1, multiplier: 1.0}
controls: [liquidity_tier, results_season]
statistics: {unit: calendar_week_cohort, primary_metric: net_abnormal_return, min_events: 300, min_cohorts: 12}
validation: {holdout: sealed_h_v1, battery: default_v1}
```

`data_tier`, `spec_hash`, trial count and history coverage are **computed by the compiler**, not
written by the author.

### 6.2 DSL vocabulary [DESIGN]

| Field | Allowed values (v1) |
|---|---|
| `event.source` | `earnings_surprises`, `results_filings` (news RESULT first-seen; F), `impact_events` (impact_reports hop-0; F), `guidance_changes` (transcript_guidance; H-short), `shp_changes` (H), `deals` (F), `market_state_changes` (F), `commodity_shocks` (F) |
| `event.clock` | `announcement_date` (date-level), `exchange_first_seen` (dual-exchange first-wins timestamp), `analysis_ready` (impact report created) |
| `entry.rule` | `next_session_open`, `next_session_close`, `same_session_close_if_before(HH:MM)`, `t_plus_minutes(n)` (F only, needs marks), `preopen_auction` (F only) |
| `exit` | `holding_days` ⊂ {1,2,3,5,10,20,40,60}; `stop` ∈ {none, vol_trail(σ), flat(pct)}; `squareoff_intraday` |
| `direction` | long, short (overnight short requires F&O lot universe) |
| operators | `gt, ge, lt, le, eq, in, decile_ge, decile_le, pctile_ge, pctile_le, zscore_ge, zscore_le, changed_to` |
| sweep | `sweep: {param_path: [values…]}`, at most 24 points per experiment; every point counts as a trial |

### 6.3 Compiler checks (hard errors) [DESIGN]

| Code | Check |
|---|---|
| E001 | Feature or version not in registry |
| E002 | Feature not point-in-time for the chosen clock (`inputs_max_ts ≥ clock`) |
| E003 | Blocked clock/rule pair: `announcement_date` with any intraday entry; `announced_at` timestamp use at all while `integrity.ts_ist_as_utc` is open |
| E004 | Entry rule could execute before the information exists (e.g. `next_session_close` on the same date for evening filings) |
| E005 | Horizon/data mismatch (intraday needs marks → F tier; medium horizon needs ≥ holding window of history) |
| E006 | Insufficient pre-holdout events or cohorts (min_events/min_cohorts; Tier F: ≥100 events for SIMULATED) |
| E007 | Control coverage < 80% (e.g. `sector` — `sector_nse` 18.8% filled → rejected) |
| E008 | Parameter out of range / unknown operator |
| E009 | Explicit dates overlapping the sealed holdout or an active embargo |
| E010 | Duplicate `spec_hash` (returns existing experiment ID) |
| E011 | Overnight short outside F&O lot universe; intraday short without squareoff |
| E012 | Sweep exceeds family trial or budget cap |
| E013 | Universe contamination (fund schemes, MF entries in `companies`) not excluded |
| E014 | Benchmark lacks history for the window (MID150BEES before 2022-05) |
| E015 | Dataset version stale (>26h) or failing integrity gates |

## 7. Evaluation methodology

### 7.1 Data tiers, holdout and embargo [DESIGN]

- **Tier H** = every feature has ≥3 years of point-in-time history (§1.1).
  - In-sample: data before **2026-09-12 − 365 days = 2025-09-12**.
  - Sealed holdout `sealed_h_v1`: 2025-09-12 onward, growing as snapshots advance.
  - Each hypothesis version gets **one** holdout access. Each family gets ≤10 accesses per quarter.
  - All accesses logged in `holdout_access_log`. Changing the seal is human-only and ledger-recorded.
- **Tier F** = any feature with less history. No historical holdout exists; validation is the
  forward SHADOW record under a frozen spec. Historical LLM re-analysis of old filings is
  **forbidden** as validation evidence, because today's model carries knowledge from after the
  event.
- **E-series embargo:**
  - Families whose features derive from `expectations`, `earnings_surprises` or `guidance_*`
    cannot use events announced **2026-10-13 → 2026-11-14** (Q2 season) in any test or report.
  - This holds until the founder records `embargo_lifted`, after Paper B's pre-registered primary
    endpoint is computed.
  - Enforced by compiler E009 and the report renderer.

### 7.2 Returns [DESIGN]

- **Calendar:** trading dates present in `price_points`.
- **Clocks:**
  - `announcement_date` = IST date of `announced_at`. Mislabelled rows shift late, which is
    conservative; flagged `bias.ts_late`.
  - `exchange_first_seen` = min(news `timestamp_utc`) across NSE/BSE for the symbol-event (2024+
    BSE, 2026+ NSE).
- **Entry and exit prices** from `price_points` open/close. Minute-level rules only use
  `paper_signal_marks` (forward).
- **Corporate actions v1:** exclude any event whose [entry−1, exit+1] window contains a
  split/bonus/rights ex_date. Also flag unexplained |overnight move| > 40% with no CA row as a
  suspected unrecorded action. Split-adjustment factors come in v2.
- **Abnormal return** = stock return − benchmark ETF return over the identical window. "Liquidity
  matched" means NIFTYBEES for the top liquidity tercile and JUNIORBEES otherwise (MID150BEES once
  available). Market cap is not used because `companies.mcap_cr` is not point-in-time.

### 7.3 Statistics [DESIGN]

- **Unit of inference:** calendar-week entry cohorts (mean net abnormal return per cohort).
  Overlapping holding windows are handled by Newey-West on the weekly cohort series with lag =
  ceil(holding_days / 5). Year-by-year cohort means are always reported.
- **CI:** stationary block bootstrap over weekly cohorts, 10,000 resamples, **seed 42** (visible
  constant `FACTORY_SEED = 42`).
- **Trials:** every sweep point of every experiment in a family is recorded. The sweep point is
  selected by pre-declared rule (the primary horizon), never by best outcome. The other points are
  reported with Benjamini–Hochberg-adjusted p-values.
- **Deflated Sharpe:** probability that the cohort-series Sharpe beats the expected maximum Sharpe
  of N independent trials, using family trial count, skew and kurtosis. Gate: ≥ 0.95.
- **Online FDR:** one global LORD++ stream (α = 0.05, initial wealth α/2). Each hypothesis version
  spends wealth **once**, at gate A, on its primary p-value; wealth is earned back on rejections.
  Wealth exhausted → gate A refuses all promotions until the founder reviews. Ledger:
  `alpha_wealth_ledger`.
- **Placebo:** 1,000 pseudo-event draws on the same symbols' non-event dates (seed 42). The real
  effect must exceed the 97.5th percentile.
- **Sign-flip permutation** of cohort means (10,000, seed 42) is used as a robustness p-value, not
  the gate p-value.
- **Minimums:** Tier H ≥300 events and ≥12 cohorts pre-holdout; Tier F ≥100 events for
  SIMULATED; SHADOW_VERIFIED ≥100 forward events.

### 7.4 Costs, liquidity and tradability [DESIGN; rates are ASSUMPTION until verified]

**`india_cash_v1`, per side:**
- STT: delivery 0.1% on buy and sell; intraday 0.025% on sell.
- Stamp: 0.015% on delivery buys, 0.003% on intraday buys.
- NSE transaction charge 0.00297%; SEBI fee ₹10/crore.
- Brokerage: min(₹20, 0.03%) per order (discount-broker assumption).
- GST 18% on brokerage + exchange + SEBI charges.

**`india_fno_v1`:** futures STT 0.05% on sell, plus exchange/SEBI/GST (rates to verify). Lot
sizes come from the scrip master.

**`adv_bucket_v1` slippage per side, by 20-session median traded value:**

| Median traded value (₹ Cr) | Slippage |
|---|---|
| ≥ 50 | 5 bps |
| 10–50 | 15 bps |
| 2–10 | 40 bps |
| 1–2 | 80 bps |
| < 1 | untradeable |

These are recalibrated monthly from paper `provenance.slippage_bps` and `exit_slippage_bps`.

**Tradability:**
- Participation ≤2% of 20-session median traded value per trade.
- Exclude fund schemes and suspended names.
- Exclude GSM stage ≥ II and ASM stage ≥ II where `market_state` covers the date.
- Exclude days where OHLC shows a locked price band (high = low with volume).
- Overnight shorts only in the F&O lot universe.

### 7.5 Adversarial battery `default_v1` [DESIGN]

| Test ID | What | Critical? | Kill condition |
|---|---|:--:|---|
| latency_plus1 | entry one session later (or +30 min for F) | ✓ | net effect sign flips or < 50% |
| cost_x2 | costs doubled | ✓ | net ≤ 0 |
| drop_top5 | remove 5 symbols contributing most | ✓ | net ≤ 0 or t < 2 |
| year_loo | leave-one-year-out | ✓ | any single year removal flips sign |
| placebo_dates | pseudo-events (§7.3) | ✓ | real ≤ 97.5th percentile |
| ca_exclusion | stricter CA window ±5 sessions | ✓ | net effect < 50% |
| clock_swap | `exchange_first_seen` vs `announcement_date` where both exist | ✓ | sign flip |
| season_split | results season vs off-season | | report only; strategy tagged |
| sector_loo | leave-one-sector-out (BSE sector map where covered) | | report |
| liquidity_tiers | effect by tercile | | report; capacity estimate |
| param_neighbourhood | ±1 grid step on each swept parameter | ✓ | median neighbour < 50% of point estimate |
| benchmark_swap | NIFTYBEES ↔ JUNIORBEES ↔ universe_median | | report |
| simpler_explanation | same test with a strictly smaller feature set (e.g. drop revenue from SUE) | | if ≥80% of effect remains → simpler child created, parent RETIRED on survival |
| survivorship_stress | restrict to symbols listed ≥3 years before event, with price history continuing to snapshot | | report + haircut note |
| market_state_excl | ASM/GSM exclusion (F coverage only) | | report |

The Adversary agent chooses **additional** catalogue tests with parameters and writes a kill memo.
The memo is advisory. A **veto is upheld** only when a critical test fails; that check is
deterministic.

### 7.6 Regime module (deterministic) [DESIGN]

- NIFTYBEES 20/60-day return sign and realized-volatility tercile.
- Breadth: % of liquid universe above 50-day moving average, from `price_points`.
- Results-season flag: weekly `earnings_surprises` count > 60th percentile of trailing 2 years.
- No VIX or index series exists [VERIFIED] — noted as a limitation.

## 8. Promotion gates [DESIGN — frozen at sign-off; changes are human-only and recorded]

| Transition | Gate (all must hold) |
|---|---|
| IDEA → SIMULATED | Compiles; integrity gates pass on the dataset_version; unique spec_hash; minimum events and cohorts |
| SIMULATED → ROBUST (**gate A**, Tier H) | Pre-holdout net abnormal cohort t ≥ **3.0**; positive in ≥60% of years; LORD++ rejects at current wealth; DSR ≥ 0.95; all critical battery tests pass; no upheld veto |
| ROBUST → OUT_OF_SAMPLE (**gate B**) | Single holdout access; same sign; OOS net effect ≥ 50% of in-sample; OOS t ≥ 2.0 (or 95% CI excludes 0); result_hash reproduced on a fresh dataset_version |
| SIMULATED → SHADOW (Tier F) | ≥100 historical events; critical latency and cost tests pass; Adversary review completed; frozen strategy_version with expectation envelope |
| SHADOW → SHADOW_VERIFIED | ≥100 forward events and ≥40 trading days; net expectancy > 0 at t ≥ 2.5; LORD++ rejects; envelope classification ≠ research_error |
| OUT_OF_SAMPLE or SHADOW_VERIFIED → PAPER | Tradable (§7.4); ≤6 concurrent paper strategies, ≤2 per family; notional caps from pre-approved envelope (₹1L/trade, ≤10 open, ≤₹20L gross per strategy) |
| PAPER → PAPER_VERIFIED | ≥100 events and ≥40 trading days; net > 0 at t ≥ 2; realized slippage ≤ 1.5× model; last divergence class ∈ {within_tolerance, regime_change with tag} |
| PAPER_VERIFIED → PRODUCTION_CANDIDATE | Automatic; report shows **ACTION REQUIRED** |

**Kill / pause rules:**
- 3 consecutive failed children in a family with no new mechanism → family PAUSED. Revisit needs
  a Chief memo citing a feature version or data window **not used** by those children; the gate
  checks the references deterministically.
- Any leakage or integrity finding → QUARANTINED plus all dependents (lineage and shared
  dataset/feature version).
- Family spend > 2× cap → halted.
- Paper classification `research_error` or `implementation_bug` → strategy paused.

## 9. Paper-trading feedback [DESIGN]

**Expectation envelope**, stored on every `strategy_version`, from backtest or shadow:
- mean net return per trade with 95% CI; hit rate with CI
- slippage bps per side
- decay curve (t0/5/15/30/60 min, 1/5/20 days)
- holding period distribution
- per-regime means

**Realized** = `paper_trades` (+ provenance) and `paper_signal_marks` joined on
`paper_links.paper_strategy_name`.

**Divergence classifier** (deterministic, first match wins; weekly, plus daily for new strategies):
1. **implementation_bug** — replaying the frozen spec on the paper trade log disagrees with what
   the engine did: different exit rule, entry outside rule, stale-price fill.
2. **data_mismatch** — decision-time features ≠ snapshot-recomputed features beyond tolerance
   (magnitude, materiality p, clock).
3. **execution_error** — realized slippage > 1.5× model, or fill-time deviation > rule tolerance.
4. **model_drift** — upstream artifact or pipeline version changed (provenance `model_artifact`,
   stage model IDs) coincident with the break.
5. **regime_change** — divergence concentrated in a regime cell with z > 2 vs the envelope's regime
   split.
6. **signal_decay** — rolling 30-trade mean slope t < −2 while execution checks pass.
7. **research_error** — realized mean outside the envelope CI with n ≥ 60 and none of the above.
8. **within_tolerance**.

Every class except 8 opens a `research_question` with evidence refs and pauses the strategy if the
class is 1, 2 or 7.

**First case (runs in week 3):** `orderwin_long`.
- The paper engine applied trailing stops; the D0T spec had a 5-day hold with no stop → class 1
  expected.
- Follow-up recomputes **all 134** entries under both exit rules from `price_points`. It must not
  compare stopped against unstopped survivors, because time exits are conditional on not having
  been stopped.

## 10. Research memory [DESIGN]

- **Records:**
  - `failures` (FAIL-xxxxx): category, reason, lesson, recommendation, evidence refs, provenance,
    1024-dim embedding.
  - Successes = hypotheses with gate decisions ≥ ROBUST or SHADOW_VERIFIED.
  - Family summaries regenerated nightly and deterministically: counts, best and worst metrics,
    kill reasons, open questions.
- **Retrieval** (`search_memory` tool): relational filters (family, category, status, date) +
  pgvector cosine on embeddings (`llm/embeddings.embed_texts`) + lineage neighbours. Top-k ≤ 10,
  each item with provenance.
- **Import at Day 4–7** (`provenance=imported_unreverified`, visible to agents with that label):
  - `ledger.jsonl` (138 rows)
  - STAGE0–4B findings
  - the 2026-07-04 signal audit (LLM direction ≈ coin flip)
  - PEAD v2 validation
  - pre-open veto inversion for shorts
  - Tier-B rubric replay FAIL
  - C0 commentary replay FAIL
  - orderwin_long paper result
- **Contamination guards:** quarantined experiments' lessons are excluded by default. The
  embedding model version is stored and re-embedded on change. Imported items can never count as
  evidence at any gate.

## 11. Research frontier [DESIGN]

```
priority = EIG × P_success × V_econ × R_relevance × T_time × Q_coverage
           ─────────────────────────────────────────────────────────────
                     C_expected × (1 + D_dup)
```

| Term | Definition (v1) |
|---|---|
| EIG | 1 − SE_after/SE_before, where SE_after uses expected new events n_new (history + forward accrual in the next 30 days) |
| P_success | Beta posterior mean of the family's next-gate pass rate; prior Beta(1, 9) |
| V_econ | point estimate of net bps × capacity (2% participation × median traded value) × tradability ∈ {0, 1}, min-max normalized across open families |
| R_relevance | 1.0 divergence or founder question; 0.7 frontier-generated; 0.3 generalization of a paused family |
| T_time | 1.5 if needed data arrives within 30 days (e.g. results season for results families), else 1.0 |
| Q_coverage | 1.25 for a horizon or tier with <15% of the last 30 days' trials, else 1.0 |
| C_expected | expected $ (LLM + compute) + queue latency (hours × $0.10) |
| D_dup | max cosine similarity of the claim + spec embedding to hypotheses in the last 90 days (0–1) |

Chief Scientist allocations are bounded:
- ±50% change per family per day
- total ≤ daily cap
- kill and revisit rules per §8

## 12. 24-hour research report [DESIGN]

**When:** `report_daily` starts 07:00 IST in its **own process** and reads only the ledger.
Target delivery 07:30 IST.

**Pipeline:**
1. Build **ReportFacts** deterministically (§23.10).
2. Render every number and table directly from the facts.
3. The Reporter writes only the prose parts: Executive Summary bullets, the "why it matters"
   lines, and the frontier rationale.
4. **Claim reconciliation**, a sentence splitter in the `outreach/checks.py` pattern. Every
   number must equal a fact value within display rounding. Every H/EXP/FAIL/DS/commit ID must
   exist. Opinion words that go beyond what the facts support ("proves", "robust" before gate A)
   are rejected. Failing sentences are dropped and counted.
5. Structural validation: all sections present; "research activity low" appears when completed
   experiments < 3.
6. **ACTION REQUIRED** is computed deterministically. It fires on any of:
   - a PRODUCTION_CANDIDATE
   - a pending approval
   - an open integrity gate
   - budget ≥ 80% of the monthly cap
   - α-wealth exhausted
   - yesterday's delivery failed
   - stale snapshot
   - E1 or E7 failing
   - a quarantined result
7. Store in `daily_research_reports` (idempotent on `report_date`).
8. POST to FLOW 1 (retry 3× with backoff); success = HTTP 200 and `ok:true`.
9. `PutMetricData Factory/ReportDelivered=1`.

**Failure behaviour:**
- Reporter agent fails → the deterministic report still ships with "narrative unavailable (agent
  failure)".
- Flow fails → SNS publishes a plain-text summary (top sections, ≤ SNS size limit).
- **Dead-man switch:** CloudWatch alarm on `ReportDelivered`, 1-day period,
  `TreatMissingData=breaching` → SNS email to the founder. This is independent of the EC2 host.
  Period alignment is validated during build.

**Subject:** `[Quant Research] 24h Research Report — YYYY-MM-DD`.

**Sections:** v1 §23–24 plus **Agent Quality** (E0/E3 trends, judge κ, champion/challenger) and
**Data Integrity** (gate status, staleness, embargo state). Every finding lists hypothesis ID,
experiment ID, dataset_version, code commit, feature versions, strategy version and trace ID.

## 13. Observability [DESIGN]

- **Tracing:** one trace per `research_cycle`.
  - The DBOS workflow is the root span. Children: compile, backtest (with `cpu_seconds`),
    stats, battery test, gate decision.
  - The AgentCore invoke carries a W3C `traceparent`, so the Runtime session spans, Strands model
    and tool spans, and Gateway→Lambda spans join the same trace.
  - `trace_id` is stored on `experiment_runs`, `agent_runs`, `gate_decisions` and
    `daily_research_reports`.
- **Sinks:**
  - AgentCore Observability → CloudWatch Transaction Search (agent spans, GenAI dashboard).
  - Orchestrator and workers → ADOT/OTel exporter → CloudWatch.
  - Research metrics → Postgres `factory` schema → Grafana (existing container, Postgres
    datasource).
- **System metrics:** workflow step success/retry/latency; queue depth and wait; AgentCore
  session count, errors, duration; Bedrock throttles and 5xx per pool; tokens and $ by
  agent/version/family; Opus 4.6 and 4.5 TPD used; snapshot age and integrity status; backtest
  CPU-seconds; host CPU credit balance and steal; Postgres connections; report delivery.
- **Research metrics:** funnel by status per day; conversion rates (SIMULATED→ROBUST→OOS→PAPER…);
  compile-reject rate by error code; duplicate rate; repeated-failure rate; α-wealth; holdout
  accesses left; per-family p-value histograms (a uniformity check flags p-hacking); Adversary
  kill rate and upheld-veto rate; divergence classes; $ per ROBUST survivor; research velocity
  (7-day moving average of completed cycles).
- **SLOs:**

| SLO | Target |
|---|---|
| Report delivered | 100% by 07:30 IST, measured off-host |
| Workflow step success | ≥ 99% |
| Snapshot age | < 26h, otherwise fail closed |
| E6 reproducibility | 100% |
| Agent-step p95 queue wait | < 2h |

- **Alerts:** existing `get_monitor()` webhook for in-band alerts (budget breach, integrity gate,
  E7 failure, reproducibility mismatch, α-wealth exhausted, throttle storm > 30 min). SNS email for
  off-host alerts (report dead-man, orchestrator heartbeat metric missing > 2h in market days).
- **Logs:** JSON with `trace_id`, `workflow_id`, `hypothesis_id`, `experiment_id`,
  `agent_version`. No secrets, no prompt bodies in application logs; prompts are in spans with
  90-day retention.

## 14. Agent evaluation program [DESIGN]

| Layer | Scope | Mechanism | Cadence | Pass bar (initial) |
|---|---|---|---|---|
| **E0 Contract** | every agent run | schema validity; DSL compile rate; referenced features exist; cited IDs resolve; usage present; budget respected | inline, 100% | logged. Compile-fail > 40% over 20 runs → challenger flagged |
| **E1 Offline golden sets** | per agent version | see E1 datasets below | every agent-version change + weekly | see E1 datasets below |
| **E2 System simulations** | whole factory | **Null world**: returns replaced by date-block-permuted returns (weekly blocks, seed 42), full loop in a sandbox ledger namespace; any OOS/SHADOW_VERIFIED promotion = false discovery. **Planted alpha**: +δ bps drift on a random 10% event subset keyed to a synthetic registered feature | weekly (Saturday) | factory FDR ≤ 5% across accumulated runs; planted δ = 100 bps @10d reaches ROBUST within the sim budget ≥ 80% of runs |
| **E3 Online** | sampled live sessions | AgentCore online evaluation: built-in evaluators (tool selection, goal success, faithfulness/hallucination class) + custom LLM judge (research-quality rubric: falsifiability, mechanism specificity, novelty vs memory) + code-based Lambda evaluators (ID resolution, numeric grounding) | Adversary and Reporter 100%, Scientist 25%, Chief 100% | trend alert: −15% vs 14-day median |
| **E4 Judge calibration** | each custom judge | founder labels 30–50 items per judge per month; Cohen's κ | monthly | κ ≥ 0.6, otherwise judge is advisory-only and its scores are hidden from gates |
| **E5 Regression** | challenger vs champion | E1 non-inferiority (one-sided 95% bound within 5 pts) + null-world smoke (1 run) + 10% shadow for ≥3 days | per change | pass → eligible for founder-approved champion PR |
| **E6 Reproducibility** | experiment runs | re-execute 5% of completed runs from saved spec + dataset_version + commit | nightly | 100% `result_hash` match; mismatch = P1 alert and gates frozen |
| **E7 Evaluator correctness** | stats, costs, compiler | known-answer tests: cohort t and Newey-West on synthetic AR(1) series, bootstrap CI coverage (seeded), BH/LORD++ against published examples, DSR formula, cost model worked examples, compiler error-code fixtures | CI on every PR + nightly | all pass; failure freezes gates |

**E1 datasets:**

| Agent | Test set | Pass bar |
|---|---|---|
| Scientist | (a) *rediscovery*: memory truncated to date D, does it propose findings recorded after D; (b) *dead-end avoidance*: re-proposal rate of FAIL records without new mechanism; (c) duplicate rate vs ledger; (d) falsifiability rubric (custom judge) | dead-end re-proposal ≤ 5%; duplicates ≤ 10%; rubric ≥ 3.5/5 |
| Adversary | ***flaw zoo***: 40 planted-defect experiments + 20 clean controls. Defect classes: look-ahead via revised fundamentals; +5.5h clock; same-day pre-filing close entry; survivor-only universe; cost omission; single-name concentration; one-regime dependence (2020 only); overlapping-window t inflation; post-hoc arm selection; stop-rule selection bias; unadjusted split; placebo-level effect | recall ≥ 90% on critical classes; false alarms on clean controls ≤ 10% |
| Chief Scientist | retrospective allocation replay against ex-post information gain; policy compliance | 100% compliance; beats uniform allocation on replay |
| Reporter | zero-activity fixture; seeded-contradiction fixture; claim reconciliation | 0 unreconciled claims shipped; "activity low" stated 100% |

Offline sets live in `bharat_terminal/factory/evals/datasets/` (versioned). Agent-facing sets run
through the AgentCore dataset runner (public preview), with a local pytest runner as fallback. The
deterministic portions (battery vs flaw zoo, compiler fixtures) run in CI.

**Eval budget:** E3 ≈ 30 evaluated traces/day × 2 built-in evaluators × ~12k input tokens ≈
$2/day. Measure in week 3 and fit within §18.

## 15. Security and permissions [DESIGN]

- **Database roles** (created by migration 043 as NOLOGIN group roles; login users and passwords
  are provisioned by an approved ops step from the environment, never in code):
  - `factory_reader`: SELECT on snapshot source tables.
  - `factory_writer`: INSERT/SELECT on `factory.*` **except** status and gate tables.
  - `factory_gate`: only role that can INSERT `gate_decisions` and UPDATE `hypothesis_status`.
  - `papertrade` reads `factory.strategy_versions WHERE status IN ('approved_for_paper','paper','shadow')`.
- **IAM** (each a separate approval):
  - `factory-orchestrator` (EC2 instance-profile addition): `bedrock-agentcore:InvokeAgentRuntime`
    on 4 runtime ARNs, `cloudwatch:PutMetricData` (namespace `Factory`), logs, `sns:Publish` on
    one topic.
  - Per-agent runtime roles: `bedrock:InvokeModel*` on Opus 4.6/4.5 inference-profile ARNs **only**,
    Gateway invoke, logs/X-Ray.
  - `factory-tools-lambda`: VPC ENI only; no data-plane AWS permissions.
- **Network:** the tools API binds to the EC2 private IP. Its security group admits only the
  Lambda security group on one port. No public exposure, no NAT, no VPC endpoints. Reachability is
  validated at the spike.
- **Secrets:** existing `.env` for EC2 services; AWS Secrets Manager for the Lambda→tools-API
  bearer token; none baked into images.
- **Production boundary:**
  - Factory status can reach PRODUCTION_CANDIDATE only.
  - No order-placement code exists [VERIFIED] and none will be added.
  - Paper caps are enforced in papertrade code.
  - CODEOWNERS on `bharat_terminal/{papertrade,analysis,ingestion,kb,api}/**`.
- **Git:** agents (a later Coder agent) open PRs on branches `factory/agent/<agent_run_id>`; the
  founder merges. The factory CI job must pass.

## 16. Git and CI [DESIGN]

`.github/workflows/factory.yml`, on PRs touching `bharat_terminal/factory/**`,
`tests/factory/**`, `migrations/versions/04[3-9]*` or `infra/agentcore/**`:
- ruff scoped to those paths (repo-wide ruff is red and not our scope)
- pytest `tests/factory` in `python:3.12-slim` with `requirements/factory.txt`
- E7 known-answer tests
- DSL fixture compile
- JSON-Schema export diff check (§23)
- Cedar deny tests (policy files linted)

AgentCore dataset evals need AWS credentials, so they run on EC2 via `scripts/factory_eval.sh`.
GitHub OIDC is a later approval. On EC2 the existing `scripts/test.sh` pattern is used (throwaway
container, unreachable DB hosts).

## 17. Model routing [DESIGN]

| Task class | Model | Why |
|---|---|---|
| Hypothesis generation, mutation, report narrative | Opus 4.5 (5 RPM, 13.5M TPD) | larger token allowance for tool-heavy loops |
| Adversarial analysis, allocation, contradiction | Opus 4.6 (5 RPM, 2.59M TPD) | strongest available reasoning; low volume |
| Structured extraction / classification | **existing production pipeline** (no factory LLM) | avoids consuming production pools |
| Embeddings | existing Bedrock Cohere v4 @1024 (`embed_texts`) | reuse; low volume |
| Coding (later: feature primitives, PRs) | Opus 4.6 via Code Interpreter or Claude Agent SDK | Nice-to-have |

**Prompt caching:** stable system prompts and tool lists are placed first with cache breakpoints.
Bedrock caching support for these models is to be confirmed at the spike.

**Provider outage:** agent steps wait durably (DBOS). Deterministic steps continue. The report
states the degradation.

## 18. Budget and cost model

**Assumptions [ASSUMPTION]:**
- 90 days; new spend only.
- Opus-class at Anthropic list price $5/$25 per M input/output tokens. The Bedrock page did not
  render these rows; verify on the AWS bill in week 1.
- Scientist session ≈ 6 turns × 10k input / 1.2k output ≈ $0.48; Adversary ≈ 8 × 14k / 1.5k ≈
  $0.86; Chief ≈ $0.90/day; Reporter ≈ $0.30/day.
- AgentCore compute ≈ $0.002/session (mostly I/O wait, which is free).
- CloudWatch spans ≈ 60 KB per call.

| Line (90 days, USD) | Lean | **Recommended** | Aggressive (~60 days) |
|---|---:|---:|---:|
| Agent LLM (Opus pools) | 450 | **1,000** | 1,600 |
| AgentCore Evaluations (online + dataset) | 60 | **200** | 400 |
| Simulation-world LLM runs (E2) | 80 | **250** | 400 |
| AgentCore Runtime / Gateway / Policy + Lambda | 30 | **50** | 80 |
| CloudWatch (spans, logs, alarms, dashboards) | 40 | **100** | 150 |
| Burst compute (off-host spot for E2, placebo sweeps) | 0 | **150** | 250 |
| Storage / backups (snapshots, ledger dumps) | 30 | **80** | 60 |
| Security (Secrets Manager, access analysis) | 20 | **40** | 60 |
| Reserve | — | **1,130** | 0 |
| **Total** | **710** | **3,000** | **3,000** |

Throughput implied by the Recommended budget: ~10 Scientist sessions, ~4 Adversary reviews, 1
Chief memo and 1 report per day.

**Controls (deterministic, in the orchestrator):**
- Daily LLM + eval: soft $15, hard $25.
- Monthly hard $1,000 for agent LLM.
- Per-family cap set by the Chief within bounds.
- Runaway detector: session cost > 3× its agent's 14-day p95 → session cancelled + alert.
- Opus 4.6 token guard: stop new Opus 4.6 tasks at 85% of the daily token quota.
- `budget_ledger` records *measured* (usage returned by agents) and *estimated* (compute, CW)
  entries; reconciled with the AWS bill monthly.

## 19. Technology choices

| Need | Choice | Rejected (why) | Status |
|---|---|---|---|
| Durable workflows | **DBOS Transact** on existing Postgres | Temporal (server + UI + per-shard pools on a host with connection-leak history); Airflow/Prefect (schedulers, not durable execution); hand-rolled loops (current pain) | README verified; API details at spike |
| Agent runtime | **AgentCore Runtime** (microVM per session) | EC2 containers (weaker isolation, production host load); Anthropic Managed Agents (outside Bedrock; not chosen) | docs verified |
| Agent framework | **Strands Agents** | LangGraph (heavier); OpenAI Agents SDK (provider mismatch); Claude Agent SDK (kept for a later Coder agent) | Evaluations-supported frameworks verified |
| Tool boundary | **Gateway + Policy (Cedar) + Lambda target** | agents with DB credentials (no deterministic boundary) | docs verified; private reachability at spike |
| Agent evals | **AgentCore Evaluations + pytest harness** | third-party eval SaaS (extra vendor and cost) | docs verified; dataset eval = preview |
| Observability | **CloudWatch (AgentCore Observability) + Grafana (existing)** | fixing Prometheus (not needed); Datadog (cost) | — |
| Backtest compute | **DuckDB + pandas over parquet** | Spark/Ray (overkill at ~50k events) | pin versions at build |
| Ledger | **Postgres `factory` schema** | MLflow/W&B (duplicate the ledger) | — |
| Email | **Power Automate FLOW 1 + SNS dead-man** | SES (domain verification + sandbox exit — later option) | FLOW 1 contract verified from spec doc |
| Embeddings | **existing `embed_texts`** (Bedrock Cohere v4) | new provider | verified in code |

## 20. Failure-mode matrix

Probability: H / M / L. Impact: Crit / High / Med / Low.

| # | Failure | P | Impact | Detection | Mitigation | Component |
|---|---|:--:|:--:|---|---|---|
| 1 | Data leakage (future info in a feature) | M | Crit | compiler E002; per-feature-version PIT audit (`inputs_max_ts` < clock); placebo; null-world FDR | PIT materialization in snapshots; QUARANTINE + dependents | compiler, snapshotter, battery |
| 2 | Timestamp mismatch (IST stored as UTC, +5.5h) — **live** | H | High | nightly gap test vs `exchange_first_seen` | E003 blocks intraday rules on `announced_at`; conservative date clock; upstream fix is a human task | snapshotter integrity gates |
| 3 | Hallucinated feature definition | M | High | E001; E0 contract | features only from registry; new features only by PR | compiler, registry |
| 4 | Duplicate experiments | H | Med | `spec_hash` unique; D_dup embedding similarity | return existing ID; duplication penalty | ledger, frontier |
| 5 | Runaway compute | M | Med | per-run `cpu_seconds` budget; host CPU credit metric | cgroup CPU cap; cancel; off-market heavy jobs | backtest-worker |
| 6 | Infinite hypothesis loops | M | Med | lineage depth > 6; same-parent children > 8; daily cap | hard caps; family pause rule | frontier, orchestrator |
| 7 | Agent crash / malformed output | H | Low | E0; AgentCore session errors | ≤2 retries then step failed; deterministic path continues | orchestrator |
| 8 | Workflow crash (process killed mid-cycle) | M | Med | DBOS pending workflows; heartbeat metric | automatic recovery from last step; idempotent steps | DBOS |
| 9 | API outage (Bedrock / AgentCore) | M | Med | 5xx rate; alarm | durable wait with backoff; report notes degradation | orchestrator |
| 10 | Model pool throttling / token quota exhaustion | H | Med | 429 counter; token-quota guard | concurrency 1; backoff; IAM forbids production pools | orchestrator, IAM |
| 11 | Corrupted dataset / snapshot | L | High | sha256 manifest check on load; row-count delta > ±5% vs prior | refuse version; keep previous; alert | snapshotter |
| 12 | Stale data (project's characteristic failure) | H | Crit | source max-date per table vs last trading day; snapshot age | fail closed (no new experiments or enrolments); ACTION REQUIRED | snapshotter, gates, report |
| 13 | Backtest engine bug | M | Crit | E7; EXP-000001 reproduces incumbent PEAD; E6; planted-alpha recovery | gates frozen while E7/E6 failing | backtest, CI |
| 14 | Misleading result summary | M | High | claim reconciliation; E3 faithfulness; judge κ | numbers only from facts; unreconciled sentences dropped | report, Reporter |
| 15 | Overfitting | H | High | DSR; parameter neighbourhood; holdout gate B | pre-declared sweep selection; one holdout access | stats, gates |
| 16 | Multiple-testing inflation | H | High | LORD++ wealth; trial counts; p-value histograms | gate A refuses when wealth exhausted | stats, gates |
| 17 | Regime-specific result | M | High | year-LOO; season split; regime module | ≥60% positive years; regime tag on strategy | battery |
| 18 | Transaction-cost underestimation | M | High | cost_x2; paper realized vs model slippage | monthly recalibration; execution_error class | cost model, feedback |
| 19 | Liquidity failure | M | High | participation check; locked-band detection | ≤2% participation; untradeable bucket excluded | universe, execution |
| 20 | Execution mismatch (engine ≠ spec) — **seen: orderwin stop** | H | High | classifier rule 1 replays spec | pause; follow-up question | feedback engine |
| 21 | Paper/live divergence | M | High | envelope checks | classify; pause per class | feedback engine |
| 22 | Strategy decay | M | Med | rolling slope; CUSUM | pause; re-research question | feedback engine |
| 23 | Code regression | M | High | factory CI; E7; E6 nightly | `code_commit` pinned per run; gates frozen on failure | CI, orchestrator |
| 24 | Unauthorized production change | L | Crit | no tools; IAM least privilege; DB roles; Cedar deny tests; CloudTrail | PR-only; founder merge; CODEOWNERS | security model |
| 25 | Report-generation failure | M | High | status row; dead-man CloudWatch alarm | deterministic fallback report; SNS text fallback | report worker, CloudWatch |
| 26 | Database failure / connection exhaustion | L | Crit | Postgres health; connections > 80 | DBOS pools ≤5+5; statement timeouts; nightly `pg_dump` of `factory` | orchestrator, ops |
| 27 | Research-memory contamination | M | High | provenance flags; quarantine propagation | exclude quarantined; imported ≠ evidence | memory |
| 28 | EC2 freeze / host crash (2 incidents in Aug) | M | High | off-host alarms | DBOS resume; AgentCore unaffected; dead-man email | ops |
| 29 | Factory starves production (CPU credits, DB load) | M | High | CPU credits/steal; DB active queries | cgroups; snapshot COPY at 01:30 IST; backtests read parquet, not Postgres | snapshotter, worker |
| 30 | Survivorship bias (no PIT universe) — **live** | H | High | `bias.survivorship` flag on every Tier H result; stress test | forward SHADOW/PAPER mandatory before any candidate; PIT universe = Nice-to-have | compiler, gates |
| 31 | Unadjusted corporate actions | H | High | CA window check; unexplained >40% gap detector | exclusion v1; adjustment factors v2 | backtest |
| 32 | E-series pre-registration contamination | M | High | embargo table checks in compiler and renderer | E009; report omits embargoed windows | compiler, report |
| 33 | Circuit / GSM-locked names create fake reactions | M | Med | market_state (≥2026-07-30); OHLC locked-band detection | exclusion; fills rejected | universe, battery |
| 34 | Cedar / IAM misconfiguration | L | Crit | CI deny tests; Policy decision logs review weekly | default-deny; change needs approval | infra |
| 35 | LLM judge drift / miscalibration | M | Med | monthly κ; judge version pinning | advisory-only below κ 0.6 | evals |
| 36 | Angel One session expiry breaks paper marks | M | Med | existing `PAPERTRADE_LTP_DRY` alert; `data_mismatch` class | existing re-login; affected windows excluded from verification | papertrade (existing) |
| 37 | Email flow URL rotated / flow edited | L | High | non-200 or missing `ok:true`; dead-man | retries; SNS text fallback | report worker |
| 38 | Calendar error (holiday table covers 2026 only) | M | Med | backtest calendar from `price_points` dates; December check alert | data-derived calendar for history | backtest |

## 21. Repository structure [DESIGN]

```
bharat_terminal/factory/
  contracts.py            # pydantic models = source of truth for §23; JSON Schemas exported to contracts/schemas/
  dsl/                    # models.py (HypothesisSpec/ExperimentSpec), compiler.py (E001–E015), hashing.py
  registry/               # features.py (seed registry + coverage), versions
  snapshot/               # exporter.py (COPY → parquet), manifest.py (sha256), integrity.py (gates)
  backtest/               # engine.py (event study), calendar.py, returns.py, universe.py
  costs/                  # india_cash_v1.py, india_fno_v1.py, slippage.py
  stats/                  # cohort.py (NW t), bootstrap.py, fdr.py (BH, LORD++), dsr.py, placebo.py
  battery/                # default_v1.py (tests of §7.5)
  gates/                  # engine.py (transitions + kill rules), holdout.py, embargo.py
  frontier/               # priority.py, allocation.py
  feedback/               # envelope.py, classifier.py
  workflows/              # dbos app: snapshot, frontier, research_cycle, feedback, repro, null_world
  report/                 # facts.py, render.py, reconcile.py, deliver.py (FLOW 1 + CloudWatch + SNS)
  tools_api/              # FastAPI read tools + submit tools (called only via the Lambda proxy)
  agents/{scientist,adversary,chief,reporter}/   # Strands app (BedrockAgentCoreApp entrypoint), prompt.md, tools.json
  evals/{datasets,flaw_zoo,null_world,judges}/
infra/agentcore/          # CloudFormation: runtimes, gateway, policy (Cedar), lambda proxy, alarms, SNS
tests/factory/            # E7 known-answer, compiler fixtures, gate rules, reconciliation
migrations/versions/043_factory_ledger.py
requirements/factory.txt · docker/factory/Dockerfile · compose services under profiles: ["factory"]
.github/workflows/factory.yml · scripts/factory_eval.sh
```

## 22. Ledger schema (`factory` schema, migration 043) [DESIGN]

Migration 043 creates **25 tables**. All are append-only except `hypothesis_status` — one mutable
row per hypothesis, writable only by the gate role. Every row carries `created_at`; runs, decisions and
reports also carry `trace_id`.

| Table | Key columns |
|---|---|
| `research_families` | family_id PK, title, horizon, data_tier, status (active/paused/killed/halted), budget_cap_usd, killed_reason |
| `research_questions` | question_id PK (RQ-nnnnnn), family_id, text, source (frontier/divergence/adversary/founder/import), status, evidence_refs jsonb, closed_at |
| `hypotheses` | (hypothesis_id, version) PK, family_id, question_id, claim jsonb, spec_yaml, spec_json jsonb, horizon, data_tier, created_by (agent_run_id / founder / import) |
| `hypothesis_status` *(mutable; gate role only)* | hypothesis_id PK, version, status, last_decision_id, updated_at |
| `hypothesis_lineage` | child (id, ver), parent (id, ver), op, diff jsonb |
| `feature_versions` | feature_id, version, source_table, data_tier, pit_clocks, history_start, coverage (NULL = unmeasured, which blocks use as a control), definition_ref, code_commit, deprecated_at. No separate parent `features` table: the version IS the identity |
| `dataset_versions` | dataset_version PK (DS-YYYYMMDD-nn), manifest jsonb (table → rows, max_date, sha256), integrity jsonb, snapshot_path, sealed_from, embargoes jsonb |
| `experiments` | experiment_id PK (EXP-nnnnnn), hypothesis (id, ver), spec jsonb, **spec_hash UNIQUE**, sweep jsonb, trial_count |
| `experiment_runs` | run_id PK, experiment_id, sweep_point jsonb, dataset_version, feature_versions jsonb, code_commit, seed, status, cpu_seconds, result jsonb, **result_hash**, error, workflow_id, trace_id; UNIQUE (experiment_id, sweep_point_hash, dataset_version, code_commit) |
| `stat_tests` | test_id, run_id, kind (cohort_nw_t/bootstrap/dsr/bh/lordpp/permutation/placebo), inputs jsonb, output jsonb, p_value, passed |
| `robustness_tests` | test_id, experiment_id, battery_test, critical, requested_by (battery/adversary), run_id, verdict, details jsonb |
| `gate_decisions` *(gate role only)* | decision_id, hypothesis (id, ver), from_status, to_status, gate_version, rule_results jsonb, inputs jsonb, llm_recommendation jsonb, decided_by (gate_engine / founder:email) |
| `holdout_access_log` | access_id, hypothesis (id, ver), run_id, window jsonb, decision_id |
| `alpha_wealth_ledger` | entry_id, hypothesis (id, ver), p_value, alpha_tested, rejected, wealth_before, wealth_after |
| `strategy_versions` | strategy_id, version, hypothesis (id, ver), spec jsonb (frozen), params jsonb, caps jsonb, expectation_envelope jsonb, mode (shadow/paper), status, approved_by, code_commit. No separate parent `strategies` table |
| `paper_links` | strategy (id, ver), paper_strategy_name (= `paper_trades.strategy`), started_at, ended_at |
| `divergence_reports` | report_id, strategy (id, ver), window jsonb, metrics jsonb, classification, evidence jsonb, followup_question_id |
| `failures` | failure_id PK (FAIL-nnnnn), hypothesis (id, ver), experiment_id, category, reason, lesson, recommendation, evidence_refs jsonb, provenance, quarantined, embedding vector(1024), embedding_model |
| `frontier_snapshots` | snapshot_id, ranking jsonb (all terms), allocation jsonb, memo_agent_run_id |
| `agent_versions` | agent_version_id PK (`scientist@3`), agent, model_id, prompt_sha256, tools_sha256, framework_version, runtime_arn, endpoint, status (challenger/champion/retired), e1 jsonb |
| `agent_runs` | agent_run_id PK, agent_version_id, workflow_id, task jsonb, result jsonb, input/output/cache_read/cache_write tokens, cost_usd, turns, latency_ms, status, error, agentcore_session_id, trace_id |
| `eval_results` | eval_id, layer (E0–E7), agent_version_id, agent_run_id, evaluator, score, label, details jsonb, dataset_ref |
| `budget_ledger` | entry_id, date, category, family_id, amount_usd, source (measured/estimated), ref jsonb |
| `approvals` | approval_id, kind, payload jsonb, requested_at, decided_at, decided_by, decision |
| `daily_research_reports` | report_date PK, status, facts jsonb, html, text, claim_check jsonb, delivery jsonb, metric_pushed_at |

## 23. Interfaces (typed contracts) [DESIGN]

Source of truth: pydantic models in `factory/contracts.py`. CI exports JSON Schema
(`model_json_schema()`) and diffs it. Condensed, with field → type:

1. **Event Intelligence → Event Store** (`EventRecord`, parquet row in `events` within a dataset_version):
   `event_id: str` · `symbol: str` · `source: enum` · `event_type_v2: enum(22 buckets + other)` ·
   `subcategory: str|null` · `exchange_first_seen: datetime|null` · `announcement_date: date|null` ·
   `impact_id: str|null` · `hop: int|null` · `sentiment: enum|null` · `magnitude: int(1..5)|null` ·
   `materiality_p: float|null` · `analysis_ready: datetime|null` · `pipeline_version: str|null` ·
   `pit_ok: bool` · `integrity_flags: [str]`
2. **Scientist → Hypothesis Store** (`submit_hypotheses` input):
   `slot_id: str` · `proposals: [ {spec_yaml: str (≤8KB), parents: [{id, version, op}], rationale: str (≤1.5KB), memory_refs: [str]} ] (1..5)`
   → output `[ {proposal_index, status: enum(accepted|compile_error|duplicate|rejected_cap), hypothesis_id?, errors: [{code, path, message}]} ]`
3. **Hypothesis → Experiment Compiler** (`CompileResult`):
   `hypothesis_id, version` · `ok: bool` · `errors: [{code: E0xx, path, message}]` ·
   `experiment: {experiment_id, spec_hash, data_tier: H|F, trial_count, history: {start, end, events_pre_holdout, cohorts_pre_holdout}, bias_flags: [str]}`
4. **Experiment → Backtest Engine** (`RunRequest`):
   `run_id, experiment_id, spec_hash` · `sweep_point: {path: value}` · `dataset_version` ·
   `feature_versions: {feature: version}` · `code_commit: str(40)` · `seed: int (=42)` ·
   `window: {start, end, role: in_sample|holdout|forward}` · `cpu_seconds_budget: int`
5. **Backtest → Evaluation Engine** (`RunResult`):
   `run_id` · `status` · `n_events, n_cohorts, n_symbols` · `cohorts: [{week, n, mean_net_abn, mean_gross_abn}]` ·
   `by_year: [{year, mean, n}]` · `cost_breakdown_bps: {stt, stamp, exch, sebi, brokerage, gst, slippage}` ·
   `exclusions: {ca_window, untradeable, locked_band, embargo}` · `result_hash: sha256` · `cpu_seconds` · `trace_id`
6. **Evaluation → Promotion Engine** (`GateInput` → `GateDecision`):
   input `hypothesis_id, version, from_status, run_ids, stat_test_ids, robustness_test_ids, adversary_run_id?, wealth_snapshot, holdout_access_id?`;
   output `decision_id, to_status, gate_version, rule_results: [{rule, value, threshold, pass}], llm_recommendation?: {agent_run_id, verdict, memo_ref}`
7. **Paper Trading → Feedback Engine** (`PaperObservation`):
   `strategy_id, version, paper_trade_id` · `signal_at, entry_at, exit_at` · `side, instrument, qty, entry_price, exit_price, exit_reason` ·
   `slippage_bps_entry, slippage_bps_exit` · `marks: {t0,t5,t15,t30,t60}` · `provenance_digest: {model_artifact, event_type, magnitude, materiality_p}` · `regime_cell`
8. **Feedback → Research Frontier** (`DivergenceReport`):
   `report_id, strategy_id, version, window` · `expected: Envelope` · `realized: Envelope` ·
   `classification: enum(8)` · `evidence: [{kind, ref, value}]` · `action: enum(none|pause|followup)` · `followup: {question_text, family_id, relevance: 1.0}`
9. **Research Frontier → Hypothesis Generator** (`FrontierSlot`, AgentCore task payload):
   `task_id, agent: scientist, agent_version, workflow_id, traceparent` · `slot: {slot_id, family_id, question_id, allowed_ops: [op], max_proposals: int, usd_cap: float}` ·
   `context_refs: {memory: [FAIL/H ids], runs: [run ids], divergence: [report ids]}` · `budget: {max_turns, max_output_tokens}`
   → `AgentResult {task_id, status: ok|budget_exhausted|error, submitted: [ids], usage: {input_tokens, output_tokens, cache_read, cache_write, model_id}, turns, error?}`
10. **Research DB → Daily Report Generator** (`ReportFacts`):
    `report_date, window: {from, to}` · `scorecard: {hypotheses_tested, experiments_completed, rejected, promoted, entered_shadow, entered_paper, integrated, cpu_hours, llm_usd, eval_usd, budget_remaining_usd, runway_days}` ·
    `hypotheses: [{id, version, family, question, horizon, universe, experiments, primary: {effect_bps, t, n}, status, refs: {experiment_ids, dataset_version, code_commit, feature_versions, trace_id}}]` ·
    `failures: [{failure_id, hypothesis_id, category, reason, lesson, recommendation}]` · `survivors: [...]` · `integrations: [...]` ·
    `paper: [{strategy_id, version, expected, realized, deviation, class, status}]` · `frontier: {top: [...], abandoned: [...], allocation_rationale_ref}` ·
    `health: {workflow_failures, agent_failures, integrity, stale_sources, api_failures, backtest_failures, pending_approvals}` · `agent_quality: {...}` ·
    `action_required: bool` · `actions: [{kind, detail, approval_id?}]` · `activity_low: bool`

## 24. End-to-end walkthrough [DESIGN; all values ILLUSTRATIVE]

1. **Filing arrives.**
   - 18:42 IST: BSE posts ABCLTD's Q2 results (first-wins). The existing pipeline writes
     `news_items` (RESULT) and an `impact_reports` row. XBRL lands 23:10 IST.
   - `pead_signals` writes an `earnings_surprises` row: decile 10, `sue_composite` 2.9.
2. **Structured event.**
   - 01:30 IST `snapshot_nightly` materializes an `EventRecord`: `{event_id: "ev:ABCLTD:2026Q2:result", source: "earnings_surprises", event_type_v2: "results", exchange_first_seen: "2026-10-20T13:12:00Z", announcement_date: "2026-10-20", pit_ok: true, integrity_flags: []}`.
   - The integrity gate records `ts_ist_as_utc: open (99/503)` → `DS-20261021-01`.
3. **Hypothesis.**
   - `frontier_daily` gives slot S-88 (family `pead_liquidity`, RQ-000007 "does liquidity tier condition drift?").
   - Scientist retrieves FAIL-00012 ("EPS-only SUE nets ≈ 0 after costs") and submits H-000031
     (§6.1): mutation of H-000012, `min_median_traded_value_cr` 1→5.
4. **Compile.** `CompileResult{ok: true, data_tier: H, trial_count: 3, history: {start: 2020-01-06, end: 2025-09-11, events_pre_holdout: 1,840, cohorts_pre_holdout: 262}, bias_flags: ["survivorship", "ts_late"]}`.
   Note: `events_pre_holdout` excludes the E-series embargo by construction.
5. **Backtest.** Three `RunRequest`s (5/10/20 days); primary = 20 days, pre-declared.
   `RunResult{n_events: 1,742 (98 CA-window exclusions), mean_net_abn_bps: 142, n_cohorts: 255, result_hash: "9f2c…"}`.
6. **Statistics.** Cohort NW t = 3.6; positive years 5/6; DSR 0.97; LORD++ tests p = 0.0004 at α_t = 0.0021 → rejects; bootstrap CI [71, 214] bps.
7. **Adversarial.**
   - Battery: `latency_plus1` 118 bps ✓; `cost_x2` 96 ✓; `drop_top5` 131 ✓; `year_loo` min 104 ✓;
     `placebo_dates` 99.6th percentile ✓; `param_neighbourhood` (2 Cr, 10 Cr) 128/137 ✓.
   - Adversary requests `clock_swap` on 2024+ events (n = 412): 121 ✓. Its memo notes a 2021
     concentration; season split is recorded.
   - Gate A → **ROBUST** (`decision_id D-000451`).
8. **OOS.** One holdout access (2025-09-12 → 2026-09-11): 97 bps, t = 2.3, 68% of in-sample; hash
   reproduced on `DS-20261022-01` → **OUT_OF_SAMPLE**.
9. **Paper.**
   - `strategy_versions` row `pead_liq5@1`: frozen spec, envelope `{mean_net_bps: [60, 190], hit: [0.52, 0.61], slippage_bps: 15}`, caps ₹1L/trade.
   - Founder approval is required only once, for the generic executor.
   - `paper_links.paper_strategy_name = "dsl:pead_liq5@1"`.
10. **Paper evaluation.**
    - After 104 events and 47 trading days: realized 61 bps, t = 2.1, slippage 22 bps (1.47× model)
      → `within_tolerance`, low edge.
    - → **PAPER_VERIFIED** → **PRODUCTION_CANDIDATE** → report shows ACTION REQUIRED ("review
      pead_liq5@1; production blocked by Phase A").
11. **Memory update.** Success record plus lesson "liquidity ≥ ₹5 Cr retains ~70% of effect with
    lower slippage". The sibling H-000033 (≥ ₹10 Cr) was REJECTED on `drop_top5` → FAIL-00031.
12. **Next hypothesis.** Frontier raises `pead_liquidity` P_success; Chief allocates +30%. The
    `simpler_explanation` child H-000038 (revenue-SUE only) is queued.

## 25. 30-day implementation plan

**Days 1–3 — smallest durable loop (Must build)**
- This spec signed off. Approval batch #1: IAM spike roles, AgentCore spike resources, migration
  043 apply, `factory` compose profile.
- Migration 043 (ledger + NOLOGIN roles).
- `snapshot_nightly` v1: prices, earnings_surprises, corporate_actions, news RESULT first-seen,
  manifest, integrity gates (ts gap test, freshness, row deltas).
- DBOS orchestrator running one **no-LLM** cycle: EXP-000001 = incumbent composite-SUE PEAD from
  spec → backtest → stats → ledger. Must reproduce the incumbent D10 long leg within documented
  deltas vs the STAGE4A method.
- `report_daily` skeleton + FLOW 1 + CloudWatch dead-man live. "Research activity low" is a valid
  Day-3 report.
- AgentCore spike: hello Strands agent → Gateway → Policy → Lambda → tools API; trace visible;
  one on-demand evaluation; Cedar deny test observed.

**Days 4–7 — real hypotheses → real backtests**
- Full DSL + compiler E001–E015 with fixtures.
- Feature registry seed: `sue_composite@2.1`, `sue_eps`, `sue_revenue`, E1 surprise vs
  `expectations` (gbm/guidance), `event_type_v2`, impact `magnitude`/`sentiment`/`materiality_p`
  (F), deals (F), SHP deltas, `market_state` flags (F), liquidity tier.
- Backtest engine v1 + `india_cash_v1` + `adv_bucket_v1`.
- Memory import.
- Scientist v0 on AgentCore → first compiled proposals run end-to-end.

**Week 2 — rigor + lineage**
- Newey-West cohort t, bootstrap, DSR, BH, LORD++ wealth; holdout vault + embargo.
- Battery `default_v1` + Adversary agent.
- Lineage operators.
- E0, E1 (flaw zoo v1, rediscovery, dead-ends), E6, E7.
- Factory CI job.

**Week 3 — shadow and paper loop**
- Generic `dsl_strategy` executor (shadow + paper modes) in papertrade — founder approval + deploy.
- Envelope + divergence classifier; first case `orderwin_long`.
- Chief Scientist + frontier priority.
- E3 online evaluations + E4 calibration set.

**Week 4 — hardening**
- E2 null-world + planted-alpha.
- Budget controls + runaway detector + token guard.
- Grafana and CloudWatch dashboards + alarms.
- Report v1 complete (all sections).
- Failure drills: kill -9 orchestrator mid-cycle; simulated throttle storm; corrupted parquet;
  FLOW 1 down; stale source.

**Nice to have (after MVP):**
- Coder agent (PRs) via Code Interpreter / Claude Agent SDK
- point-in-time universe build (listings/delistings)
- CA adjustment factors
- S3 snapshot mirror
- us-east-1 quota-pool independence test
- prompt-caching tuning
- GitHub OIDC for evals in CI
- SES

**Do not build yet:**
- live orders or any broker order API
- tick/order-book research
- AgentCore Memory as system of record
- multi-region; Kubernetes
- fine-tuning
- a general-purpose code sandbox for agents
- customer-facing surfaces

## 26. MVP definition (v1 §33 mapping)

| # | Capability | Component | Acceptance test |
|---|---|---|---|
| 1 | Generate hypotheses | Scientist + `submit_hypotheses` | ≥5 compiled proposals/day for 5 consecutive days |
| 2 | Test hypotheses | compiler + backtest + stats | EXP-000001 reproduces; E7 green |
| 3 | Reject failures | gates + battery | flaw zoo recall ≥ 90%; FAIL records written with category + lesson |
| 4 | Refine survivors | lineage operators + frontier | ≥1 child per ROBUST parent within 48h |
| 5 | Maintain lineage | `hypothesis_lineage`, `gate_decisions` | any report finding traces to spec + dataset + commit + trace |
| 6 | Paper trade promising strategies | `dsl_strategy` executor | shadow signals logged with marks; paper fills with provenance |
| 7 | Feed paper back | feedback engine | `orderwin_long` classified; follow-up question created |
| 8 | Controlled Git integration | PR-only + factory CI | agent-authored PR blocked without CI + founder merge |
| 9 | Continue the loop | DBOS cron workflows | 7 days unattended with no stuck workflows; kill -9 recovery drill passes |
| 10 | Trustworthy 24h email | `report_daily` + dead-man | 7/7 delivered; claim check 100%; suppressed-metric drill triggers SNS email |

## 27. Open verification items (owned, dated)

| Item | How | When |
|---|---|---|
| Bedrock per-token prices for Opus 4.5/4.6 | AWS bill / Cost Explorer (needs `ce` permission or console) | week 1 |
| Trading-cost rates (NSE transaction, SEBI, stamp, F&O) | NSE / BSE circulars (NSE page timed out) | before gate freeze |
| Gateway → Lambda → EC2 private reachability; Cedar entity names; Bedrock prompt caching on Opus 4.5/4.6 | spike | Days 1–3 |
| DBOS pool settings and API names; Strands MCP client to Gateway | spike | Days 1–3 |
| CloudWatch alarm period alignment for daily dead-man | drill | Day 3 |
| Live FLOW 1 matches `POWER_AUTOMATE_SPEC.md` (Response after Send) | test POST to founder inbox | Day 3 |
| us-east-1 quota pool independence | controlled ≤3-call test off-market (approval) | Nice to have |
| `announced_at` +5.5h root cause (which writer) | code trace; founder decides fix | out of factory scope; tracked |

## 28. Decision rules (v1 §35)

1. **Top five bottlenecks:**
   - point-in-time data integrity (timestamps, survivorship, corporate actions)
   - multiple testing and holdout depletion
   - forward sample accrual: Tier F features have ≤4 months of history
   - model capacity (5 RPM per Opus pool)
   - founder approval latency for code and infra
2. **Deterministic:** snapshots and integrity, compile, backtests, statistics, costs and
   liquidity, battery, all transitions, budgets, kill rules, holdout, embargo, execution, report
   numbers, ACTION REQUIRED.
3. **Where LLM reasoning helps most:**
   - proposing mechanisms across heterogeneous evidence
   - combining findings across families
   - planning attacks and finding simpler explanations
   - weighing qualitative lessons in allocation
   - readable narrative
4. **Where LLMs would most likely create false discoveries:**
   - picking sweep winners post hoc
   - forking-path re-proposals until something passes
   - inventing feature semantics
   - narrative inflation
   - intraday timing confusion (gap vs capturable)
   - replaying historical filings with a model that already knows the outcome

   The countermeasures are in §6.3, §7.3, §8, §12 and §14.
5. **Minimum viable control plane:** one DBOS process on existing Postgres + `factory` schema +
   gate engine + report worker + one CloudWatch alarm.
6. **Persist forever:**
   - hypotheses and versions, lineage, specs, `spec_hash`
   - run results + `result_hash`, dataset manifests, code commits
   - gate decisions, holdout log, α-wealth ledger
   - failures and lessons
   - strategy versions + envelopes, paper links, divergence reports
   - agent versions (prompt hashes), agent-run usage, daily reports

   Parquet snapshots referenced by any hypothesis ≥ ROBUST are kept forever; others 30 days.
7. **Hard permission boundaries:**
   - production tables and services
   - papertrade code and config, `.env`
   - orders
   - git merge/push
   - IAM and AgentCore changes
   - gate thresholds, holdout seal, embargo lift, budget caps
   - the PRODUCTION transition
8. **Safe self-modification:** ledger data only (hypotheses, experiments, allocations within caps,
   paper parameters inside a pre-approved envelope). Prompts and code are PRs.
9. **Terminate a branch on:**
   - 3 failed children without a new mechanism
   - an integrity or leakage finding
   - OOS sign flip or < 25% of the in-sample effect
   - family budget exhausted without a survivor
   - family α-wealth exhausted
   - paper `research_error`
   - dominated by a simpler child
10. **Give a branch more compute on:**
    - a ROBUST or OUT_OF_SAMPLE survivor
    - high EIG with a large economic point estimate
    - paper divergence with capital at stake
    - imminent data (results season)
    - under-covered horizon or tier
    - planted-alpha evidence that the family's method has power
