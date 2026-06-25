# Bharat Terminal — System State Audit (2026-06-15)

> Evidence-based snapshot of what is built, what is running, and what is
> merely documented. Supersedes `BHARAT_TERMINAL_AUDIT_2026-05-31.md` as the
> current source of truth.
>
> **Audit executed:** 2026-06-15, DB figures measured 05:51–05:55 UTC
> (`SELECT now()` → `2026-06-15 05:54:39+00`). Read-only throughout; no
> production data, config, or service was modified.

**What this audit covers.** The 05-31 audit's headline finding was a *dark*
LLM pipeline (Anthropic over quota) and a set of empty/dormant tables. Since
then there are **two commits** and **a batch of uncommitted-but-deployed
changes** that reverse most of those findings. Each material change is flagged
inline as *"diverges from 05-31."*

**Provenance of the changes audited:**
- `52b4f5f` (2026-06-10) — Gemini dossier migration + NSE DB-grounding layer + MCP server.
- `ead3dc4` (2026-06-11) — wire **all** analysis + research LLM stages to Gemini (Claude is not available on Vertex).
- **Uncommitted working tree (7 files) — NOT in git, but VERIFIED RUNNING in
  the deployed `bharat-analysis` container** (see §0a). These are treated as
  *live* per the verification, and flagged *[UNCOMMITTED]* wherever they appear.

---

## 0. Audit method

**Queries (read-only, via `docker exec bharat-postgres psql -U bharat -d bharat_terminal`):**
exact `n_live_tup`/`count(*)` per table; `min`/`max` of the primary time column
per table; per-column NULL% for `companies`; `company_relationships` by
`source_of_truth`; `dossier_sensitivities` rationale markers (calibration
evidence); `sync_log` open rows.

**Containers/logs:** `docker ps -a`; `docker inspect` image build time + env on
`bharat-analysis`; `docker logs` for analysis (LLM provider, gates, SLA),
commodity (shock counts), kb-monitor (NSE 403 status). In-container file checks
to confirm the uncommitted code is actually deployed.

**Infra/edge:** `curl` health on api (:8000) + kb (:8001); `curl -I https://taperead.trade`.

### 0a. The uncommitted-but-deployed nuance (read this first)

Seven files are modified/created in the working tree but **not committed to
git**. They are nonetheless **running in production** — verified by reading them
*inside* the `bharat-analysis` container (image built 2026-06-12 05:53 UTC) and
by observing their log output live:

| File | State | Verified live by |
|---|---|---|
| `analysis/signal_gates.py` | **new** | `test -f` inside container → PRESENT; gate log lines firing |
| `analysis/batch_processor.py` | modified | `grep BATCH_ESCALATE_MAX` inside container → present |
| `analysis/stages/stage2_extraction.py` | modified | KB name-resolution path |
| `analysis/stages/stage4_propagation.py` | modified | weight-based decay |
| `analysis/stages/stage5_signals.py` | modified | imports `signal_gates`; gate logs |
| `ingestion/adapters/nse_filings.py` | modified | IST→UTC fix (future-dated rows fell 28→4) |
| `research/calibrate_weights.py` | **new** | applied — see calibration markers in `dossier_sensitivities` |

**Risk flag:** production is running code that exists only on this VM's disk.
A clean rebuild from `git HEAD` would *revert* the signal-quality gates, batch
escalation, weight-calibrated propagation, the filing-timestamp fix, and the
calibrator. **These should be committed.**

---

## 1. Executive summary

**The core product is lit again — on Gemini/Vertex, not Anthropic.** The
05-31 audit's headline ("AI pipeline dark, Anthropic over quota until
2026-06-01") is obsolete. Claude is unavailable on Vertex, so every LLM stage
was rerouted to Gemini (commit `ead3dc4`). **Verified live:** the running
`bharat-analysis` container has `LLM_PROVIDER=vertex`, and live logs show
`stage3_impact` on `gemini-2.5-flash` and `stage5_signals` on
`gemini-3.1-pro-preview`, both `POST aiplatform.googleapis.com … 200`.
`impact_reports` carry real impacts/signals again **from 2026-06-11** (06-09 and
06-10 were still 0; 06-15 so far: 95 reports with impacts, 12 with signals).

