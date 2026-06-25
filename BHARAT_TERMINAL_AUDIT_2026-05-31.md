# Bharat Terminal — System State Audit (2026-05-31)

> Evidence-based snapshot of what is built, what is running, and what is
> merely documented. Replaces optimistic claims in the prior overview
> with measured ground truth.
>
> **Audit executed:** 2026-05-31 11:46 UTC (`SELECT now()` → `2026-05-31 11:46:15+00`).
> All DB figures below were measured in the window 11:46–12:55 UTC on 2026-05-31.

**Source-of-truth note.** The task asked me to verify against
`BHARAT_TERMINAL_OVERVIEW.md`. **That file does not exist** — not in the
working tree and not in git history (`git log --all -- BHARAT_TERMINAL_OVERVIEW.md`
→ *path does not exist*). The closest artifacts are (a) `README.md` (the
public GitHub readme) and (b) the prior audit `BHARAT_TERMINAL_AUDIT_2026-05-24.md`
(dated, measurements taken 2026-05-30). I treat **both** as the
"prior overview / hypothesis to verify." A central theme of this audit
is that **the prior audit is itself partially stale**: four significant
fixes have landed in the last ~24–48h (financials migration, profile-field
population, the VM self-stop removal, an LLM wrapper) that materially
change the system's state. Each is flagged inline as *"diverges from
05-24 audit."*

**Freshness caveat.** The Docker stack was last (re)started by the systemd
unit at **2026-05-31 15:47 IST (10:17 UTC)** — all application containers
show `Up ~1 hour` in `docker ps`. Container logs therefore mostly cover
this boot; older history is gone. Where behaviour predates the boot I rely
on DB timestamps and surviving pre-boot log lines. Note also that several
repo files (`docker-compose.yml`, `.env.example`, `requirements/`) were
edited at ~16:54 IST *after* this boot — so the **running images can be
older than the source on disk**, a distinction that matters below.

---

## 0. Audit Plan (method)

**Queries run** (all read-only, via `docker exec bharat-postgres psql -U bharat -d bharat_terminal`):
- `pg_stat_user_tables` for inventory; exact `count(*)` per table.
- `information_schema.columns` to pick the correct time/identity columns.
- Per-table `min`/`max` of the primary time column; last-30-day filters.
- Per-column `NULL%` for `companies` and `financial_results`.
- Distribution queries: `nse_filings` parse-state + filing-year histogram;
  `financial_results` period_end coverage by quarter; `impact_reports`
  relevance / company_impacts / trade_signals / per-day; `news_items` by
  source + future-dated; `price_points` coverage; `result_events`
  sync_status; full `sync_log` dump; `derivative_ticks`; `participant_oi`;
  dossier tables; the seven scaffolded tables.

**Logs examined:** `docker logs --tail 200–400` for `bharat-analysis`,
`bharat-commodity`, `bharat-kb-monitor`, `bharat-sentiment`,
`bharat-ingestion`, filtered for error/exception/403/429/vertex/anthropic.

**Files inspected:** `README.md`, `docker-compose.yml`, `CLAUDE.md`,
`.env` vs `.env.example` (names only), `scripts/deploy/bharat-terminal.service`,
`bharat_terminal/llm/wrapper.py`, `bharat_terminal/analysis/batch_processor.py`
(import/call sites), `bharat_terminal/ingestion/main.py`, and a delegated
read of `frontend/src/` (tabs → components → endpoints).

**Infrastructure:** `docker ps -a`; `systemctl status bharat-terminal.service`;
`gcloud compute instances describe` (scheduling/status); live `curl -I https://taperead.trade`;
Anthropic 1-token API probe (HTTP status only); `docker exec … env` and
`docker inspect … --format {{.Created}}` to compare running-image age vs git.

**Estimated time:** ~50 min of checks; this document is the deliverable.

**Constraint honoured:** no production data, config, or service was modified.
All operations were reads.

---

## 1. Executive Summary

**What is actually working vs what is claimed.** The data-ingestion spine is
genuinely live and is in materially *better* shape than the 05-24 audit found.
News ingestion (7 sources) is flowing through 2026-05-31; `analysis-worker`
consumes with zero lag; the API gateway, KB service, and frontend are healthy
and `https://taperead.trade` returns HTTP 200 via Cloudflare (Mumbai edge).
**Three of the prior audit's headline problems have been fixed since 05-24:**
(1) `financial_results` now covers quarters **through 2026-03-31** (was frozen
at 2024-12-31) — the NSE Integrated-Filing migration works; (2) `price_points`
now reaches **2026-05-29**, the last trading day, and is current (was frozen at
2026-05-13); (3) the **GCP VM no longer carries the 5-hour `maxRunDuration` STOP**
— the scheduling block now shows only `automaticRestart/MIGRATE/STANDARD`.
`result_events` synced jumped from **6 → 535**.

**However, the headline feature — the AI analysis pipeline — is still dark.**
The Anthropic key remains over its usage limit (live probe → HTTP 400,
"regain access on 2026-06-01 at 00:00 UTC"). As a direct result **no
`impact_report` has carried a real company_impact since 2026-05-22 10:31 UTC,
nor a trade_signal since 10:21 UTC** — now **nine days** of relevance-only stubs.
Stage 1 (a *local* zero-shot classifier) runs; Stages 2/3/5 (LLM) fail closed.

