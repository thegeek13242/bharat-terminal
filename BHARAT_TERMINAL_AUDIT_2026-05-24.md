# Bharat Terminal — System State Audit (2026-05-24)

> Evidence-based snapshot of what is built, what is running, and what is merely documented.
> Replaces optimistic claims in the prior overview (`README.md`) with measured ground truth.

**Audit executed:** 2026-05-30 13:22 UTC (`SELECT now()` → `2026-05-30 13:22:41+00`).
The filename retains the requested `2026-05-24` label; all measurements below were taken on 2026-05-30.

**Caveat on freshness of evidence:** the entire Docker stack was restarted by the systemd unit at **2026-05-30 18:47 IST (13:17 UTC)** — every container shows `Up ~4 minutes` in `docker ps`. Container logs therefore only cover the last few minutes of this boot; older log history is gone. Where a service's behaviour predates this boot, I rely on DB timestamps and pre-boot log lines that survived.

**Source-of-truth note:** This repo has **no dedicated "system overview" document**. The closest artifact is `README.md` (the public GitHub readme). It is treated here as the "prior overview / hypothesis to verify." A central finding is that `README.md` describes only a *subset* of the system (news → analysis → impact graph) and omits entire subsystems that exist in code and DB (sentiment/Polymarket, commodity/Angel-One, F&O, research/dossiers, macro-event propagation). Those omitted subsystems are flagged `[UNCLAIMED]`.

---

## 0. Audit Plan (method)

**Queries run (all read-only, via `docker exec bharat-postgres psql -U bharat -d bharat_terminal`):**
- `pg_stat_user_tables` + `pg_tables` for the full table inventory.
- Exact `count(*)` per table.
- `information_schema.columns` for every table's schema (to pick the correct time column).
- Per-table `min`/`max` of the primary time column + `count(*) FILTER (… >= now()-interval '30 days')`.
- Per-column `NULL%` for the highest-value columns of `companies` and `financial_results`.
- Distribution queries: `nse_filings` parse states & filing-year histogram; `impact_reports` relevance / signal presence / per-day; `news_items` by source & future-dated; `derivative_ticks` by symbol; `price_points` coverage & latest dates; `participant_oi` by date; `result_events` sync-status; full `sync_log` dump.

**Logs examined:** `docker logs --tail 600` for `bharat-ingestion`, `bharat-analysis`, `bharat-sentiment`, `bharat-commodity`, `bharat-kb-monitor`, `bharat-kb`, `bharat-api`, filtered for error/exception/403/429/401/websocket.

**Files inspected:** `README.md`, `docker-compose.yml`, `.env` vs `.env.example` (names only), `scripts/deploy/bharat-terminal.service`, plus a structured read of `bharat_terminal/{ingestion,analysis,sentiment,commodity,fno_engine,kb,research,api}/` and `frontend/src/` (tab/router/component → endpoint mapping).

**Infrastructure:** `docker ps -a`; `systemctl status bharat-terminal.service`; `gcloud compute instances describe` (scheduling/status); GCP metadata server; live `curl -I https://taperead.trade` (edge headers); Anthropic API 1-token probe (HTTP status only).

**Estimated time:** ~45–60 min of checks; this document is the deliverable.

**Constraint honoured:** no production data, config, or service was modified. All operations were reads.

---

## 1. Executive Summary

**What is actually working vs what is claimed.** The *ingestion spine* is genuinely live: `ingestion-worker` is pulling news (7 distinct sources, `news_items` growing through 2026-05-30), `analysis-worker` is consuming `raw.news.in` with zero consumer lag, and `impact_reports` rows are still being written daily. The API gateway, KB service and frontend are healthy and `https://taperead.trade` returns HTTP 200 through Cloudflare. **However, the headline feature — the AI analysis pipeline — is effectively dark.** The Anthropic API key is over its usage limit (live probe returns HTTP 400, "regain access on 2026-06-01"), and as a direct result **no `impact_report` has contained a real company impact since 2026-05-22 10:31 UTC, nor a trade signal since 10:21 UTC** — eight days of relevance-only stubs. So Stage 1 (a *local* zero-shot classifier) runs; Stages 2–5 (Claude Haiku) fail closed.

**Most surprising findings.** (1) The **GCP VM self-terminates every 5 hours**: `scheduling.maxRunDuration = 18000s` with `instanceTerminationAction: STOP`. This — not any single bug — is the structural cause of the stale/gappy data (prices frozen at 2026-05-13, missing days in `news_items`/`impact_reports`). (2) The README's marquee impact-graph example ("RIL ripples to Jio, RRVL, IOC") cannot work: the `company_relationships` table that Stage 4 propagation traverses is **empty (0 rows)**. (3) The README describes a *smaller* system than what exists — four entire subsystems (sentiment/Polymarket, commodity/Angel-One, F&O, research/dossiers) are real in code and partly in DB but **completely absent from the README**. (4) `sync_log` has logged nothing since 2026-05-14 and contains a **zombie `running` row** (id=6) open since 2026-05-10, even though incremental refreshes clearly still run — the current sync paths simply don't write to it.

