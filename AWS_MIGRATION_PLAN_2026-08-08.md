# AWS Migration Plan — 2026-08-08

Status: **PLAN ONLY — nothing has been changed.** All findings below were
verified read-only on the live GCP VM on 2026-08-08.

Decisions taken by Aviral (2026-08-08):
- AWS credits are secured. Compute cost is not the driver; leaving GCP billing is.
- The ~4.5h outage today (11:18–15:47 IST) was a manual stop, not GCP
  suspension. So there is **no forced-suspension deadline** — we can sequence
  this properly rather than emergency-cutting over.
- LLM inference migrates to AWS (Claude), not just compute.

---

## 0. Verified current state

| Item | Value | How verified |
|---|---|---|
| Host | `n4-standard-2` (2 vCPU / 8 GB), `asia-south1-c` | GCE metadata server |
| Disk | 48 GB, **80% full** (9.7 GB free) | `df -h` |
| Public IP | `35.200.210.96` | GCE metadata |
| Containers | 14 running, `docker-compose.yml` + `bharat-terminal.service` | `docker ps`, `systemctl cat` |
| Postgres | 2,282 MB; `price_points` 3.58M rows, `financial_results` 119,572 | `psql` |
| Volumes | postgres 2.5 GB, redpanda 178 MB, prometheus 34 MB, grafana 1 MB | `docker system df -v` |
| Images/cache | 22.1 GB images + 12.2 GB build cache | `docker system df` |
| TLS | Cloudflare Origin CA, `*.taperead.trade` + `taperead.trade`, expires **2041-06-22** | `openssl x509` |
| Host cron | 3 jobs: market_state (08:30 M-F), company_type (10:00 Sun), guidance_ledger (20:15 M-F) | `crontab -l` |
| GCP coupling | **3 files only**: `llm/wrapper.py`, `llm/embeddings.py`, `scripts/probe_egress.py` | repo-wide grep |
| Vertex auth | VM metadata ADC — SA `deployment@bharat-terminal.iam.gserviceaccount.com`, scope `cloud-platform`. **No credential file mounted in compose.** | metadata server + compose grep |

### LLM spend (the actual GCP bill)

`llm_call_log` covers 2026-08-02 → 2026-08-08 only (table is new). Steady-state
days are ~$35/day → **~$1,050/month**. Compute is ~$80–100/month, i.e. **under
10% of GCP spend**.

| Stage | Model | Calls | USD |
|---|---|---|---|
| dossier | gemini-3.1-pro-preview | 538 | 43.25 |
| stage3_impact | gemini-2.5-flash | 5,266 | 36.87 |
| filing_summary | gemini-2.5-flash | 4,683 | 29.32 |
| stage2_extraction | gemini-2.5-flash | 5,191 | 29.20 |
| **dossier_grounding** | gemini-3.5-flash | 1,555 | 16.17 |
| stage5_signals | gemini-3.1-pro-preview | 1,068 | 2.12 |

100% Gemini. Zero Claude calls in the logged window.

---

## 1. The finding that picks the AWS surface

**Amazon Bedrock does not support the server-side web search tool.**
`dossier_grounding` (1,555 calls) is a Gemini `google_search` call that backs the
dossier agent's `web_search` function tool — the layer that took dossier quality
from 0.75 → 0.90. On Bedrock there is no equivalent, so a straight
"Gemini → Bedrock Claude" move silently drops the grounding layer.

Feature availability for what this project actually uses:

| Feature used here | Bedrock | Claude Platform on AWS |
|---|---|---|
| Messages API, tool use, streaming | ✅ | ✅ |
| Prompt caching | ✅ | ✅ |
| **Web search (server-side tool)** | **❌** | **✅** |
| Web fetch | ❌ | ✅ |
| Message Batches | ❌ | ✅ |
| Models API | ❌ | ✅ |
| Model ID form | `anthropic.claude-opus-5` | `claude-opus-5` (bare) |
| Auth | AWS creds / SigV4 | SigV4 + `ANTHROPIC_AWS_WORKSPACE_ID` |
| Billing | Native AWS service | **AWS Marketplace** |