**Five of the 05-31 audit's headline problems are now fixed:**
1. **Vertex migration deployed** (was "source only, not deployed").
2. **`company_relationships` populated** 0 → **421 rows** (`price_calibrated`
   160, `nse_shp` 141, `dossier_agent` 120) — Stage-4 propagation now has a real
   graph, weighted by calibrated edges.
3. **Dossiers** 1 (RELIANCE PoC) → **54 companies**.
4. **NSE 403 resolved** — `www.nseindia.com` now returns 200 to the VM (was the
   persistent 403 blocker).
5. **Commodity shock detector now fires** — `shocks=1` in 399 of the last
   logged cycles (was `shocks=0` in *every* cycle ever).

**Net-new since 05-31:** an NSE-only grounding layer (`shareholding_patterns`
216, `corporate_actions` 938), a streamable-HTTP MCP server (:8002, opt-in), and
a data-backed **dossier weight calibrator** that replaces LLM-guessed numbers
with price-panel correlations / realized betas / measured cost pass-through
(46 sensitivities now `Realized beta`, 52 `Fundamentals-blended`).

**What is still broken / unchanged from 05-31:**
- `companies.industry_nse`, `description_200w`, `revenue_segments`, `embedding`
  remain **100% NULL** — the advertised pgvector entity-resolution still has no
  data; `sector_nse` still 68.3% NULL.
- `derivative_ticks` still frozen at 2026-05-14; `fno-engine` exited 4 weeks ago.
- Stage-1 local classifier **still breaching its 800 ms SLA** (saw 1370 ms today).
- `sync_log` zombies (ids 6, 13, 14) **still open** (`completed_at` NULL).
- Scaffolded-empty tables unchanged: `derivatives_eod`, `trigger_betas`,
  `fundamental_exposures`, `sector_leaders`, `universe_membership` (all 0).

**Freshness caveat.** Containers show `Up 3 days` (deployed 2026-06-12). Price/
filing/participant-OI data is current through **2026-06-11** (Thu); Fri 06-12's
bhavcopy was not captured before the weekend gap, and today (Mon 06-15) the
11:00 UTC price sync has not run yet. Mild, schedule-explainable staleness — not
a freeze. News ingestion and the analysis pipeline are live to **now**.

---

## 2. Database state (per table, classified)

Postgres 15 + pgvector. `alembic` head = **009** (unchanged). Counts =
`n_live_tup` (approx) unless noted.

| Table | Rows | Earliest → Latest | Classification (vs 05-31) |
|---|---:|---|---|
| `companies` | 2,367 | refresh-stamped | **[OPERATIONAL]** rows; profile fields still **partial** |
| `financial_results` | 54,073 | 2017-09-30 → 2026-03-31 | **[OPERATIONAL]** current |
| `nse_filings` | 54,845 | 2018-05-21 → **2026-06-11** | **[OPERATIONAL]** live |
| `price_points` | 576,920 | 2025-05-09 → **2026-06-11** | **[OPERATIONAL]** (see freshness caveat) |
| `impact_reports` | 13,785 | — | **[OPERATIONAL]** ingest+Stage1–5 **lit again** (diverges) |
| `news_items` | 12,244 | 2026-05-10 → **2026-06-15** | **[OPERATIONAL]**; future-dated rows 28→**4** (diverges, fix landed) |
| `result_events` | 1,742 | synced to 06-11 | **[OPERATIONAL]** |
| `participant_oi` | 408 | 2026-05-13 → 2026-06-11 | **[OPERATIONAL]** |
| `company_relationships` | **421** | — | **[OPERATIONAL]** (diverges — was 0) |
| `company_dossiers` | **54** | 2026-06-04 → 2026-06-10 | **[OPERATIONAL]** (diverges — was 1) |
| `dossier_sensitivities` | 213 | calibrated | **[OPERATIONAL]** (diverges — calibrated) |
| `dossier_linkages` | 264 | calibrated (55 distinct strengths) | **[OPERATIONAL]** (diverges) |
| `shareholding_patterns` | **216** | 54 symbols | **[OPERATIONAL]** (diverges — was 0) |
| `corporate_actions` | **938** | ex_date 2005 → 2026-07-31, 54 symbols | **[OPERATIONAL]** (diverges — was 0) |
| `derivative_ticks` | 1,859 | **2026-05-14 only** | **[DORMANT]** (unchanged) |
| `macro_events` / `event_sector_edges` / `event_transmissions` / `event_degrees` / `triggers` | 9 / 43 / 7 / 27 / 36 | seed 2026-05-11 | **[SCAFFOLDED]** seed (unchanged) |
| `sync_log` | 31 | → 2026-06-12 | **[OPERATIONAL]**; **3 zombie rows (6,13,14)** still open |
| `derivatives_eod`, `trigger_betas`, `fundamental_exposures`, `sector_leaders`, `universe_membership` | 0 | — | **[SCAFFOLDED]** (unchanged) |