**Critical gaps.** Company *profile* data is hollow: `sector_nse`, `industry_nse`, `description_200w`, `revenue_segments`, `mcap_cr`, `embedding`, `pe_ratio`, `median_target_price` and all analyst-consensus columns are **100% NULL** across all 2,367 companies. The `embedding` column being 100% NULL means the advertised "pgvector entity resolution / HNSW embeddings" does not exist as data. Live filing/price ingestion is blocked by NSE returning **403 Forbidden** to the VM. The dossier/research layer is a single hand-run proof of concept (1 company: RELIANCE). The whole thing runs on a 5-hour-leash VM with an exhausted LLM budget.

---

## 2. Database State (per table, classified)

Database: `bharat_terminal` (Postgres 15 + pgvector). 27 tables in `public` (+ `alembic_version`). Time column chosen per-table is named in each row.

### 2.1 Summary table

| Table | Rows | Time column | Earliest → Latest | Rows last 30d | Classification |
|---|---:|---|---|---:|---|
| `companies` | 2,367 | `updated_at` | 2026-05-10 → 2026-05-30 | 2,367 | **[OPERATIONAL]** (row-level), profile fields **[SCAFFOLDED]** |
| `financial_results` | 43,245 | `created_at` | 2026-05-10 → 2026-05-30 | 43,245 | **[OPERATIONAL]**, period coverage stale (≤2024-12-31) |
| `nse_filings` | 44,011 | `created_at` / `filing_date` | created 2026-05-10→05-30; filing_date 2018→2026-02-02 | 44,011 created / 0 by filing_date | **[OPERATIONAL]** backfill store; **[DORMANT]** live ingestion |
| `price_points` | 527,953 | `date` | 2025-05-09 → **2026-05-13** | 14,013 | **[DORMANT]** (frozen 17 days) |
| `impact_reports` | 5,155 | `created_at` | 2026-05-10 → 2026-05-30 | 5,155 | **[OPERATIONAL]** ingest+Stage1; **[DORMANT]** Stage2–5 |
| `news_items` | 3,818 | `created_at` | 2026-05-10 → 2026-05-30 | 3,818 | **[OPERATIONAL]** |
| `result_events` | 1,734 | `created_at` | 2026-05-10 → 2026-05-30 | 1,734 | **[OPERATIONAL]** calendar; sync mostly failing |
| `participant_oi` | 216* | `date` | 2026-05-13 → 2026-05-29 | 216 | **[OPERATIONAL]** |
| `derivative_ticks` | 1,859 | `timestamp_utc` | **2026-05-14 09:24 → 10:03 only** | 1,859 | **[DORMANT]** |
| `dossier_sensitivities` | 12 | `generated_at` | 2026-05-11 only | 12 | **[SCAFFOLDED]** (RELIANCE PoC) |
| `dossier_linkages` | 8 | `generated_at` | 2026-05-11 only | 8 | **[SCAFFOLDED]** (RELIANCE PoC) |
| `company_dossiers` | 1 | `generated_at` | 2026-05-11 only | 1 | **[SCAFFOLDED]** (RELIANCE PoC) |
| `macro_events` | 9 | `created_at` | 2026-05-11 only | 9 | **[SCAFFOLDED]** seed |
| `event_sector_edges` | 43 | `created_at` | 2026-05-11 only | 43 | **[SCAFFOLDED]** seed |
| `event_transmissions` | 7 | (none) | seed (2026-05-11) | — | **[SCAFFOLDED]** seed |
| `event_degrees` | 27 | (none) | seed | — | **[UNCLAIMED]** seed |
| `triggers` | 36 | `created_at` | 2026-05-11 only | 36 | **[SCAFFOLDED]** reference seed |
| `sync_log` | 11 | `started_at` | 2026-05-10 → **2026-05-14** | 0 | **[DORMANT]** (no writes since 05-14; zombie row) |
| `corporate_actions` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `shareholding_patterns` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `derivatives_eod` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `trigger_betas` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `fundamental_exposures` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `sector_leaders` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `universe_membership` | 0 | — | — | 0 | **[SCAFFOLDED]** |
| `company_relationships` | 0 | — | — | 0 | **[SCAFFOLDED]** — *empties Stage-4 propagation* |
| `alembic_version` | 1 | — | head = migration 009 | — | n/a (migration tracker) |

\* `participant_oi` was observed growing from 192 → 216 rows *during* the audit (kb-monitor inserted the 2026-05-29 batch mid-run) — direct evidence it is live.

### 2.2 Scrutinised tables (detail)