**Most surprising findings.** (1) A provider-agnostic LLM wrapper
(`bharat_terminal/llm/wrapper.py`) was committed today (`eeb9e60`,
2026-05-31 16:55 IST) to migrate Claude calls onto **Vertex AI** — but the
**running `analysis-worker` image was built 2026-05-11** (`docker inspect`),
three weeks *before* the wrapper, and is still calling `api.anthropic.com`
directly. The migration is **in source only, not deployed**; the running
container's env has **no `LLM_PROVIDER`/`VERTEX_*` vars** at all. So Vertex is
not in production use, and even a redeploy is reported (project memory) to be
**blocked on granting `roles/aiplatform.user` to the VM service account**.
(2) The marquee impact-graph example still cannot work: `company_relationships`
— the table Stage-4 propagation traverses — is **still empty (0 rows)**.
(3) `sync_log` now has **three** zombie `running` rows (ids 6, 13, 14), up from
one. (4) The Stage-1 *local* classifier is repeatedly **breaching its own 800 ms
SLA** (observed 1.6–2.7 s per item) — a new performance regression in the one
LLM-free stage.

**Critical gaps.** The LLM pipeline (the core product) is dark and will stay so
until at least 2026-06-01, and the path back to "lit" now runs through an
**undeployed** Vertex migration that is itself IAM-blocked. Company *profile*
enrichment is **partial**: `mcap_cr` and `isin` are now ~92% / ~99.7% populated
(big improvement vs the prior 100% NULL), `sector_nse` is 31.7% populated, but
`industry_nse`, `description_200w`, `revenue_segments`, and `embedding` remain
**100% NULL** — so the advertised pgvector entity-resolution still has *no data*.
The dossier/research layer is still a single hand-run PoC (RELIANCE only).
F&O tick capture is dormant (frozen at 2026-05-14). The commodity shock detector
has **still never fired** (`shocks=0` every cycle ever logged).

---

## 2. Database State (per table, classified)

Database `bharat_terminal` (Postgres 15 + pgvector). 26 data tables in `public`
(+ `alembic_version`, head = migration **009**). Time column chosen per table is
named in each row.

### 2.1 Summary table

| Table | Rows | Time column | Earliest → Latest | Rows last 30d | Classification |
|---|---:|---|---|---:|---|
| `companies` | 2,367 | `updated_at` | all 2026-05-31 (bulk refresh) | 2,367 | **[OPERATIONAL]** rows; profile fields **partial** (see 2.2) |
| `financial_results` | 54,063 | `period_end` | 2017-09-30 → **2026-03-31** | n/a | **[OPERATIONAL]** — *now current* (diverges from 05-24) |
| `nse_filings` | 54,835 | `filing_date` | 2018-05-21 → **2026-05-31** | — | **[OPERATIONAL]** — *live path now works* (diverges from 05-24) |
| `price_points` | 558,296 | `date` | 2025-05-09 → **2026-05-29** | — | **[OPERATIONAL]** — current to last trading day (diverges from 05-24) |
| `impact_reports` | 5,611 | `created_at` | 2026-05-10 → 2026-05-31 | 5,611 | **[OPERATIONAL]** ingest+Stage1; **[DORMANT]** Stage2–5 |
| `news_items` | 4,116 | `created_at` | 2026-05-10 → 2026-05-31 | 4,116 | **[OPERATIONAL]** |
| `result_events` | 1,736 | `synced_at` | synced through 2026-05-31 | 1,736 | **[OPERATIONAL]** — 535 synced (was 6) |
| `participant_oi` | 216 | `date` | 2026-05-13 → 2026-05-29 | 216 | **[OPERATIONAL]** |
| `derivative_ticks` | 1,859 | `timestamp_utc` | **2026-05-14 09:24 → 10:03 only** | 0 | **[DORMANT]** |
| `dossier_sensitivities` | 12 | `generated_at` | 2026-05-11 only | 0 | **[SCAFFOLDED]** (RELIANCE PoC) |
| `dossier_linkages` | 8 | `generated_at` | 2026-05-11 only | 0 | **[SCAFFOLDED]** (RELIANCE PoC) |
| `company_dossiers` | 1 | `generated_at` | 2026-05-11 only | 0 | **[SCAFFOLDED]** (RELIANCE PoC) |
| `macro_events` | 9 | `created_at` | 2026-05-11 only | 9 | **[SCAFFOLDED]** seed |
| `event_sector_edges` | 43 | `created_at` | 2026-05-11 only | 43 | **[SCAFFOLDED]** seed |
| `event_transmissions` | 7 | (none) | seed (2026-05-11) | — | **[SCAFFOLDED]** seed |
| `event_degrees` | 27 | (none) | seed | — | **[UNCLAIMED]** seed |
| `triggers` | 36 | `created_at` | 2026-05-11 only | 36 | **[SCAFFOLDED]** reference seed |
| `sync_log` | 17 | `started_at` | 2026-05-10 → **2026-05-31** | — | **[OPERATIONAL]** (writing again) but **3 zombie rows** |
| `corporate_actions` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `shareholding_patterns` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `derivatives_eod` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `trigger_betas` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `fundamental_exposures` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `sector_leaders` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `universe_membership` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `company_relationships` | 0 | — | — | 0 | **[SCAFFOLDED]** — *empties Stage-4 propagation* |
| `alembic_version` | 1 | — | head = 009 | — | n/a |

### 2.2 Scrutinised tables (detail)