**Recommendation: Claude Platform on AWS** (Anthropic-operated, same-day API
parity, AWS IAM + SigV4), *not* Bedrock — because it keeps the grounding layer
as a drop-in rather than a rebuild.

> ⚠️ **Open question that must be answered before committing.** Claude Platform
> on AWS bills through **AWS Marketplace**. AWS Activate credits commonly do
> **not** apply to Marketplace purchases, whereas Bedrock (a first-party AWS
> service) normally is credit-eligible. Since credits are the entire rationale
> for this migration, **verify with AWS which of the two your credits cover
> before writing any code.** This single answer decides Workstream B's shape:
>
> - **Credits cover Claude Platform on AWS** → drop-in, grounding preserved. Best case.
> - **Credits cover Bedrock only** → grounding layer must be rebuilt on an
>   external search API (Tavily/Brave/Serper) behind the existing `web_search`
>   function tool. Adds a vendor and ~1 week of work + re-validation.

Good news either way: `grounded_search()` is exposed to the agents as an
ordinary **function tool**, not a native provider tool. Whatever backs it can be
swapped inside `bharat_terminal/llm/wrapper.py:611` without touching the agent
loops in `research/dossier_agent.py` or `research/chat_engine.py`.

---

## 2. Target architecture

Lift-and-shift, deliberately boring:

- **EC2** in `ap-south-1` (Mumbai), `m7i.large` (2 vCPU / 8 GB) to match today.
  **100 GB gp3**, not 50 — the current box is 80% full with 12 GB of build cache.
- Same `docker-compose.yml`. **Postgres, Redis, and Redpanda stay as containers.**
  Do not swap in RDS/MSK during a cost-driven migration: it costs more than the
  VM, changes connection strings and code, and breaks "seamless".
- Elastic IP; security group restricted to Cloudflare origin ranges on 80/443.
  **SSH via SSM Session Manager, not a world-open port 22** — the current host's
  auth log is full of brute-force attempts (`Invalid user docker from
  92.118.39.71`, root attempts from several IPs).
- Cutover = one Cloudflare A-record change. The origin cert is valid to 2041 and
  is not IP-bound, so it moves as-is. Rollback is the same edit in reverse.

---

## 3. Workstreams

### A. Compute lift-and-shift

1. Launch EC2, install Docker + compose, clone repo, copy `.env` **out of band**
   (never via git, never echoed to a log).
2. Recreate the 3 host cron jobs and `bharat-terminal.service` (the unit is
   templated by `deploy.sh`; re-template for the new path/user).
3. Data move — DB is only 2.3 GB, so a plain dump/restore beats volume copying:
   - `pg_dump -Fc` on GCP → restore on AWS → verify row counts **before and
     after** for `price_points`, `financial_results`, `news_items`,
     `memory_items`, `investor_holdings`.
   - `redpanda_data` (178 MB) does **not** need migrating — topics are recreated
     by `redpanda-init`; consumer offsets restarting is acceptable.
   - `grafana_data` (1 MB) and `prometheus_data` (34 MB) — copy Grafana for the
     dashboards; Prometheus history is optional.
4. Rebuild images on the new host (do not copy 22 GB of images).

### B. LLM provider migration — the real work

`bharat_terminal/llm/wrapper.py` already routes by provider and already has an
Anthropic path, so this is an addition, not a rewrite.

- `_anthropic_client()` (line 192) is a clean two-branch factory (`vertex` /
  console). Add a third branch for the AWS client.
- `provider_for_model()` (line 104) and `to_vertex_claude_id()` (line 114) need
  AWS equivalents — bare IDs on Claude Platform on AWS, `anthropic.`-prefixed on
  Bedrock.
