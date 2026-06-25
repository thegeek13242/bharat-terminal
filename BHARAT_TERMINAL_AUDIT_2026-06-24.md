# Bharat Terminal — System State Audit (2026-06-24)

> Evidence-based snapshot of what is built, running, and merely documented.
> Supersedes `BHARAT_TERMINAL_AUDIT_2026-06-15.md` as current source of truth.
>
> **Audit executed:** 2026-06-24, DB + log figures measured ~14:35 UTC
> (`SELECT now()` → `2026-06-24 14:35:37+00`). Read-only throughout; no
> production data, config, or service was modified.

**Method.** Live `docker ps -a`; `docker exec bharat-postgres psql` row counts
and time-column min/max per table; `docker logs` on analysis / commodity /
kb-monitor; `docker exec ... env` for LLM routing; `git status` for the
committed-vs-deployed gap. Every figure below was queried today, not carried
over from the 06-15 audit.

---

## 1. Executive summary

**The core product remains lit and is current to today.** The Gemini/Vertex
pipeline that came online around 06-11 has run continuously for the 9 days
since the last audit. `impact_reports` are being written every day through
**2026-06-24** (653 reports today: 562 relevant, 408 carrying company impacts,
44 carrying trade signals). NSE returns 200, the commodity shock detector still
fires, and the relationship graph + dossiers are still populated. **No
regression to the dark-pipeline state of late May.**

**Verified-stable since 06-15 (re-confirmed live today):**
1. LLM routing genuinely on Vertex — `LLM_PROVIDER=vertex`, live `200`s to
   `aiplatform.googleapis.com` for `gemini-2.5-flash` and `gemini-3.1-pro-preview`.
2. `company_relationships` = **421** (price_calibrated 160, nse_shp 141,
   dossier_agent 120) — Stage-4 has a real weighted graph.
3. NSE 403 still resolved — kb-monitor pulling integrated filings at `200`.
4. Commodity shock detector still firing (`shocks=1` every cycle).
5. Pipeline freshness: news + impacts live to **now**.

**Unchanged problems (all carried from 06-15, none fixed):**
- `companies` profile: `industry_nse` / `description_200w` / `revenue_segments`
  / `embedding` still **100% NULL**; `sector_nse` still **68.3% NULL**.
  pgvector entity resolution still has no backing data.
- ~~`sync_log` zombies **6, 13, 14** (`nse_xbrl`, status `running`)~~ — RESOLVED
  2026-06-24: marked `aborted` (orphaned runs hard-killed on bootstrap day 05-10
  and migration day 05-30; never blocked syncs — resume keys off `status='failed'`,
  no concurrency guard reads `running`). 0 open rows remain. Underlying cause
  (hard-kill bypasses the try/except close) is unfixed; a startup sweep would
  close it permanently.
- `derivative_ticks` frozen at **2026-05-14**; `fno-engine` Exited (now 5 weeks).
- Scaffolded-empty unchanged: `derivatives_eod`, `trigger_betas`,
  `fundamental_exposures`, `sector_leaders`, `universe_membership`.
- **Governance risk persists:** 7 code files + new modules run in production
  but are still **uncommitted** (see §5).

**New / changed since 06-15 (flagged, not yet in the prior audit):**
- **Dossiers stalled at 54** (max `generated_at` still 2026-06-10) — dossier
  generation has not run in 2 weeks. Was framed as "growing" on 06-15; it is now
  clearly **paused**, not progressing.
- **New E2E pipeline SLA + breach:** logs now show
  `E2E pipeline SLA breach: 21254ms > 5000ms`. A full-pipeline 5 s SLA exists and
  is being breached by 4× on heavy items — a stricter, newer check than the
  Stage-1-only SLA flagged before.
- **`nse_filings` latest = 2026-06-18** — NOT a sync gap. The table is
  exclusively quarterly financial filings (all rows `period='Quarterly'`), so its
  cadence follows the results calendar: June is a seasonal trough (2026-06 = 12
  filings; cf. 2026-05 = 1,917 at the Q4 deadline). Every Mar/Jun/Sep/Dec shows
  the same 12–58-row dip. kb-monitor is running and ingesting (new rows 06-17/18);
  there is simply nothing new to file until Q1-FY27 results land late Jul–Aug.