**`companies` (2,367 rows; NSE-only, 100% `exchange='NSE'`, no BSE).**
- **Per-column NULL% (n = 2,367), measured 2026-05-31:**

  | Column | NULL% | vs 05-24 audit |
  |---|---:|---|
  | `sector_nse` | **68.3** | was 100.0 → **750 rows now filled (31.7%)** |
  | `mcap_cr` | **7.7** | was 100.0 → **2,184 filled (92.3%)** |
  | `isin` | **0.3** | was 100.0 → **2,361 filled (99.7%)** |
  | `financials_updated_at` | 23.3 | was 31.4 |
  | `industry_nse` | **100.0** | unchanged |
  | `description_200w` | **100.0** | unchanged |
  | `revenue_segments` | **100.0** | unchanged |
  | `embedding` | **100.0** | unchanged |

- **Diverges from 05-24 audit:** commit `3b7256a` ("populate company profile
  fields from official NSE sources") materially filled `isin`, `mcap_cr`, and
  ~⅓ of `sector_nse`. **But `industry_nse`, `description_200w`,
  `revenue_segments`, and `embedding` are still 100% NULL** — so the
  pgvector / HNSW "entity resolution" claim still has **no backing data**, and
  company descriptions/segments are absent.
- All 2,367 `updated_at` timestamps fall in a single 26-minute window today
  (2026-05-31 10:46→11:12 UTC) — i.e. a **bulk refresh ran during this boot**,
  not incremental per-company updates. Classification: row-level
  **[OPERATIONAL]**; profile enrichment **[OPERATIONAL-partial]** for
  isin/mcap/sector, **[SCAFFOLDED]** for industry/description/segments/embedding.

**`company_dossiers` / `dossier_sensitivities` / `dossier_linkages`.**
Still **one** dossier (`RELIANCE`, `generated_at 2026-05-11 07:29`), 12
sensitivities + 8 linkages, all stamped 2026-05-11. Unchanged since the prior
audit. The research layer has run **once, by hand, for one company.** →
**[SCAFFOLDED]**.

**`financial_results` (54,063 rows; 2,259 distinct symbols).** **Diverges
sharply from 05-24 audit.** `period_end` now ranges **2017-09-30 → 2026-03-31**.
Recent quarters are populated: Q ending 2025-03-31 → 2,134 rows; 2025-06-30 →
2,183; 2025-09-30 → 2,220; 2025-12-31 → 2,199; **2026-03-31 → 2,199**. The
~18-month gap the prior audit flagged is **closed** (commits `f3b2418`,
`c7e2820`). NULL%: `revenue_cr` 1.9, `pat_cr` 2.6, `eps_basic` 1.9,
`nse_filing_id` 0.0, `segment_data` 0.0. → **[OPERATIONAL]**, coverage current.

**`nse_filings` (54,835 rows).** Parse states: `xbrl_downloaded=t, xbrl_parsed=t
→ 54,589 (99.6%)`; `f,f → 246`. **(There is no `parse_error` column** in the
schema — only the boolean `xbrl_parsed`; the brief's "parse_error states" does
not map to a column.) Filing-year histogram: 2018:3,840 … 2024:7,775,
**2025:8,443, 2026:4,436**, `max(filing_date)=2026-05-31` (today). **Diverges
from 05-24 audit** (which saw 2026:1). Live filing ingestion is working. →
**[OPERATIONAL]**.

**`price_points` (558,296 rows; 2,346 symbols, 261 trading days).** Range
2025-05-09 → **2026-05-29**. Daily ~2,330 symbols, continuous across recent
trading days (05-25…05-29 all present; 05-23/24 and 05-30/31 are
weekend/non-trading). Last trading day was Fri 2026-05-29; today is Sun 05-31, so
prices are **current**. → **[OPERATIONAL]** (was [DORMANT] in 05-24 audit).

**`derivative_ticks` (1,859 rows).** `FUT` only. All timestamps in a single
~40-min window **2026-05-14 09:24 → 10:03** across 51 symbols. **Unchanged**
since the prior audit — one run two weeks ago, nothing since. → **[DORMANT]**.

**`participant_oi` (216 rows).** 9 dates, 2026-05-13 → 2026-05-29, 24 rows/date.
kb-monitor logs `no CSV for 2026-05-30 (404) — likely non-trading day` (correct;
Saturday). → **[OPERATIONAL]**.

**`news_items` (4,116 rows).** By source: `NSE_FILINGS 1,759`, `PTI 641`,
`MINT 579`, `ECONOMIC_TIMES 527`, `INVESTING_IN 228`, `REUTERS_INDIA 226`,
`MARKETS_FEED 156`. **7 sources** producing rows (no BSE rows). **28 rows are
future-dated** (`timestamp_utc > now()`, NSE_FILINGS up to 2026-06-09) — a
data-quality bug (was 20 at the prior audit; growing). → **[OPERATIONAL]** with a
data-hygiene defect.

**`impact_reports` (5,611 rows).** `relevant=true: 3,844` (of which **267 carry
company_impacts, 54 carry trade_signals**); `relevant=false: 1,767`. **Last
report with a real company_impact: 2026-05-22 10:31:12; last with a trade_signal:
2026-05-22 10:21:09.** Per-day since: 05-23…05-31 every day has
`with_impacts=0, with_signals=0` (e.g. 05-31: 287 reports, 0 impacts, 0 signals).
`macro_theme` last 5 days: **890 blank vs 135 SECTORAL** — the signature of
Stage-2 extraction not running. → ingest+Stage-1 **[OPERATIONAL]**; Stage-2–5
**[DORMANT]** (Anthropic cap, §6).

**`result_events` (1,736 rows).** `sync_status`: `no_data 1,100`, `synced 535`,
`pending 101`. **Diverges from 05-24 audit** (which saw only 6 synced). NSE
result→XBRL sync is now substantially functional; `max(synced_at)` = today.
→ **[OPERATIONAL]**.

**`sync_log` (17 rows).** Now writing again — entries 12–17 cover financials/price
syncs on 2026-05-30 and 2026-05-31 (id 16 `financials` succeeded 05-31 11:13;
id 17 `prices` succeeded 05-31 10:44). **But there are now THREE zombie
`running` rows with `completed_at = NULL`: id 6 (financials, started 2026-05-10),
id 13 (2026-05-30 15:18), id 14 (2026-05-30 15:27)** — two of them new since the
prior audit. → **[OPERATIONAL]** as a surface, but telemetry is leaking
never-closed rows.

**`triggers` (36), `macro_events` (9), `event_sector_edges` (43),
`event_transmissions` (7), `event_degrees` (27).** All stamped 2026-05-11 07:25 —
a one-time seed of the macro-event-propagation reference graph. No ongoing
writes. → **[SCAFFOLDED]** seed (`event_degrees` additionally **[UNCLAIMED]**).

**`company_relationships`, `trigger_betas`, `fundamental_exposures`,
`sector_leaders`, `universe_membership`, `corporate_actions`,
`shareholding_patterns`, `derivatives_eod` — all 0 rows.** Schema exists
(migrations 004/006/007/009). Never produced output. → **[SCAFFOLDED]**.
`company_relationships` being empty means Stage-4 propagation and
`GET /graph/{symbol}` have nothing to traverse. *(See §9.)*

---

## 3. Service State (per service, classified)

`docker ps -a` (2026-05-31): 13 application/infra containers `Up ~1 hour`
(systemd boot 10:17 UTC). `bharat-fno-engine` and `bharat-kb-backfill` show
`Exited (0) 2 weeks ago`. `bharat-console` (redpanda-console :8080) is **not
running** (excluded from the systemd start set). `bharat-kb-migrate` and
`bharat-redpanda-init` ran once and exited (0) at boot, as designed.

| Service | Source path | Status | Writing data? | Last error seen | Classification |
|---|---|---|---|---|---|
| ingestion-worker | `bharat_terminal/ingestion/` | Up | Yes — `news_items` to today | `in.investing.com … 403 Forbidden` (body-fetch); Angel One WS `_on_close() takes 2 args but 4 given` callback error | **[OPERATIONAL]** (BSE adapter dormant) |
| analysis-worker | `bharat_terminal/analysis/` | Up, lag 0 | Partially — relevance-only stubs | `Batch LLM call failed: 400 … API usage limits … 2026-06-01` (repeating) | **[OPERATIONAL]** Stage1 / **[DORMANT]** Stage2–5 |
| sentiment-worker | `bharat_terminal/sentiment/` | Up, connected | To Kafka only (no DB table) | none; only consumer-group churn | **[OPERATIONAL]** (output not persisted) |
| commodity-engine | `bharat_terminal/commodity/` | Up | **No output** — `shocks=0` every cycle ever | `aiokafka … UnknownMemberIdError` (group rebalance, 05-30) | **[OPERATIONAL]** consume / **[SCAFFOLDED]** output |
| fno-engine | `bharat_terminal/fno_engine/` | **Exited (0) 2wks** | Last wrote `derivative_ticks` 2026-05-14 | clean exit; not in systemd start set | **[DORMANT]** |
| kb-monitor | `bharat_terminal/kb/sync/` | Up | `result_events`/`participant_oi` yes; some NSE paths blocked | `www.nseindia.com … 403 Forbidden` (homepage path) | **[OPERATIONAL]** |
| kb-service | `bharat_terminal/kb/api.py` | Up (healthy) | Serves reads | `/health 200` | **[OPERATIONAL]** |
| kb-backfill | compose `--profile backfill` | **Exited (0) 2wks** | One-shot backfill | clean exit; opt-in profile | **[DORMANT]** (by design) |
| api-gateway | `bharat_terminal/api/` | Up (healthy) | Serves reads + WS relay | `/health 200` | **[OPERATIONAL]** |
| frontend | `frontend/` (nginx) | Up (healthy) | Serves SPA :3000 | none | **[OPERATIONAL]** |
| research-runner | `bharat_terminal/research/` | not running (`--profile research`) | one-shot, opt-in | — | **[SCAFFOLDED]** (RELIANCE PoC only) |
| redpanda-console | compose `redpanda-console` | **not running** | — | excluded from systemd | **[DOCUMENTED → not running]** |

**Ingestion adapters (`ingestion/main.py`).** Eight news adapters wired
(NSE_FILINGS, BSE_FILINGS, ECONOMIC_TIMES, MINT, MoneyControl→`INVESTING_IN`,
NDTV Profit→`MARKETS_FEED`, Reuters, PTI) plus **Polymarket**
(`POLYMARKET → raw.sentiment.in`, strict Fed+commodities universe filter,
`main.py:100-110`) and **Angel One MCX** (`ANGELONE → raw.commodity.in`,
optional fno). **`BSE_FILINGS` produces 0 rows** in `news_items` (effectively
dormant), the `INVESTING_IN`/`MARKETS_FEED` aliases are misleading
(MoneyControl/NDTV under the hood), and `in.investing.com` body-fetches return
403. The Angel One WS shows a recurring callback-signature bug
(`_on_close() takes 2 positional arguments but 4 were given`).

**Analysis pipeline (5 stages, `analysis/stages/`).** Stage 1 = **local**
zero-shot relevance classifier (runs offline) — but is **breaching its 800 ms
SLA repeatedly** (observed 1.6–2.7 s/item, e.g.
`[stage1] SLA breach: 2700ms > 800ms (local)`). Stages 2 (extraction),
3 (impact), 5 (signals) call the LLM and **fail closed** on the Anthropic 400.
Stage 4 (propagation) traverses the **empty** `company_relationships`.
Net `[BATCH] Published: relevant=True, companies=0, signals=0` on every batch.

**LLM routing — important nuance.** The source on disk *has* been migrated:
`batch_processor.py:19-20,135` imports `bharat_terminal.llm` and calls
`await llm.acomplete(...)`; the wrapper (`bharat_terminal/llm/wrapper.py`)
defaults `LLM_PROVIDER=vertex` and would route `claude-*` to `AnthropicVertex`.
**But the running container does not reflect this:** `docker inspect
bharat-terminal-analysis-worker` shows the image was **built 2026-05-11
06:28 UTC**, three weeks before the wrapper commit (`eeb9e60`, 2026-05-31
16:55 IST); `docker exec bharat-analysis env` shows **no `LLM_PROVIDER` and no
`VERTEX_*`** vars; and the live log shows `POST https://api.anthropic.com/v1/messages`
(the *console* API, not Vertex). **Conclusion: the Vertex migration is in source
only and is not deployed.** Per project memory it is additionally blocked on
granting `roles/aiplatform.user` to the VM service account, so a redeploy alone
may not light it up. → wrapper **[SCAFFOLDED in prod]**; analysis effectively on
the capped console key.

**commodity-engine.** Heavy consumption logged (e.g.
`ticks=17500 shocks=0 | CRUDEOIL=4028 GOLD=3964 SILVER=3926 …` on 2026-05-27)
but **`shocks=0` in every cycle ever logged** — the z-score detector has never
fired. Last real tick activity 2026-05-27 18:18 (Angel One WS connects only in
market hours). → **[OPERATIONAL]** consume / **[SCAFFOLDED]** output.

**kb-monitor.** Scheduled tasks run (results calendar; event→XBRL sync; daily
bhavcopy; weekly sweep; participant-OI). The **homepage path
`www.nseindia.com` still returns 403**, but the integrated-filing/event endpoints
now succeed enough to drive 535 synced `result_events` and current
`financial_results`. `event_sync` logs `filings_found=5, parsed=0, skipped=5`
for fresh events ("no XBRL yet, will retry") — expected for very recent results.

---

## 4. Pipeline End-to-End Check

**Flow A — News → Analysis → Impact Report.** Ingestion writes `news_items`
(today) → `raw.news.in` (lag 0) → Stage 1 (local) labels relevance →
**Stages 2–5 fail on the Anthropic 400**. `impact_reports` rows are still
created, but **company_impacts/trade_signals have been empty since 2026-05-22**
(9 days). **Verdict: runs end-to-end structurally, produces no analytical output.**
May self-heal at 2026-06-01 00:00 UTC *only if* the running image still points at
the console key (it does) — unverifiable until then.

**Flow B — XBRL → Financials → Dossier.** `nse_filings` (54.8k, 99.6% parsed) →
`financial_results` (54k, **current to 2026-03-31**). **This flow is now healthy**
(diverges from the prior audit's "stale ≤2024-12-31"). Dossier generation has run
**once, for RELIANCE**. **Verdict: financials flow operational and current;
dossier flow remains a manual PoC.**

**Flow C — Angel One → Commodity shock → Impact.** Ticks flow into
commodity-engine but **0 shocks detected, ever** → no output impact_report.
**Verdict: consuming but never producing.**

**Flow D — Angel One → F&O ticks → derivative_ticks.** Ran **once on 2026-05-14**
then the container exited and is not in the auto-start set. **Verdict: dormant.**

**Flow E — Sentiment (Polymarket).** ingestion → `raw.sentiment.in` →
sentiment-worker (running, connected) → `analysed.sentiment.in` → API/WS →
Sentiment tab. **No DB persistence** (Kafka/Redis only), so historical state
can't be verified from the DB. **Verdict: wired and live as of this boot, but
unauditable via DB.**

---

## 5. Frontend Functional State

7 tabs defined in `frontend/src/App.tsx:39-47` — **all 7 present**: Live Feed
(`feed`), Sentiment, Companies, Impact Graph (`graph`), Signals (`trade`),
Watchlist, Database. (Static read; the app was not launched, so "renders" is
inferred from the component/route wiring, not a live screenshot.)

| Tab | Wired? | Reflects real DB state? | Notes / evidence |
|---|---|---|---|
| Live Feed | Yes | Yes | `NewsFeed` → `/impact/feed` + WS `/ws/feed` (`useImpactFeed.ts:6,19`). News is live. |
| Sentiment | Yes | Partially | `SentimentPanel` → `/sentiment/{latest,recent,themes}` + WS. Depends on sentiment-worker; empty-state when no events. |
| Companies | Yes | **Partially — profile fields uneven** | `CompaniesBrowser` → `/companies`, `/companies/{symbol}/{financials,prices,linkages}`, `/dossier/{symbol}` (`api.ts:58-91`). Financials current; mcap/isin mostly present; sector present for ~32%; industry/description/segments blank. |
| Impact Graph | Yes | **Mostly empty** | `ImpactGraph` default trigger hardcoded `'BRENT'` (`ImpactGraph.tsx:190`), overridden by `/dossier/triggers/` if any have coverage (`:201-214`); reads `/dossier/impact-graph/` (`:221`). Only the RELIANCE dossier exists → empty for almost all triggers. |
| Signals | Yes | **Empty now** | `TradeIdeas` reads `report.trade_signals` from the WS feed (`TradeIdeas.tsx:23-28`); count in tab label (`App.tsx:37`). **No signal generated since 2026-05-22**, so effectively empty. |
| Watchlist | Yes | Yes | Server-persisted via `/watchlist/` GET/POST (`api.ts:35,38`). |
| Database | Yes | Yes | `DatabaseExplorer` → `/db/tables`, `/db/{table}` (`api.ts:127-135`); faithfully reflects the (sparse) DB. |

**Companies sector filter — double finding (unchanged from prior audit).** The
API supports a `sector` param (`api.ts:60,67-68`), but **`CompaniesBrowser.tsx:48-51`
never passes it and there is no sector-filter UI control.** So even setting aside
the 68.3% NULL `sector_nse`, there is no way for a user to trigger a sector filter.

**Console errors / stale code:** the delegated read found **no TODO/FIXME, no
mock data, no dead code** in the `.tsx`/`.ts` tree — only legitimate empty-state
messaging and hardcoded layout constants (`ImpactGraph.tsx:15-31`). I did **not**
run the app or open a browser, so genuine runtime console errors are *unverified*
— confirming them would require launching the SPA against the live API.

---

## 6. Environment and Infrastructure

**`.env` keys (presence only — no values read).** Present:
`ANTHROPIC_API_KEY`, `DATABASE_URL`, `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS`,
`KB_SERVICE_URL`, `EXTRACTION_MODEL`, `IMPACT_MODEL`, `SIGNALS_MODEL`,
`RELEVANCE_THRESHOLD`, `LLM_LOG_FILE`, `POLYMARKET_MIN_VOLUME_TOTAL`,
`RESEARCH_MODEL/MAX_TURNS/ENABLE_WEB_SEARCH`, the four commodity tuning vars,
and **all four Angel One creds** (`ANGEL_ONE_API_KEY/CLIENT_CODE/PASSWORD/TOTP_SECRET`).
No key has a blank value.
- **Drift vs `.env.example`** (keys in the example but **missing from `.env`**):
  `LLM_PROVIDER`, `VERTEX_PROJECT`, `VERTEX_CLAUDE_REGION`, `VERTEX_GEMINI_REGION`,
  `DOSSIER_MODEL`, `RELEVANCE_BACKEND`, `RELEVANCE_MODEL`,
  `RELEVANCE_ANTHROPIC_MODEL`, `RELEVANCE_LOCAL_THRESHOLD`,
  `RELEVANCE_CACHE_TTL_SEC`, `RELEVANCE_TIMEOUT_SEC`. `.env` keeps the legacy
  `RELEVANCE_THRESHOLD` (renamed in the example). **The absence of `LLM_PROVIDER`
  and `VERTEX_*` in `.env` is why the running stack is not on Vertex** — and is a
  config change that must accompany the wrapper deploy.

**API keys (presence/working).**
- **Anthropic — present but NOT working.** Live 1-token probe to
  `api.anthropic.com/v1/messages` → **HTTP 400** ("You have reached your specified
  API usage limits. You will regain access on 2026-06-01 at 00:00 UTC."). Matches
  the `bharat-analysis` log. Root cause of the dark pipeline.
- **Angel One TOTP + creds — present** (all four). Ticks flowed (05-14/27); the WS
  callback bug above is non-fatal to ingestion but worth fixing. Not re-tested live
  (would require a market-hours WS connect; today is Sunday).
- **Vertex / GCP ADC** — not exercised by any running container (no `VERTEX_*` env);
  unverified whether `roles/aiplatform.user` is granted (project memory says it is
  **not** yet). Would require `gcloud … test-iam-permissions` to confirm.

**GCP VM.** Instance `instance-20260508-044518`, zone `asia-south1-c`,
**status RUNNING**. **`scheduling` now: `automaticRestart: true`,
`onHostMaintenance: MIGRATE`, `preemptible: false`, `provisioningModel: STANDARD`
— and crucially NO `maxRunDuration` / `instanceTerminationAction` block.**
**This is a major divergence from the 05-24 audit**, whose headline finding was a
5-hour self-stop; that scheduling constraint has been removed. The VM no longer
self-terminates on a timer. (The stack still shows a fresh boot at 10:17 UTC today,
but that is a one-off restart, not a recurring 5-hour cycle.)
- **Auto-start:** `systemctl status bharat-terminal.service` → `enabled` +
  `active (exited)`, last started Sun 2026-05-31 15:47 IST.
  `ExecStart=docker compose up -d kb-service kb-monitor ingestion-worker
  analysis-worker sentiment-worker commodity-engine api-gateway frontend
  prometheus grafana`. **`fno-engine`, `research-runner`, `kb-backfill`,
  `redpanda-console` are deliberately excluded** — consistent with fno-engine
  being dormant.

**Cloudflare / `taperead.trade`.** `curl -I` → **HTTP/2 200**, `server: cloudflare`,
`cf-ray: …-BOM` (Mumbai edge), `cf-cache-status: DYNAMIC`, `cache-control:
public, max-age=300`, plus a **content-security-policy** header (default-src 'self';
script-src self + unpkg + cloudflareinsights; etc.) and NEL `report-to`. Proxy
(orange-cloud) is ON, dynamic caching, CSP and NEL enabled. **How origin traffic
reaches the VM (named tunnel elsewhere vs direct origin pull) is *unable to
determine from this host*** — `cloudflared` is not installed here and no tunnel
config is in the repo; would require the Cloudflare dashboard / API token.

---

## 7. Documented-but-Missing Items (claims with no/insufficient backing)

Drawn from `README.md` and the prior audit's "prior overview" claims:

- **"~5,000 NSE/BSE companies"** → 2,367, **NSE-only**, no BSE. **[DOCUMENTED, overstated]**
- **"pgvector entity resolution / HNSW embeddings"** → `companies.embedding`
  still **100% NULL**; no embeddings exist. **[DOCUMENTED → SCAFFOLDED]**
- **Company descriptions / revenue segments / industry classification** →
  `description_200w`, `revenue_segments`, `industry_nse` all **100% NULL**.
  **[DOCUMENTED → SCAFFOLDED]**
- **"Analyst consensus / DCF" as a live feature** → DCF columns populated once
  (2026-05-10, per `sync_log` id 3/9), not refreshed; consensus columns NULL.
  **[DOCUMENTED → DORMANT/SCAFFOLDED]**
- **"8 Indian financial sources" incl. explicit BSE / MoneyControl / NDTV** →
  7 sources produce rows; BSE = 0; MoneyControl/NDTV appear only as
  `INVESTING_IN`/`MARKETS_FEED` aliases. **[DOCUMENTED, divergent]**
- **"Impact graph: RIL ripples to Jio, RRVL, IOC" / "kb-seed seeds 7
  relationships"** → `company_relationships` = **0 rows**; Stage-4 has nothing to
  traverse. **[DOCUMENTED → SCAFFOLDED, false]**
- **"Redpanda Console :8080"** → defined in compose, **not running** (excluded
  from systemd). **[DOCUMENTED → not running]**
- **"≤5s end-to-end P95" SLA** → unverifiable while Stages 2–5 are down; and the
  one always-on stage (Stage-1 local) is *itself* breaching its 800 ms budget at
  1.6–2.7 s. **[DOCUMENTED, currently violated]**
- **Vertex AI migration "live"** (implied by `docker-compose.yml` defaults) →
  not deployed; running image predates the wrapper; no `VERTEX_*` env in the
  container. **[DOCUMENTED-in-compose → SCAFFOLDED in prod]**

---

## 8. Unclaimed Items (in code/DB, absent from the README)

The README describes a first-generation "news → impact" product. The repo/DB
contain a larger second-generation system the README never mentions:

- **Sentiment / Polymarket subsystem** — `bharat_terminal/sentiment/`,
  Polymarket adapter, `raw/analysed.sentiment.in`, Sentiment tab. **[UNCLAIMED]**
- **Commodity engine + Angel One MCX** — `bharat_terminal/commodity/`, z-score
  shock detector, `raw.commodity.in`. **[UNCLAIMED]**
- **F&O engine + derivatives** — `bharat_terminal/fno_engine/`,
  `derivative_ticks`, `derivatives_eod`, `participant_oi`. **[UNCLAIMED]**
- **Research / dossier agent** — `bharat_terminal/research/`, `company_dossiers`,
  `dossier_sensitivities`, `dossier_linkages`, `trigger_betas`,
  `fundamental_exposures`. **[UNCLAIMED]**
- **Macro-event propagation graph** — `triggers`, `macro_events`,
  `event_sector_edges`, `event_transmissions`, `event_degrees`, `sector_leaders`;
  the `/dossier/impact-graph/` endpoint + Impact-Graph tab are built on this, not
  on `company_relationships`. **[UNCLAIMED]**
- **Provider-agnostic LLM wrapper** — `bharat_terminal/llm/wrapper.py` (Vertex/
  Anthropic/Gemini routing), imported by analysis Stages 2/3/5, batch_processor,
  and the dossier agent. New as of today; not in any narrative doc. **[UNCLAIMED]**
- **Database explorer** — `/db/tables`, `/db/{table}` + Database tab. **[UNCLAIMED]**
- **Tables in no narrative:** `event_degrees`, `company_relationships` (empty),
  `universe_membership` (empty). **[UNCLAIMED]**

---

## 9. [FOUNDER ANNOTATION REQUIRED]

> Placeholders for context only the founder has. The audit measured *what*;
> these ask *why*. (Several prior-audit questions are now answered by the
> recent fixes; the open ones are below.)

1. **Anthropic budget cap** — Was the monthly limit hit intentionally (cost cap)
   or by surprise? Should Stages 2–5 degrade gracefully (queue + retry after the
   2026-06-01 reset) instead of silently writing relevance-only stubs?
2. **Vertex migration deploy** — The wrapper is committed but the running images
   are 3 weeks old and `.env` lacks `LLM_PROVIDER`/`VERTEX_*`. Is the plan to
   rebuild+redeploy with `LLM_PROVIDER=vertex`, and is the
   `roles/aiplatform.user` grant on `deployment@bharat-terminal.iam` done yet?
   *Why is X (Vertex) not yet live?*
3. **`industry_nse` / `description_200w` / `revenue_segments` / `embedding` still
   100% NULL** — the recent profile-population commit filled isin/mcap/sector but
   not these. Was the rest deprioritised, or is the source feed for descriptions/
   segments/embeddings not built? The embedding-based entity resolution remains
   data-less.
4. **`company_relationships` empty** — Is the RIL→Jio/RRVL/IOC relationship seed a
   known-broken/never-run dependency? Should Stage-4 instead traverse the
   dossier-linkage / macro-event graph that actually has data?
5. **commodity-engine `shocks=0` always** — Are the z/%/baseline thresholds
   mis-tuned, or has there genuinely been no qualifying shock? Has it *ever*
   produced a shock impact_report?
6. **fno-engine deprioritised?** — Excluded from systemd auto-start, dormant since
   05-14. Intentional pause, or forgotten? Same question for the Angel One WS
   `_on_close` callback bug.
7. **Sentiment output not persisted** — Is the Kafka/Redis-only design intentional
   (no historical sentiment table by design)?
8. **`sync_log` zombie rows (6, 13, 14)** — Are the two new 2026-05-30 `running`
   rows a crash mid-financials-sync, or a code path that never closes the row?
9. **Stage-1 local SLA breaches (1.6–2.7 s vs 800 ms)** — CPU starvation on the VM,
   a model-load regression, or a too-tight SLA? It's the only always-on stage.
10. **Residual NSE 403 on `www.nseindia.com`** — the integrated-filing path works
    now, but the homepage path still 403s. Is that path still needed anywhere?
11. **README staleness** — Is the README intentionally a "public face" describing
    only gen-1, with gen-2 (sentiment/commodity/F&O/research/Vertex) intentionally
    undocumented?

---

## 10. Recommended Priority Order for Fixes

Ordered **data integrity > functionality > polish**, one-line rationale each.

**P0 — Core product availability:**
1. **Restore the LLM pipeline.** Either (a) wait for the 2026-06-01 Anthropic
   reset and confirm the *running* image recovers, or (b) finish the Vertex
   deploy: grant `roles/aiplatform.user`, add `LLM_PROVIDER=vertex` + `VERTEX_*`
   to `.env`, **rebuild the analysis image** (the live one is from 05-11), and
   verify a real `acomplete` against Vertex. *Stages 2–5 — the product — are dark.*
2. **Make analysis fail loudly and recoverably** when the LLM is unavailable —
   stop writing relevance-only stubs indistinguishable from "nothing happened."
   *5,611 reports include 9 days of empty results that look processed.*

**P1 — Data integrity:**
3. **Populate `company_relationships`** (or repoint Stage-4 to the macro-event /
   dossier-linkage graph that has data). *Propagation and `/graph/{symbol}` are
   no-ops today.*
4. **Finish `companies` profile enrichment** — `industry_nse`,
   `description_200w`, `revenue_segments`, and especially `embedding`. *These gate
   the Companies tab and the advertised pgvector entity resolution.*
5. **Close the `sync_log` zombie rows (6, 13, 14)** and ensure every sync path
   closes its row. *Telemetry currently lies about in-flight syncs.*
6. **Scrub future-dated `news_items`** (28 rows with `timestamp_utc` in the
   future, up to 2026-06-09). *Confident-but-wrong timestamps.*

**P2 — Functionality:**
7. **Re-tune / validate the commodity shock detector** (`shocks=0` forever) and
   **re-enable/auto-start fno-engine** if derivatives are in scope; fix the Angel
   One `_on_close` callback bug.
8. **Run the dossier agent beyond RELIANCE** (≥ nifty50) so the Impact-Graph tab
   is non-empty.
9. **Investigate the Stage-1 local SLA regression** (1.6–2.7 s vs 800 ms budget).
10. **Add the Companies sector-filter UI control** (meaningful only after #4 fills
    `sector_nse` beyond 32%).

**P3 — Polish / config hygiene:**
11. **Reconcile `.env` with `.env.example`** (add `LLM_PROVIDER`, `VERTEX_*`,
    `RELEVANCE_*`; retire `RELEVANCE_THRESHOLD`). *Config drift is why Vertex
    isn't live.*
12. **Implement `/metrics`** on api-gateway/kb-service if Prometheus scraping is
    intended (prior audit saw 404s).
13. **Rename misleading source aliases** `INVESTING_IN`/`MARKETS_FEED` →
    MoneyControl/NDTV; fix or retire the BSE adapter (0 rows) and the
    `in.investing.com` 403 body-fetch.
14. **Rewrite `README.md`** to match reality (company count, source list, gen-2
    subsystems, Vertex direction) — and create the missing
    `BHARAT_TERMINAL_OVERVIEW.md` if a canonical overview is wanted.

---

*End of audit. All figures and statuses above are backed by the queries, log
lines, file paths, and commands cited inline and listed in §0. Items I could not
verify (Cloudflare origin path; live runtime console errors; live Vertex/ADC
auth; SLA latency while the LLM is down; live Angel One WS on a Sunday) are
explicitly marked "unable to determine." Where this audit diverges from
`BHARAT_TERMINAL_AUDIT_2026-05-24.md`, the divergence is flagged inline — the net
change in the last week is materially positive on data freshness (financials,
prices, filings, result_events) and on infrastructure (VM self-stop removed),
while the core LLM analysis pipeline remains dark.*