### 2.1 `companies` profile NULL% (n = 2,367)

| Column | NULL% | vs 05-31 |
|---|---:|---|
| `mcap_cr` | 7.7 | unchanged |
| `sector_nse` | 68.3 | unchanged |
| `industry_nse` | **100.0** | unchanged |
| `description_200w` | **100.0** | unchanged |
| `revenue_segments` | **100.0** | unchanged |
| `embedding` | **100.0** | unchanged |

Profile enrichment **did not advance** since 05-31. pgvector entity resolution
still has no backing data.

### 2.2 `company_relationships` (421 — the marquee fix)

By `source_of_truth`:
- `nse_shp` = **141** — real promoter/holder ownership stakes from SEBI
  shareholding XBRL (`nse_ownership_sync`, Layer 2; the calibrator **never
  touches these** — they are filing facts).
- `dossier_agent` = **120** — LLM-derived linkage topology.
- `price_calibrated` = **160** — `dossier_agent` edges whose weights were
  replaced by price-panel correlations by `calibrate_weights.py`.

Stage-4 propagation now multiplies impact magnitude by these calibrated edge
weights (clamped 0.1–0.9), pruning edges < 0.15, instead of the old flat
0.6/0.35 hop constants. *[UNCOMMITTED — `stage4_propagation.py`]*

### 2.3 Calibration evidence (`dossier_sensitivities`, n = 213)

- 46 rows rationale `Realized beta vs equal-weight market factor …` (market
  betas for `FII_FLOW`/`NIFTY50`/`NIFTY_BANK`).
- 52 rows rationale `Fundamentals-blended: cost_share=… pass_through=…`
  (`INPUT_COST` channel, blended with measured per-company cost pass-through).
- Pre-calibration backups exist: `exports/backup_{linkage_strengths,sensitivities}_pre_calibration.csv` (2026-06-11).

---

## 3. Service / pipeline state

`docker ps -a`: 14 application/infra containers **Up 3 days** (deployed
2026-06-12). `bharat-fno-engine` **Exited (0) 4 weeks ago**;
`bharat-kb-backfill` Exited 5 weeks ago (opt-in). api/kb `/health` → **200**;
`https://taperead.trade` → **HTTP/2 200**.

| Service | Status | Writing data? | Classification (vs 05-31) |
|---|---|---|---|
| ingestion-worker | Up | `news_items` to now | **[OPERATIONAL]** |
| analysis-worker | Up | impacts+signals (Gemini) | **[OPERATIONAL] Stage 1–5 lit** (diverges) |
| sentiment-worker | Up | Kafka/Redis only | **[OPERATIONAL]** (no DB persistence) |
| commodity-engine | Up | shock detector **now fires** | **[OPERATIONAL]** (diverges — shocks>0) |
| kb-monitor | Up | results/OI/shareholding/corp-actions | **[OPERATIONAL]**; **NSE 403 resolved** (diverges) |
| kb-service / api-gateway / frontend | Up (healthy) | reads | **[OPERATIONAL]** |
| mcp-server (:8002) | opt-in profile | grounding tools | **[SCAFFOLDED]** (new; not in auto-start) |
| fno-engine | **Exited 4wks** | last 2026-05-14 | **[DORMANT]** (unchanged) |

**LLM routing — now genuinely on Vertex/Gemini.** Running container env:
`LLM_PROVIDER=vertex`, `VERTEX_PROJECT=bharat-terminal`,
`VERTEX_{CLAUDE,GEMINI}_REGION=global`, `EXTRACTION_MODEL=gemini-2.5-flash`,
`SIGNALS_MODEL=gemini-3.1-pro-preview`. Live log confirms both models hitting
`aiplatform.googleapis.com … 200`. Stage 2/3 = `gemini-2.5-flash`
(flash-lite was verified too weak — `MALFORMED_FUNCTION_CALL` on the nested
extraction schema), Stage 5 + research agent = `gemini-3.1-pro-preview`.