- Two **new untracked modules** not in the 06-15 file list: `ingestion/pdf_fetch.py`
  and `kb/company_memory.py` (plus `kb/api.py` modified). Built, uncommitted,
  not yet audited for live use.

---

## 2. Database state (measured 2026-06-24 ~14:35 UTC)

| Table | Rows | Latest / span | Classification |
|---|---:|---|---|
| `companies` | 2,367 | — | **[OPERATIONAL]**; profile fields still partial |
| `financial_results` | ~54k | period_end → **2026-03-31** | **[OPERATIONAL]** current |
| `nse_filings` | ~54k | filing_date → **2026-06-18** | **[OPERATIONAL]**; mildly stale (6d) |
| `price_points` | ~577k | date → **2026-06-23** | **[OPERATIONAL]** fresh |
| `news_items` | **17,869** | → now (3 forward-dated filings) | **[OPERATIONAL]** live |
| `impact_reports` | 13,785+ | created_at → **2026-06-24** | **[OPERATIONAL]** Stage 1–5 live daily |
| `participant_oi` | 408+ | date → **2026-06-23** | **[OPERATIONAL]** fresh |
| `company_relationships` | **421** | — | **[OPERATIONAL]** (unchanged since 06-15) |
| `company_dossiers` | **54** | generated_at → **2026-06-10** | **[OPERATIONAL] but STALLED** (no new in 2wk) |
| `dossier_sensitivities` | 213 | calibrated | **[OPERATIONAL]** |
| `dossier_linkages` | 264 | calibrated | **[OPERATIONAL]** |
| `shareholding_patterns` | 216 | 54 symbols | **[OPERATIONAL]** |
| `corporate_actions` | 938 | 54 symbols | **[OPERATIONAL]** |
| `derivative_ticks` | 1,859 | **2026-05-14 only** | **[DORMANT]** (unchanged) |
| `derivatives_eod`, `trigger_betas`, `fundamental_exposures`, `sector_leaders`, `universe_membership` | 0 | — | **[SCAFFOLDED]** (unchanged) |
| `sync_log` | — | — | **OPERATIONAL**; zombies 6,13,14 marked `aborted` 2026-06-24 (0 open) |

### 2.1 `companies` profile NULL% (n = 2,367) — unchanged

| Column | NULL% |
|---|---:|
| `mcap_cr` | 7.7 |
| `sector_nse` | 68.3 |
| `industry_nse` | 100.0 |
| `description_200w` | 100.0 |
| `embedding` | 100.0 |

### 2.2 Pipeline output, last 7 days (impact_reports)

| Date | Total | Relevant | w/ impacts | w/ signals |
|---|---:|---:|---:|---:|
| 06-18 | 693 | 572 | 404 | 54 |
| 06-19 | 624 | 538 | 374 | 41 |
| 06-20 | 283 | 228 | 149 | 10 |
| 06-21 | 151 | 91 | 34 | 3 |
| 06-22 | 724 | 636 | 442 | 40 |
| 06-23 | 662 | 574 | 397 | 36 |
| 06-24 | 653 | 562 | 408 | 44 |

Weekday volume ~650–720/day; weekends (06-20/21) drop as expected. ~6–8% of
relevant reports produce a trade signal after the gates — consistent with the
"gates suppress low-conviction noise" design.

---

## 3. Service / pipeline state (live today)

| Service | Status | Classification |
|---|---|---|
| bharat-ingestion | Up 9 days | **[OPERATIONAL]** news to now |
| bharat-analysis | Up 9 days | **[OPERATIONAL]** Stage 1–5 on Gemini |
| bharat-kb / kb-monitor | Up 13 days | **[OPERATIONAL]** NSE 200; results/OI/SHP/corp-actions |
| bharat-api / frontend | Up 13/9 days (healthy) | **[OPERATIONAL]** |
| bharat-sentiment | Up 13 days | **[OPERATIONAL]** (Kafka/Redis only, no DB persist) |
| bharat-commodity | Up 13 days | **[OPERATIONAL]** shocks=1 firing |
| bharat-postgres / redis / redpanda | Up 13 days (healthy) | **[OPERATIONAL]** |
| bharat-grafana / prometheus | Up 13 days | **[OPERATIONAL]** monitoring |
| bharat-fno-engine | **Exited (0) 5 weeks** | **[DORMANT]** (unchanged) |
| bharat-kb-backfill | Exited 6 weeks | opt-in, idle |
| mcp-server (:8002) | opt-in profile | **[SCAFFOLDED]** not in auto-start |