**`companies` (2,367 rows; README claims "~5,000 NSE/BSE").**
- Actual: **2,367 rows, exchange = `NSE` for 100%** (`SELECT exchange,count(*) … → NSE|2367`). **No BSE companies.** All `is_active = true`.
- **Per-column NULL% (n = 2,367):**

  | Column | NULL% | Column | NULL% |
  |---|---:|---|---:|
  | `sector_nse` | **100.0** | `industry_nse` | **100.0** |
  | `description_200w` | **100.0** | `revenue_segments` | **100.0** |
  | `mcap_cr` | **100.0** | `embedding` | **100.0** |
  | `pe_ratio` | **100.0** | `median_target_price` | **100.0** |
  | `isin` | **100.0** | `financials_updated_at` | 31.4 |
  | `fair_value_per_share` (DCF) | 32.7 | `wacc_pct` (DCF) | 32.7 |
  | `revenue_ttm_cr` | 32.4 | | |

- So **~68.6% have financials, ~67.3% have a DCF value**, but **sector/industry/description/segments/mcap/embedding/PE/consensus are entirely absent.**
- `dcf_updated_at` for all populated rows = **2026-05-10** (one-shot; matches `sync_log` id=3/9 `dcf_refresh`). DCF block is **[DORMANT]** — computed once, never refreshed.
- `consensus_updated_at` is empty (NULL everywhere) → analyst-consensus feature **[SCAFFOLDED]**, never populated.
- `embedding` 100% NULL → the "pgvector entity resolution / HNSW embeddings" claim has **no data** behind it (column exists, type `USER-DEFINED` = vector).
- `updated_at`/`financials_updated_at` advance one company at a time as results arrive (distinct timestamps, 1 row each: 2026-05-30 13:21, 05-27 16:56, 05-25 09:21…) — genuine incremental refresh, **not** a boot artifact.

**`company_dossiers` / `dossier_sensitivities` / `dossier_linkages`.** One dossier exists: `RELIANCE | v1.0.0 | source_quarter 2026-03-31 | generated_at 2026-05-11 07:29 | model claude-sonnet-4-6 | data_quality 0.88 | tokens_used 193,787`. Its 12 sensitivities + 8 linkages are all stamped 2026-05-11. The research layer ran **once, by hand, for one company**. → **[SCAFFOLDED]**.

**`financial_results` (43,245 rows; 2,053 distinct symbols).** Clean data — NULL%: `revenue_cr` 1.8, `pat_cr` 2.6, `eps_basic` 1.8, `nse_filing_id` 0.0, `raw_xbrl_facts` 0.0, `segment_data` 0.0. **But `period_end` ranges 2017-09-30 → 2024-12-31** — i.e. **no financial period after Dec-2024 has been ingested**, despite filings existing into 2026. `created_at`/`updated_at` reach today only because existing rows are re-touched by the result-driven refresh, not because new quarters land. → **[OPERATIONAL]** store, **stale coverage** (≈18-month gap on recent quarters). *(See FOUNDER ANNOTATION.)*