> **Config nuance (resolves the 05-31 "config drift" finding):** `.env` on disk
> **still lacks** `LLM_PROVIDER`, `VERTEX_*`, `DOSSIER_MODEL`, `GROUNDING_MODEL`,
> `MCP_SERVER_PORT`. Vertex is live anyway because these now come from
> **`docker-compose` `x-common-env` defaults**, not `.env`. So the 05-31
> recommendation to "add `LLM_PROVIDER` to `.env`" is moot for runtime — but
> `.env`/`.env.example` drift remains a hygiene gap.

**Signal-quality gates (new, live).** *[UNCOMMITTED — `signal_gates.py`]* Three
gates, all firing in today's logs:
1. **Derivative-headline filter** — skips recaps/gainers-losers/tips/“stocks in
   focus”/“rally up to N%”. Live: `[stage5] skipped derivative headline: …`.
2. **Stale-article filter** — drops articles > 12 h old at processing time.
   Live: `[stage5] skipped stale article (…)`.
3. **Symbol resolution + Redis dedup** — drops unroutable tickers; 48 h dedup
   per (symbol, direction). Live: `[gates] dropped signal: dup VEDL/short`.

**Batch triage no longer fabricates signals.** *[UNCOMMITTED —
`batch_processor.py`]* The off-hours batch path used to emit template signals
from 300-char bodies with unvalidated symbols. It now emits **no** signals and
instead escalates ≤5 high-conviction, non-derivative items per batch to the
full per-item pipeline (where the gates apply). Live: `Batch complete: … (1
relevant, 0 escalated)`.

**commodity-engine.** Shock detector **now fires** (`shocks=1` × 399 cycles vs
`shocks=0` × 98) — a clean divergence from the 05-31 "never fired." **Not
verified:** whether a fired shock produces a downstream `impact_report` (the
detector firing ≠ an impact written). Flag for follow-up.

**Stage-1 local classifier** still breaches its 800 ms SLA (saw 1370 ms today).
Unchanged regression.

---

## 4. Documented-but-missing / still-overstated (unchanged from 05-31)

- pgvector / HNSW embeddings → `companies.embedding` 100% NULL. **[SCAFFOLDED]**
- company descriptions / revenue segments / industry → 100% NULL. **[SCAFFOLDED]**
- "~5,000 NSE/BSE companies" → 2,367, NSE-only, no BSE. **[OVERSTATED]**
- BSE_FILINGS adapter → 0 rows. **[DORMANT]**
- F&O / `derivative_ticks` → frozen 2026-05-14; fno-engine dormant. **[DORMANT]**
- Scaffolded-empty: `derivatives_eod`, `trigger_betas`, `fundamental_exposures`,
  `sector_leaders`, `universe_membership`. **[SCAFFOLDED]**

---

## 5. Open questions for the founder

1. **Commit the 7 deployed-but-uncommitted files?** Production depends on code
   not in git; a rebuild from HEAD would silently regress signal gates,
   propagation calibration, and the filing-timestamp fix.
2. **Commodity shocks now fire — do they produce impact_reports?** Detector
   firing is verified; the downstream write is not. Worth confirming.
3. **`sync_log` zombies (6, 13, 14)** still open — crash mid-sync, or a path
   that never closes the row?
4. **Profile enrichment** (`industry_nse`/`description_200w`/`revenue_segments`/
   `embedding`) — deprioritised, or blocked on a missing source feed? Entity
   resolution remains data-less.
5. **fno-engine** still excluded from auto-start and dormant since 05-14 —
   intentional pause?
6. **Stage-1 local SLA** breaches (1.6–2.7 s historically, 1.37 s today) — the
   one always-on stage; tune the model, the SLA, or the VM?
7. **`.env` ↔ `.env.example` ↔ compose** — three-way config drift; runtime now
   leans on compose defaults. Reconcile?

---

*End of audit. All figures backed by the queries, container inspections, and log
lines cited inline (measured 2026-06-15 05:51–05:55 UTC). Items not verified are
marked explicitly: commodity-shock → impact_report linkage; live Vertex/ADC IAM
grant; runtime SPA console errors; Angel One WS (market-hours only). Net change
since 2026-05-31 is materially positive — the core LLM pipeline is lit (Gemini),
the relationship graph and dossiers are populated, NSE 403 is gone, and the
commodity detector fires — offset by a governance risk (production running
uncommitted code) and persistent profile-enrichment / SLA / dormancy gaps.*