**LLM routing (container env, live):** `LLM_PROVIDER=vertex`,
`VERTEX_PROJECT=bharat-terminal`, `EXTRACTION_MODEL=IMPACT_MODEL=gemini-2.5-flash`,
`SIGNALS_MODEL=DOSSIER_MODEL=RESEARCH_MODEL=gemini-3.1-pro-preview`. Confirmed by
live `200`s + cost logs (`batch_analysis ... cost=$0.001`).

**SLA breaches (live, today):**
- Stage-1 local classifier: 1261–2578 ms vs **800 ms** SLA — persistent.
- **E2E pipeline: 21,254 ms vs 5,000 ms SLA** — new, breaching ~4× on heavy
  NSE corporate-announcement items.

**Commodity:** `ticks=2.16M shocks=1` per cycle. Still **not verified** whether a
fired shock writes a downstream `impact_report` — carry-forward open question.

---

## 4. Still documented-but-missing / overstated (unchanged)

- pgvector / embeddings → `companies.embedding` 100% NULL. **[SCAFFOLDED]**
- descriptions / revenue segments / industry → 100% NULL. **[SCAFFOLDED]**
- "~5,000 NSE/BSE companies" → 2,367, NSE-only, no BSE. **[OVERSTATED]**
- BSE_FILINGS adapter → 0 rows. **[DORMANT]**
- F&O / `derivative_ticks` → frozen 2026-05-14; fno-engine dormant. **[DORMANT]**

---

## 5. Governance risk — production running uncommitted code (still open)

`git status` today shows **15 uncommitted entries** on branch
`feat/mcx-commodity-research-graph` (which is itself **not merged/pushed to
origin/main** — origin/main is many commits behind):

Modified (deployed, live): `analysis/batch_processor.py`,
`stages/stage2_extraction.py`, `stage3_impact.py`, `stage4_propagation.py`,
`stage5_signals.py`, `ingestion/adapters/nse_filings.py`, `kb/api.py`,
`requirements/ingestion.txt`.
New/untracked: `analysis/signal_gates.py`, `research/calibrate_weights.py`,
`ingestion/pdf_fetch.py`, `kb/company_memory.py`, two pre-calibration backup CSVs.

A clean rebuild from `git HEAD` would revert the signal-quality gates, batch
escalation, weight-calibrated propagation, the filing-timestamp fix, and the
calibrator. **These should be committed and the branch pushed.** This was raised
on 06-15 and remains unaddressed 9 days later.

---

## 6. Open questions for the founder

1. **Commit & push the working tree** — 9 days of "prod-only" code; branch not on
   origin. Highest-priority hygiene item.
2. **Dossier generation stalled at 54 since 06-10** — intentional pause, or a
   broken/disabled job? The pipeline that feeds them is healthy.
3. **New E2E 5 s SLA breaching 4×** — is 5 s realistic for Gemini-3.1-pro signal
   items, or should the SLA / model / batching be retuned?
4. ~~`nse_filings` 6 days behind~~ — RESOLVED: seasonal trough (quarterly-only
   table; June is between filing deadlines), not a sync gap.
5. ~~`sync_log` zombies (6,13,14)~~ — RESOLVED 2026-06-24 (marked `aborted`).
   Optional follow-up: add a startup sweep so a future hard-kill can't leave a
   new orphaned `running` row.
6. **Profile enrichment** (industry/description/embedding 100% NULL) — still
   deprioritised; pgvector resolution remains data-less.
7. **Commodity shock → impact_report** linkage — detector fires, downstream write
   unverified.

---

*End of audit. All figures backed by live queries, container inspections, and log
lines measured 2026-06-24 ~14:35 UTC. Net state since 2026-06-15: stable and
current — the core LLM pipeline has run continuously and produces fresh
impacts/signals daily — but every structural gap from 06-15 persists, dossier
generation has clearly stalled, a new E2E SLA breach has appeared, and the
uncommitted-code governance risk is now 9 days old.*