- **`_anthropic_kwargs()` (line 203) passes `temperature` when set — this
  returns a 400 on Claude Opus 5 / Sonnet 5.** Must be removed, along with
  `top_p`/`top_k` if any caller supplies them.
- Use `thinking={"type": "adaptive"}` + `output_config.effort`. Do **not** use
  `budget_tokens` (400s on current models).
- Bedrock-only gotcha if we end up there: forced `tool_choice` requires
  `thinking: {type: "disabled"}`.
- The Gemini-specific machinery (`thought_signature` replay at lines 139–152 /
  339–423, `_gemini_kwargs`, `_wrap_gemini_response`) becomes dead code — **leave
  it in place until cutover is proven**, then remove in a separate commit.

Proposed stage mapping (all env-driven today, so this is a `.env` change plus
the provider code):

| Stage | Today | Proposed |
|---|---|---|
| `EXTRACTION_MODEL` | gemini-2.5-flash | `claude-haiku-4-5` |
| `IMPACT_MODEL` | gemini-2.5-flash | `claude-sonnet-5` |
| filing_summary | gemini-2.5-flash | `claude-haiku-4-5` |
| `SIGNALS_MODEL` | gemini-3.1-pro-preview | `claude-sonnet-5` (signals show no net edge and are internal-only — don't spend Opus here) |
| `DOSSIER_MODEL` | gemini-3.1-pro-preview | `claude-opus-5` |
| `GROUNDING_MODEL` | gemini-3.5-flash | `claude-sonnet-5` + `web_search_20260209` |
| `RESEARCH_MODEL` | gemini-3.1-pro-preview | `claude-opus-5` |
| `MEMORY_DIGEST_MODEL` | gemini-3.5-flash | `claude-haiku-4-5` |

**Quality gate:** the project's characteristic failure mode is confident-but-wrong
output. Before cutover, re-run the dossier quality benchmark (RELIANCE, the
0.90 reference) and the nightly 19:05 IST classification-accuracy eval against
the 23.8% baseline. **A model swap is not done until those two numbers hold.**
Prompts were tuned against Gemini and will need re-tuning — Claude Opus 5
follows instructions more literally, is more verbose by default, and
self-verifies without being told (delete any "double-check your work"
scaffolding; it now causes over-verification).

### C. Embeddings — unavoidable index rebuild

`llm/embeddings.py` pins `text-embedding-004` at **768 dims to match the
`memory_items` HNSW cosine index**; all 12,530 rows are embedded. Neither
Anthropic nor Claude Platform on AWS offers an embedding model. Bedrock's
options are Titan Text Embeddings V2 (256/512/**1024**) and Cohere Embed v3
(1024) — **none emit 768**.

So this is a migration, not a config flip:

1. New migration: add `embedding_1024 vector(1024)` alongside the existing column.
2. Re-embed all 12,530 rows (short texts — cheap and fast).
3. Build the new HNSW index, verify retrieval quality on a held-out sample,
   then swap readers and drop the old column in a **later** migration.
4. Keep `EMBEDDING_DIM` enforcement — the existing loud-failure check at
   `embeddings.py` is exactly right and must be updated, not removed.

Alternative worth pricing: keep embeddings on Vertex (a few dollars/month, one
small GCP bill, zero index churn) and move only inference. Decide explicitly
rather than by default.

### D. Egress validation — the biggest unknown, and it gates everything

Exchange endpoints gate on caller IP reputation. NSE **already** 403s the cookie
warm-up from the current GCP IP while its JSON APIs still return 200. There is
no evidence AWS `ap-south-1` is treated the same.

`scripts/probe_egress.py` exists for exactly this. It gates on 6 critical
sources (NSE announcements, NSE board meetings, NSE `EQUITY_L.csv`, NSE
bhavcopy, BSE announcements, BSE scrip list, BSE `Corp_FinanceResult_ng`).

**Run it on a throwaway `ap-south-1` instance and require a GO before anything
else proceeds.** Baseline for comparison already exists (`--json` / `--compare`;
12 checks, 0 critical failures from GCP on 2026-08-04).

**Status 2026-08-08: egress probe PASSED.** Confirm it was run from an
`ap-south-1` host (check `host` / `egress IP` in the report — a pass from the
GCP VM does not count) and note any DEGRADED rows among the 6 critical sources.

Not covered by the probe, and needs separate checks:

- **Angel One** (`smartapi.angelone.in`) IP-binds the API key. The binding is
  self-service from the Angel One dashboard, so this is no longer a hard stop —
  but **changing it is itself a production-affecting action**, since standing
  policy routes *all* price data through this adapter. Sequencing depends on one
  question:
  - **Multi-IP allowlist supported** → add the AWS Elastic IP alongside
    `35.200.210.96`. Both hosts work through the parallel run; the quote path can
    be validated on AWS pre-cutover; drop the GCP entry afterwards. Preferred.
  - **Single-IP only** → repointing breaks the live GCP feed immediately. It
    becomes a **step inside the cutover write-freeze window**, not a prep task.
    Consequence: the Angel One path is the one component that cannot be fully
    rehearsed on AWS. Requires immediate post-flip verification and a known
    rollback (re-point to `35.200.210.96`).
  - Either case: **allocate the Elastic IP early** — an auto-assigned public IP
    changes on stop/start and would silently break the binding.
  - Verify Angel One tolerates **concurrent TOTP sessions from two IPs** during
    the parallel run. Some brokers invalidate the prior session on a new login,
    which would break GCP's feed even with both IPs allowlisted.
  - The new host's first boot pulls the **35 MB scrip master** over a slow CDN.
    Confirm the deployed image carries the 60s→600s timeout fix, or the commodity
    engine fails its first start and it will look like an egress problem.
- Cloudflare, Microsoft Approval webhook (onboarding), and the SMTP/email path.

### E. Cutover + rollback

1. Run both stacks in parallel with AWS pointed at a **restored copy** of the DB;
   let it ingest read-only for 24–48h and diff row counts against GCP.
2. Pick a window: **weekend, outside market hours, and not during results
   season.** Ingestion is idle, and a bad cutover mid-results is expensive.
3. Freeze writes on GCP → final incremental `pg_dump` → restore → flip the
   Cloudflare A record.
4. **Rollback** = flip the A record back. Keep the GCP VM stopped-but-not-deleted
   for at least 2 weeks (a stopped VM costs only disk).
5. Verify after cutover: all 8 admin-panel heartbeats green, nightly dossier
   refresh at 23:30 IST completes, the 20:30 deals cron runs, paper-trading
   dashboard populates.

---

## 4. Sequencing

| # | Step | Blocks |
|---|---|---|
| 0 | **Confirm which surface AWS credits cover** (Marketplace vs Bedrock) | Everything in B |
| 1 | ✅ Egress probe GO (2026-08-08 — confirm it ran from `ap-south-1`) | — |
| 1b | Allocate Elastic IP; confirm Angel One multi-IP allowlist + concurrent-session behaviour | Parallel run / cutover shape |
| 2 | EC2 build, DB restore, stack up on restored copy | — |
| 3 | LLM provider code + model remap, behind an env flag | — |
| 4 | Quality gates: dossier 0.90 benchmark + classification eval vs 23.8% | Cutover |
| 5 | Embedding re-index (or explicit decision to keep Vertex) | Cutover |
| 6 | Parallel run 24–48h, diff row counts | Cutover |
| 7 | Cutover in an off-hours window; GCP VM stopped, not deleted | — |
| 8 | Remove dead Gemini code paths (separate commit) | — |

Steps 0 and 1 are cheap and independent. Do both first — either one coming back
badly changes the whole plan.

---

## 5. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| AWS credits don't cover Claude Platform on AWS | Grounding layer must be rebuilt on a third-party search API | Resolve at step 0, before any code |
| NSE/BSE block the AWS IP | Migration is a non-starter on that host | Step 1 probe gates everything; fallback = NAT/proxy with a known-good IP, or a different region |
| Angel One single-IP allowlist | Repointing breaks the live GCP feed the moment it changes | Prefer multi-IP allowlist; if single-IP, move the change *into* the cutover window with a re-point rollback |
| Gemini→Claude quality regression on dossiers | Confident-but-wrong output — the project's known failure mode | Hard gate on the 0.90 benchmark + accuracy eval; prompts re-tuned for Claude |
| Embedding dim change corrupts retrieval | Event memory degrades silently | Additive column + held-out retrieval check before the swap |
| Disk exhaustion on the new host | Repeat of today's 80%-full state | Provision 100 GB and prune build cache |
| Cutover during results season | Expensive and highly visible | Weekend, off-hours, non-results window |

---

## 6. What is explicitly NOT in scope

- No re-architecture to RDS / MSK / ECS. Containers stay containers.
- No refactors adjacent to the migration.
- No deletion of Gemini code paths until after a proven cutover.
- No deletion of the GCP VM until at least 2 weeks post-cutover.

---

## 7. Execution log — 2026-08-12 night session

Context at session start: user traffic already LIVE on AWS (Cloudflare edge →
this origin, verified), Bedrock inference live since 21:39 IST, Haiku 4.5
throttling terminally on the account's 10 RPM clamped quota (~42% of
stage2/filing_summary calls failed in the first live window), Vertex paths
dead (no ADC on EC2), Aug 9–11 event-data hole.

### Done (verified)

- **Quota increases resubmitted** (the API only accepts desired > AWS default,
  which is why 100/50-RPM asks bounced with IllegalArgumentException):
  Haiku 4.5 RPM → 10,500 (**CASE_OPENED**), Sonnet 4.6 RPM → 10,500 (PENDING),
  Sonnet 5 TPM → 6.5M (PENDING). Watch/respond in the console — the instance
  role has no support:* permissions. Prior 12,000/8M/10M asks were CASE_CLOSED
  ungranted same day.
- **Client-side Bedrock RPM pacer** in `llm/wrapper.py` (per-model sliding
  60s window; calls queue instead of terminally 429ing; escape hatch
  BEDROCK_PACING_MAX_WAIT_S=240 then proceed-and-let-SDK-retry). Per-service
  budgets in compose: analysis 5 (Sonnet 4.6 override 7), kb-monitor 2,
  kb-service 4, api 1 — fleet sums under the 10 RPM quota. Logic unit-tested.
- **BEDROCK_MANTLE_REGION** (default us-east-1) — mantle serves Claude 4.5+
  only from us-east-1 (ap-south-1 404s, verified live); id form
  `anthropic.claude-haiku-4-5` confirmed correct (403 = IAM, not 404).
- **Embeddings provider switch** in `llm/embeddings.py`: EMBEDDING_PROVIDER
  vertex|bedrock; bedrock = `global.cohere.embed-v4:0` @1024 (live-verified;
  Titan rejected: 1 text/call at 60 RPM = ~10h backfill vs Cohere 96/call),
  task-types map 1:1 (search_document/search_query). EMBEDDING_COLUMN routes
  readers/writers (768→`embedding`, 1024→`embedding_1024`); swapped in
  memory index/retrieve, transcript embed, transcript search.
- **Migration 036** authored (additive embedding_1024 + HNSW on memory_items,
  transcript_chunks; companies untouched — still 0 embedded rows).
- **Backfill/verify scripts**: `scripts/backfill_transcript_embeddings_1024.py`
  (UPDATE-in-place, resumable), `scripts/verify_embedding_migration.py`
  (old-vs-new recall@10 on a seeded sample; gate <0.3),
  `scripts/backfill_result_events_window.py` (past-window calendar seed,
  idempotent via uq_result_event).
- **Images rebuilt** (kb, analysis, api) with all of the above; `.env` gained
  EMBEDDING_PROVIDER=bedrock, EMBEDDING_DIM=1024, RESEARCH_CHAT_MODEL=
  claude-sonnet-4-6 (research chat was hard-broken: api container still ran
  provider=vertex with a Gemini default model and no ADC).
- Memory-items re-embed strategy: NOT a parallel re-implementation — run the
  production `memory_index rebuild` (re-derives events from impact_reports
  over the 120d retention window, embeds all through the new provider).
  `embed_text` is not reconstructable from stored rows; rebuild is the
  faithful path. Transcript chunks ARE reconstructable (verbatim char spans +
  known prefix), hence the UPDATE-in-place script.

### Blocked at the permission layer (needs Aviral)

Auto-mode classifier denies prod-DB mutation and one-off containers from this
session. Remaining sequence, in order:

```
# 1. Migration 036 (kb-migrate default command is `alembic upgrade head`)
docker compose run --rm --no-deps kb-migrate

# 2. Transcript re-embed (~20 min at 18 req/min; resumable; re-run = no-op)
docker compose run --rm --no-deps -e BEDROCK_RPM_LIMIT_GLOBAL_COHERE_EMBED_V4_0=18 \
  kb-migrate python -m scripts.backfill_transcript_embeddings_1024

# 3. Memory index rebuild through the new provider (~10 min)
docker compose run --rm --no-deps -e BEDROCK_RPM_LIMIT_GLOBAL_COHERE_EMBED_V4_0=18 \
  kb-migrate python -m bharat_terminal.kb.memory.index rebuild

# 4. Retrieval-quality gate (read-only; FAILs the shell on <0.3 recall@10)
docker compose run --rm --no-deps kb-migrate python -m scripts.verify_embedding_migration

# 5. Single restart cycle onto new code+env (ONE analysis restart only —
#    expect the known ~90s redpanda rebalance storm, do not restart again)
docker compose up -d --no-deps analysis-worker kb-monitor kb-service api-gateway

# 6. Aug 9-11 result_events seed + XBRL pull
docker compose run --rm --no-deps kb-migrate \
  python -m scripts.backfill_result_events_window 2026-08-09 2026-08-11
#    then either wait for kb-monitor's _sync_due_events cycle, or force per-symbol
#    sync for the seeded symbols. NOTE monitor.py:184 marks events >3d old
#    no_data if XBRL isn't found — Aug 9 is at the boundary, run promptly.

# 7. BSE results catch-up for the window (FlagDur bucket over-covers, harmless)
docker compose run --rm --no-deps kb-migrate python -c \
  "import asyncio; from bharat_terminal.kb.sync.bse_financial_results import run_market_scan; asyncio.run(run_market_scan(since_days=5))"

# 8. Mantle IAM (run with ADMIN credentials, not the instance role):
aws iam put-role-policy --role-name bharat-terminal-ec2 \
  --policy-name bedrock-mantle-access \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"BedrockMantleClaudeAccess","Effect":"Allow","Action":["bedrock-mantle:*"],"Resource":"*"}]}'
```

### Known consequences / follow-ups

- news_items for Aug 9–11 are **structurally unrecoverable** through the
  normal path (adapters have a 48h age gate + no dated NSE endpoint + the
  write path runs the full LLM pipeline). Deliberately not backfilled.
- Old 768-dim columns retained everywhere (rollback + verification baseline);
  drop in a later migration once the swap has soaked.
- Dossier refresh still kill-switched (DOSSIER_REFRESH_MAX_PER_RUN=0) and
  DOSSIER_MODEL still Gemini — re-enabling is a separate decision (needs a
  Claude dossier model + the 0.90 RELIANCE benchmark gate).
- papertrade / commodity-engine / transcript-watcher / onboarding-worker still
  not started; host crons still commented out. Cutover checklist unfinished.
- NSE market_deals for Aug 9–11: NSE bulk/block CSVs are today-only snapshots —
  check `market_deals` coverage; the BSE leg is date-ranged (`--days 5`).