**`nse_filings` (44,011 rows).** Parse states: `xbrl_downloaded=t, xbrl_parsed=t → 43,768 (99.4%)`; `f,f → 243`. **No `parse_error` column exists** in the schema (the audit brief's "parse_error states" does not map to a column; only the boolean `xbrl_parsed` flag exists). Filing-year histogram: 2018:3,840 … 2024:7,775, **2025:2,054, 2026:1** — confirming a 2018-2025 backfill with essentially nothing in 2026 (`max(filing_date) = 2026-02-02`). Live filing pull is blocked (NSE 403 — §3). → backfill **[OPERATIONAL]**, live path **[DORMANT]**.

**`price_points` (527,953 rows).** 2,346 symbols, 248 trading days, **2025-05-09 → 2026-05-13**. Daily ≈2,335 symbols. Latest = 2026-05-13 → **frozen 17 days**; last bhavcopy `sync_log` entry = 2026-05-14 11:00. Visible gap 2026-05-09…05-12 (only 05-13 present in that window). → **[DORMANT]**.

**`derivative_ticks` (1,859 rows).** Instrument type = `FUT` only (no options). All timestamps fall in a **single ~40-minute window: 2026-05-14 09:24:26 → 10:03:15**, ~38 ticks each across ~49 symbols (NIFTY, SBIN, TATASTEEL, ICICIBANK…). One run, two weeks ago, then nothing. → **[DORMANT]**.

**`derivatives_eod` (0 rows).** Schema (with greeks delta/gamma/theta/vega) exists; never populated. → **[SCAFFOLDED]**.

**`participant_oi` (216 rows).** 9 dates, **2026-05-13 → 2026-05-29**, 24 rows/date (client_type × instrument). Grew during the audit. → **[OPERATIONAL]** (the one derivatives-adjacent flow that is live).

**`news_items` (3,818 rows).** By source: `NSE_FILINGS 1,619`, `PTI 583`, `MINT 543`, `ECONOMIC_TIMES 503`, `REUTERS_INDIA 217`, `INVESTING_IN 214`, `MARKETS_FEED 139`. **7 sources** (not the README's 8). **20 NSE_FILINGS rows are future-dated** (`timestamp_utc > now()`, up to 2026-06-09) — a data-quality issue. Continuous through 2026-05-30. → **[OPERATIONAL]**.

**`impact_reports` (5,155 rows).** `relevant=true: 3,521` (of which only **267 carry company_impacts, 54 carry trade_signals**); `relevant=false: 1,634`. **Last report with a real company_impact: 2026-05-22 10:31:12; last with a trade_signal: 2026-05-22 10:21:09.** Per-day after that: 05-23…05-30 all have `with_impacts=0, with_signals=0`. Recent reports' `macro_theme` is mostly empty (446 blank vs 135 `SECTORAL` in last 5 days) — the signature of Stage-2 extraction failing. → ingestion+Stage-1 **[OPERATIONAL]**; Stage-2–5 **[DORMANT]** (API cap, §5).

**`result_events` (1,734 rows).** `sync_status`: `pending 642`, `no_data 1,086`, `synced 6`. Only **6 events ever fully synced**; 642 stuck pending; 1,086 resolved to no data. `synced_at` max = today (the refresh still runs) but success rate is ~0.3%. → **[OPERATIONAL]** calendar population, **largely-failing** downstream sync (NSE 403).

**`sync_log` (11 rows).** Last entry **2026-05-14 11:00** (`prices/nse_bhavcopy`, 2,334 records). **Zombie row id=6:** `financials/nse_xbrl`, `started 2026-05-10 09:38`, `completed_at = NULL`, `status = running` — never closed. Failures recorded: id=2 (`records_failed=5`), id=8 (`records_failed=157`). Nothing logged since 05-14 even though `companies`/`financial_results`/`participant_oi` clearly updated since → **the live sync paths bypass `sync_log`.** → **[DORMANT]** as a telemetry surface.

**`triggers` (36), `macro_events` (9), `event_sector_edges` (43), `event_transmissions` (7), `event_degrees` (27).** All stamped 2026-05-11 07:25 — a one-time seed of the macro-event-propagation reference graph. Static; no ongoing writes. → **[SCAFFOLDED]** seed (`event_degrees` additionally **[UNCLAIMED]**).

**`trigger_betas`, `fundamental_exposures`, `sector_leaders`, `universe_membership`, `corporate_actions`, `shareholding_patterns` — all 0 rows.** Schema exists (migrations 004/006/007). Never produced output. → **[SCAFFOLDED]**.

**`company_relationships` — 0 rows.** This is the table Stage-4 propagation and `GET /graph/{symbol}` traverse. Empty ⇒ no propagation possible. Both **[SCAFFOLDED]** and effectively **[UNCLAIMED]** (README implies it is seeded; it is not). *See §6.*

---

## 3. Service State (per service, classified)

`docker ps` shows 13 containers `Up ~4 min` (booted by systemd 13:17 UTC). `bharat-fno-engine` and `bharat-kb-backfill` show `Exited (0) 2 weeks ago`. `redpanda-console` (README's port 8080) is **not running** (not in the systemd start set).

| Service | Source path | Status | Writing data? | Last error seen | Classification |
|---|---|---|---|---|---|
| ingestion-worker | `bharat_terminal/ingestion/` | Up | Yes — `news_items` to today | `in.investing.com … 403 Forbidden` (body-fetch for INVESTING_IN) | **[OPERATIONAL]** (BSE adapter **[DORMANT]**) |
| analysis-worker | `bharat_terminal/analysis/` | Up, lag 0 | Partially — relevance-only stubs | `Batch LLM call failed: 400 … reached your specified API usage limits … 2026-06-01` | **[OPERATIONAL]** Stage1 / **[DORMANT]** Stage2–5 |
| sentiment-worker | `bharat_terminal/sentiment/` | Up | To Kafka only (no DB table) | none (Kafka group churn only); last activity 2026-05-27 | **[OPERATIONAL]** (output not persisted → see annotation) |
| commodity-engine | `bharat_terminal/commodity/` | Up | **No output** — `shocks=0` every cycle | none; last tick log 2026-05-27 18:18 | **[OPERATIONAL]** consume / **[SCAFFOLDED]** output |
| fno-engine | `bharat_terminal/fno_engine/` | **Exited (0) 2wks** | Last wrote `derivative_ticks` 2026-05-14 | clean exit; not in systemd start set | **[DORMANT]** |
| kb-monitor | `bharat_terminal/kb/sync/` | Up | `participant_oi` yes; filings/prices blocked | `nseindia.com … 403 Forbidden`; `event_sync Done: synced 1, no_data 224` | **[OPERATIONAL]** (mostly blocked by NSE 403) |
| kb-service | `bharat_terminal/kb/api.py` | Up (healthy) | Serves reads | `/health 200`; (`/metrics 404`) | **[OPERATIONAL]** |
| kb-backfill | compose `--profile backfill` | **Exited (0) 2wks** | One-shot backfill | clean exit; opt-in profile | **[DORMANT]** (by design, one-shot) |
| api-gateway | `bharat_terminal/api/` | Up (healthy) | Serves reads + WS relay | `/health 200`; Prometheus hits `/metrics → 404` | **[OPERATIONAL]** |
| frontend | `frontend/` (nginx) | Up (healthy) | Serves SPA :3000 | none; reachable via Cloudflare 200 | **[OPERATIONAL]** |

**Ingestion adapters (8 in code, `ingestion/main.py:171-180`):** NSE_FILINGS, BSE_FILINGS, ECONOMIC_TIMES, MINT, MoneyControl→emits **`INVESTING_IN`**, NDTV Profit→emits **`MARKETS_FEED`**, Reuters, PTI. **`BSE_FILINGS` produces 0 rows in `news_items`** despite being wired ⇒ effectively **[DORMANT]/[SCAFFOLDED]**. The `INVESTING_IN` / `MARKETS_FEED` source aliases are misleading (they are MoneyControl/NDTV under the hood) and `in.investing.com` body-fetches return 403. Plus two non-news adapters: **Polymarket** (`POLYMARKET` → `raw.sentiment.in`) and **Angel One MCX** (`ANGELONE_MCX` → `raw.commodity.in`, optional `raw.fno.in` gated by `ANGEL_ONE_ENABLE_FNO`).

**Analysis pipeline (5 stages, `analysis/stages/`):** Stage 1 = **local** zero-shot classifier (`deberta-v3-base-zeroshot-v2.0`, `RELEVANCE_BACKEND=local` default, threshold 0.35) — runs offline, fail-open. Stage 2 (extraction), Stage 3 (impact), Stage 5 (signals) = **Claude Haiku, hard-require `ANTHROPIC_API_KEY`, fail closed**. Stage 4 (propagation) = deterministic graph traversal over `company_relationships` via kb-service (currently traverses an **empty** table). With the key over-limit, Stages 2/3/5 error out (confirmed in `bharat-analysis` log).

**commodity-engine:** logs show heavy consumption — e.g. `ticks=55000 shocks=0` (per-commodity SILVER/CRUDEOIL/GOLD/NATURALGAS/COPPER/ALUMINIUM) on 2026-05-25, last cycle 2026-05-27 18:18 — but **`shocks=0` in every cycle ever logged**: the z-score shock detector has never fired, so no commodity `impact_report` has ever been produced.

**kb-monitor:** scheduled tasks run (morning results calendar → `result_events`; evening/late event sync; daily bhavcopy 16:30 IST; weekly Sat sweep; F&O participant-OI). NSE returns **403 Forbidden** to the VM, so `sync_symbol(...)` returns `parsed=0` and `event_sync` resolves `no_data` for ~all events. Only `participant_oi` ingestion succeeds.

---

## 4. Pipeline End-to-End Check

**Flow A — News → Analysis → Impact Report.** Ingestion writes `news_items` (today) → `raw.news.in` (Kafka offsets advancing, `analysis-workers` lag = 0) → Stage 1 (local) labels relevance → **Stages 2–5 fail on the Anthropic 400**. Result: `impact_reports` rows are still created, but **company_impacts/trade_signals have been empty since 2026-05-22**. **Verdict: flow runs end-to-end structurally, but produces no analytical output for 8 days.** Will likely self-heal at 2026-06-01 00:00 UTC when the API limit resets (unverifiable until then).

**Flow B — XBRL → Financials → Dossier.** `nse_filings` (44k, 99.4% parsed) → `financial_results` (43k, clean) is **healthy historically (≤2024-12-31)** but new quarters are not landing (NSE 403; `result_events` 642 pending / 6 synced). Dossier generation (`research-runner`, opt-in profile, never auto-started) has run **once, for RELIANCE**. **Verdict: backfill flow complete; live-refresh flow blocked; dossier flow is a manual PoC.**

**Flow C — Angel One → Commodity shock → Impact.** Ticks flowed into commodity-engine (≈50k/cycle through 2026-05-27) but **0 shocks detected, ever** → no output `impact_report`, no `analysed.commodity_shock.in` content of consequence. **Verdict: consuming but never producing.**

**Flow D — Angel One → F&O ticks → derivative_ticks.** Ran **once on 2026-05-14** (1,859 FUT ticks, 40 min) then the container exited and is not in the auto-start set. **Verdict: dormant.**

**Flow E — Sentiment (Polymarket).** ingestion → `raw.sentiment.in` → sentiment-worker → `analysed.sentiment.in` → API/WS → Sentiment tab. **No DB persistence** (Kafka/Redis only), so historical state can't be verified from the DB; last worker activity 2026-05-27. **Verdict: wired, live as of 05-27, but unauditable via DB.**

---

## 5. Frontend Functional State

7 tabs defined in `frontend/src/App.tsx:39-47` — **all 7 the README claims are present**: Live Feed, Sentiment, Companies, Impact Graph, Signals, Watchlist, Database.

| Tab | Renders? | Reflects real DB state? | Notes / evidence |
|---|---|---|---|
| Live Feed | Yes | Yes | `NewsFeed` → `/impact/feed`, drilldown `/company/{symbol}`. News is live. |
| Sentiment | Yes | Partially | `/sentiment/recent`, `/sentiment/themes`, WS `sentiment_snapshot`. Depends on sentiment-worker; shows empty-state when no events. |
| Companies | Yes | **Partially — profile fields blank** | `/companies`, `/companies/{symbol}` (+financials/linkages), `/dossier/{symbol}`. Financials/DCF populate for ~67%; sector/mcap/description are blank because those columns are 100% NULL. |
| Impact Graph | Yes | **Mostly empty** | 2D radial-tree (`react-force-graph-2d`), default trigger hardcoded `'BRENT'` (`ImpactGraph.tsx:190`); reads `/dossier/impact-graph/`. Because only the RELIANCE dossier exists, the graph shows an empty-state ("No dossiers cover this trigger… run research-runner") for almost all triggers. |
| Signals | Yes | **Stale/empty** | `TradeIdeas` reads `ImpactReport.trade_signals` from the WS feed — but **no signal has been generated since 2026-05-22**, so the tab/badge is effectively empty now. |
| Watchlist | Yes | Yes | Server-persisted via `GET/POST /watchlist/`. |
| Database | Yes | Yes | Generic browser `/db/tables` + `/db/{table}`; faithfully reflects the (sparse) DB. |

**Companies sector filter — double finding.** The brief asks whether the sector filter works given `sector_nse` is 100% NULL. **It is moot: there is no sector-filter control in the UI at all.** `api.ts:60` defines a `sector` param on `listCompanies`, but `CompaniesBrowser.tsx:48-51` only ever passes `q` and `has_dossier` — the `sector` argument is never sent. So even before reaching the all-NULL column, there is no way for a user to trigger a sector filter.

**Console errors / stale code:** no code-level TODO/FIXME or mock data found; only legitimate empty-state messages (e.g. `SentimentPanel.tsx:52`, `ImpactGraph.tsx:489-493`). The Prometheus scraper hits `/metrics` on api-gateway and kb-service and gets **404** (observability endpoint not implemented) — a real but low-severity gap.

---

## 6. Environment and Infrastructure

**`.env` keys (presence only — no values read):** present — `ANTHROPIC_API_KEY`, `DATABASE_URL`, `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS`, `KB_SERVICE_URL`, `EXTRACTION_MODEL`, `IMPACT_MODEL`, `SIGNALS_MODEL`, `RELEVANCE_THRESHOLD`, `LLM_LOG_FILE`, Polymarket volume thresholds, **all four Angel One creds** (`ANGEL_ONE_API_KEY/CLIENT_CODE/PASSWORD/TOTP_SECRET`), commodity tuning vars, `RESEARCH_MODEL/MAX_TURNS/ENABLE_WEB_SEARCH`. No key in `.env` has a blank value.
- **Drift vs `.env.example`:** `.env` is **missing** `RELEVANCE_BACKEND`, `RELEVANCE_MODEL`, `RELEVANCE_ANTHROPIC_MODEL`, `RELEVANCE_LOCAL_THRESHOLD`, `RELEVANCE_CACHE_TTL_SEC`, `RELEVANCE_TIMEOUT_SEC` (code falls back to defaults: local backend, threshold 0.35). `.env` keeps the legacy name `RELEVANCE_THRESHOLD` which the example replaced with `RELEVANCE_LOCAL_THRESHOLD`.

**API keys (presence/working):**
- **Anthropic key — present but NOT working.** Live 1-token probe to `api.anthropic.com/v1/messages`: **HTTP 400**, body `invalid_request_error: "You have reached your specified API usage limits. You will regain access on 2026-06-01 at 00:00 UTC."` The `bharat-analysis` container log shows the same 400 on its batch calls. This is the root cause of the dark analysis pipeline.
- **Angel One TOTP + creds — present** (all four vars set). F&O/commodity ticks did flow (05-14/05-25/05-27), so creds have worked; not re-tested live (would require a market-hours WS connect).

**GCP VM:**
- Instance `instance-20260508-044518`, project `bharat-terminal`, zone `asia-south1-c`, **status RUNNING**. Service account `deployment@bharat-terminal.iam.gserviceaccount.com`.
- **`scheduling`: `automaticRestart: true`, `preemptible: false`, `provisioningModel: STANDARD`, `onHostMaintenance: MIGRATE`, and `maxRunDuration: 18000s` with `instanceTerminationAction: STOP`.** ⇒ **the VM self-stops after 5 hours of uptime.** `automaticRestart` covers host failures/maintenance, *not* the scheduled STOP — so after each 5-hour window the instance halts until something starts it again. This is the structural cause of the data gaps and the "Up 4 minutes" containers.
- **Auto-start on boot:** `systemctl status bharat-terminal.service` → `enabled` + `active (exited)`, `ExecStart=docker compose up -d kb-service kb-monitor ingestion-worker analysis-worker sentiment-worker commodity-engine api-gateway frontend prometheus grafana`. **`fno-engine`, `research-runner`, `kb-backfill`, and `redpanda-console` are deliberately excluded** from the auto-start set — consistent with fno-engine being dormant.

**Cloudflare / `taperead.trade`:**
- `curl -I https://taperead.trade` → **HTTP/2 200**, `server: cloudflare`, `cf-ray: …-BOM` (Mumbai edge), `cf-cache-status: DYNAMIC`, `cache-control: public, max-age=300`, NEL/`report-to` enabled. DNS resolves to Cloudflare anycast (`2606:4700:…`) ⇒ **proxy (orange-cloud) is ON**, dynamic caching, NEL reporting on.
- `cloudflared` is **not installed on this VM** and there is no `~/.cloudflared` or tunnel config in the repo, so **how origin traffic reaches the VM (named tunnel run elsewhere vs. direct origin pull to the VM's IP) is *unable to determine from this host*** — would require the Cloudflare dashboard / API token, or finding the tunnel on whatever host runs `cloudflared`.

---

## 7. Documented-but-Missing Items (README claims with no backing)

- **"~5,000 NSE/BSE companies"** → 2,367, **NSE-only**. (`companies`: NSE|2367) **[DOCUMENTED, overstated]**
- **"DCF models, analyst consensus"** → consensus columns 100% NULL; DCF computed once (2026-05-10) and never refreshed. **[DOCUMENTED → DORMANT/SCAFFOLDED]**
- **"pgvector entity resolution / HNSW embeddings"** → `companies.embedding` 100% NULL; no embeddings exist. **[DOCUMENTED → SCAFFOLDED]**
- **"8 Indian financial sources" / explicit BSE, MoneyControl, NDTV Profit list** → 7 sources produce rows; BSE produces 0; MoneyControl/NDTV appear only under aliases `INVESTING_IN`/`MARKETS_FEED`. **[DOCUMENTED, divergent]**
- **"Impact graph — news about RIL ripples to Jio, RRVL, IOC" + "kb-seed seeds RELIANCE with 7 relationships"** → `company_relationships` = 0 rows. The seeded relationships do not exist; Stage-4 propagation has nothing to traverse. **[DOCUMENTED → SCAFFOLDED, false]**
- **"≤5s end-to-end P95" SLA table** → unverifiable while Stages 2–5 are down; `processing_latency_ms` exists on `impact_reports` but reflects Stage-1-only paths now. **[DOCUMENTED, unverifiable]**
- **"Redpanda Console :8080"** → service defined in compose but **not running** (excluded from systemd start). **[DOCUMENTED → not running]**
- **`/news/feed?sector=BANKING`, `/company/search`** etc. — endpoints exist, but a `sector` filter is meaningless given 100%-NULL `sector_nse` and no UI control. **[DOCUMENTED, non-functional]**

---

## 8. Unclaimed Items (in code/DB, absent from the README)

The README describes a *first-generation* "news → impact" product. The repo and DB contain a substantially larger *second-generation* system ("Taperead") that the README never mentions:

- **Sentiment / Polymarket subsystem** — `bharat_terminal/sentiment/`, Polymarket adapter, `raw.sentiment.in`/`analysed.sentiment.in`, Sentiment tab. **[UNCLAIMED]**
- **Commodity engine + Angel One MCX** — `bharat_terminal/commodity/`, z-score shock detector, `raw.commodity.in`. **[UNCLAIMED]**
- **F&O engine + derivatives** — `bharat_terminal/fno_engine/`, `derivative_ticks`, `derivatives_eod`, `participant_oi`. **[UNCLAIMED]**
- **Research / dossier agent** — `bharat_terminal/research/`, `company_dossiers`, `dossier_sensitivities`, `dossier_linkages`, `trigger_betas`, `fundamental_exposures`. **[UNCLAIMED]**
- **Macro-event propagation graph** — `triggers`, `macro_events`, `event_sector_edges`, `event_transmissions`, `event_degrees`, `sector_leaders`; the `/dossier/impact-graph/` endpoint and the entire Impact-Graph tab are built on this, not on `company_relationships`. **[UNCLAIMED]**
- **Database explorer** — `/db/tables`, `/db/{table}` endpoints + Database tab. **[UNCLAIMED]**
- **Tables not in any narrative:** `event_degrees`, `company_relationships` (empty), `universe_membership` (empty). **[UNCLAIMED]**

---

## 9. [FOUNDER ANNOTATION REQUIRED]

> Placeholders for context only the founder has. The audit measured *what*; these ask *why*.

1. **Anthropic budget cap** — Was the monthly usage limit hit intentionally (cost cap) or by surprise? Should Stage-2–5 degrade gracefully (queue + retry after reset) instead of failing closed? *(Limit resets 2026-06-01.)*
2. **The 5-hour VM `maxRunDuration` STOP** — Is the self-terminating instance a deliberate cost control, a leftover from a spot/experiment config, or unknown? Nothing auto-restarts it after the scheduled STOP — is an external scheduler (Cloud Scheduler / cron) supposed to restart it?
3. **`sector_nse` / `industry_nse` / `mcap_cr` / `description_200w` / `embedding` 100% NULL** — Was the company-master enrichment (sector, mcap, embeddings) deprioritised, never built, or is the source feed broken? The embedding-based entity resolution is advertised but has no data.
4. **`company_relationships` empty** — Is the RIL→Jio/RRVL/IOC relationship seed a known-broken/never-run dependency? The README implies kb-seed populates 7 relationships.
5. **`financial_results` stops at 2024-12-31** — Is the post-2024 quarter gap due to the NSE 403 block, a parser issue on 2025 XBRL, or a deliberate backfill cutoff?
6. **NSE 403 Forbidden** — Is the session-cookie/headers path for NSE a known-broken dependency? It blocks live filings, prices, and `result_events` sync.
7. **commodity-engine `shocks=0` always** — Are the z/%/baseline thresholds mis-tuned, or has there genuinely been no qualifying shock? Has it *ever* produced a shock impact report?
8. **fno-engine deprioritised?** — Excluded from systemd auto-start and dormant since 05-14. Intentional pause, or forgotten?
9. **Sentiment output not persisted** — Is the Kafka/Redis-only design intentional (no historical sentiment table by design)?
10. **README staleness** — Is the README intentionally a "public face" describing only gen-1, with gen-2 (sentiment/commodity/F&O/research) intentionally undocumented?

---

## 10. Recommended Priority Order for Fixes

Ordered **data integrity > functionality > polish**, with one-line rationale.

**P0 — Infrastructure / availability (everything else is downstream of these):**
1. **Remove or externally-counter the 5-hour `maxRunDuration` STOP** (or add a Cloud Scheduler auto-start). *The VM halting every 5 h is the single largest cause of stale/gappy data.*
2. **Resolve the Anthropic usage cap** (raise limit, add a second key, or queue+retry after 2026-06-01). *Stages 2–5 — the core product — are dark until then.*
3. **Fix NSE 403 access** (rotate session cookies / proper headers / proxy). *Unblocks live filings, prices, and `result_events` sync simultaneously.*

**P1 — Data integrity (silent wrong/empty data is worse than visibly missing):**
4. **Make analysis fail *loudly and recoverably*** when the LLM is unavailable — don't write relevance-only stubs that masquerade as processed reports. *5,155 reports include 8 days of empty results indistinguishable from "nothing happened."*
5. **Populate `company_relationships`** (or repoint Stage-4 to the dossier-linkage graph that actually has data). *Stage-4 propagation and `/graph/{symbol}` are no-ops today.*
6. **Backfill `companies` profile fields** — `sector_nse`, `industry_nse`, `mcap_cr`, `description_200w`, `embedding`. *These gate the Companies tab, any sector logic, and the advertised entity resolution.*
7. **Close the `financial_results` post-2024 gap** and **clear the `sync_log` zombie row**; route live syncs through `sync_log` so telemetry is truthful.
8. **Scrub future-dated `news_items`** (20 NSE rows with `timestamp_utc` in the future).

**P2 — Functionality:**
9. **Re-tune or validate the commodity shock detector** (`shocks=0` forever suggests mis-set thresholds) and **re-enable/auto-start fno-engine** if derivatives are in scope.
10. **Run the dossier agent beyond RELIANCE** (at least the nifty50 tier) so the Impact-Graph tab is non-empty.
11. **Add the Companies sector-filter UI control** (only meaningful after #6).

**P3 — Polish / observability:**
12. **Implement `/metrics`** on api-gateway and kb-service (Prometheus currently 404s).
13. **Rename the misleading source aliases** `INVESTING_IN`/`MARKETS_FEED` → MoneyControl/NDTV, and either fix or retire the BSE adapter (0 rows) and the `in.investing.com` 403 body-fetch.
14. **Rewrite `README.md`** to match reality (company count, source list, and the existence of the sentiment/commodity/F&O/research subsystems).

---

*End of audit. All figures and statuses above are backed by the queries, log lines, file paths, and commands cited inline and listed in §0. Items I could not verify (Cloudflare origin path; SLA latency while the LLM is down; live Angel-One WS) are explicitly marked "unable to determine."*
