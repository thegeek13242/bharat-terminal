# Bharat Terminal / Taperead — Status Report 2026-07-14

Regenerated against the live system (docker `ps`, service logs, and
`bharat-postgres`). Every claim below is backed by a query or a log line
observed on 2026-07-14 ~14:30 UTC. Items I could not verify are marked.
Supersedes `BHARAT_TERMINAL_STATUS_2026-06-27.md`.

Status vocabulary: **OPERATIONAL** (verified producing output now) ·
**DORMANT** (running but no recent output) · **BROKEN** (erroring now) ·
**SCAFFOLDED** (schema exists, 0 rows) · **STALE** (data present, not fresh).

---

## 0. What the product is (unchanged)

Taperead ingests Indian market news + filings + commodity/FX, runs a
multi-stage analysis pipeline, and produces pre-computed, channel-attributed
company **dossiers** mapping a macro trigger to 1st-/2nd-/3rd-degree exposed
companies. Target user: analysts at sub-₹2,000 Cr AUM PMS/AIF firms.

---

## 1. Service / container state (`docker ps` + logs)

All 15 containers report `Up`. Verified behaviour from logs (last 3–24h):

| Container | State | Evidence (2026-07-14) |
|---|---|---|
| bharat-postgres | OPERATIONAL | Up 4wk (healthy); all queries below ran |
| bharat-redpanda (Kafka) | OPERATIONAL | Up 4wk (healthy) |
| bharat-redis | OPERATIONAL | Up 4wk (healthy) |
| bharat-ingestion | OPERATIONAL | NSE live; **BSE adapter FIXED** — "scrip map loaded: 1631 BSE-only companies" at 14:08. (pypdf cmap warnings are cosmetic) |
| bharat-analysis | OPERATIONAL | Real-time filing processing at 14:29 (`[RT] Processing … Published latency 11–33ms`) |
| bharat-sentiment | OPERATIONAL (0 signals **by design**) | `httpx` dep now present (Jun-27 bug fixed); Polymarket→impact bridge emits impacts, signals dropped by gates / RA lane |
| bharat-commodity | OPERATIONAL | **FIXED** — `ticks=913500 shocks=10` at 14:32 (was `shocks=1`/dormant on Jun 27); per-commodity z-score detector live |
| bharat-kb (:8001) | OPERATIONAL | Up 17h (healthy) |
| bharat-kb-monitor | OPERATIONAL | Live NSE XBRL sync at 11:02 today; event_sync `synced=10 failed=0` |
| bharat-api (:8000) | OPERATIONAL | Serves auth-gated routes incl. new `/investors`, `/deals` (401 without token) |
| bharat-mcp (:8002) | OPERATIONAL | Up 2wk |
| bharat-frontend (:80/:443) | OPERATIONAL | Up 17h (healthy) |
| bharat-onboarding | OPERATIONAL | Up 6d (demo-request provisioning worker) |
| bharat-prometheus / bharat-grafana | OPERATIONAL | Up 4wk |

Auto-start: `bharat-terminal.service` (systemd) brings the compose stack up on
VM boot. The VM no longer self-terminates (5h self-stop removed 2026-05-31).

---

## 2. Database — freshness (35 tables total)

Counts and freshness verified 2026-07-14. Future dates in calendars
(upcoming earnings / ex-dates) are forward-looking, not bad data.

| Table | Rows | Freshness | State |
|---|---:|---|---|
| price_points | 3,499,964 | 2026-07-13 | **OPERATIONAL** — 2017→present backfill landed (was 595K on Jun 27) |
| impact_reports | 35,632 | 2026-07-14 10:34 | **OPERATIONAL** — core analysis output, live |
| news_items | 33,999 | 2026-07-14 10:34 | **OPERATIONAL** — multi-source, live |
| nse_filings | 54,878 | 2026-07-13 21:15 | **OPERATIONAL** |
| financial_results | 102,796 | period_end **2026-06-30** | **OPERATIONAL** — current to Q1 FY27; ~2× vs Jun 27 (BSE coverage added) |
| investor_holdings | 192,638 | quarter_end 2026-07-01 | **OPERATIONAL** — marquee smart-money (migration 021) |
| market_deals | 1,558 | 2026-07-13 | **OPERATIONAL** — daily bulk/block deals cron (migration 022) |
| company_relationships | 1,717 | — | **OPERATIONAL** — populated (was EMPTY pre-Jun) |
| signal_outcomes | 1,424 | — | **OPERATIONAL** — accuracy tracking (was 768 on Jun 27) |
| **company_dossiers** | **469** | **generated_at 2026-06-26** | **STALE (~18 days)** — no regeneration in kb logs; running on pre-Q1-FY27 inputs |
| app_users | 7 | — | Invite allowlist grown from 1 → 7 |
| universe_membership | 0 | — | **SCAFFOLDED** |
| sector_leaders | 0 | — | **SCAFFOLDED** |

`sync_log` (last 36h): `financials success`, `prices success` (both 2026-07-14
13:45), `market_deals_sync completed` (2026-07-13). Daily syncs healthy.

Note: `derivative_ticks` / `derivatives_eod` referenced in the Jun-27 doc no
longer exist as tables — schema changed; not re-investigated here.

---

## 3. `companies` profile fields — landmine, still partial

Universe grew to **3,998** companies (was 2,367 — BSE-only universe merged in).

| Field | Coverage |
|---|---|
| mcap_cr | 3,576 / 3,998 (89%) |
| sector_nse | 750 / 3,998 (19%) |
| industry_nse | 0 (100% NULL) |
| description_200w | 0 (100% NULL) |
| embedding | 0 (100% NULL) |

Sector coverage *dropped* in percentage terms because the BSE universe was
added without sector backfill. `industry_nse`, `description_200w`, and the
pgvector `embedding` remain entirely empty — **semantic/vector search over
companies is still not backed by embeddings.**

---

## 4. Audit-landmine scorecard (vs CLAUDE.md / prior audits)

| Landmine | Prior state | Now (2026-07-14) |
|---|---|---|
| financial_results frozen at 2024-12-31 | stale | **RESOLVED** — current to 2026-06-30 |
| NSE 403 Forbidden to VM | blocking | **RESOLVED** — nsearchives host, 200 OK |
| company_relationships EMPTY | 0 rows | **RESOLVED** — 1,717 rows |
| commodity shock detector `shocks=0` forever | never fired | **RESOLVED** — `shocks=10`, per-commodity z-score |
| BSE filings adapter crash (`'str' has no .get`) | broken Jun 27 | **RESOLVED** — 1,631 BSE-only companies loaded |
| sentiment container missing `httpx` | 0 signals (bug) | **RESOLVED** — dep present; 0 signals now by design (gates + RA lane) |
| companies profile fields 100% NULL | all NULL | **PARTIAL** — mcap 89%; sector 19%; industry/desc/embedding still NULL |
| Scaffolded: universe_membership, sector_leaders | 0 rows | **STILL EMPTY** |

---

## 5. Bugs / risks observed live (note, do not silently fix)

1. **Dossiers stale ~18 days.** Last `generated_at` = 2026-06-26; no dossier
   activity in `bharat-kb` logs over 48h. Financials advanced to Q1 FY27 and
   prices are current, so the 469 dossiers run on ~3-week-old inputs. Confirm
   whether the generator is intentionally dormant.
2. **DCF refresh bug.** `bharat-kb-monitor`:
   `DCF refresh failed for MBAPL: 'Company' object has no attribute 'pat_ttm_cr'`
   — DCF fair-value refresh throwing an AttributeError during symbol sync.
3. **Signal quality (known).** LLM trade signals ≈ coin-flip (gates parked per
   no-improvement rule). PEAD composite-SUE is the one validated statistic
   (D10−D1 +2.11% @20d, t=4.7). Signals stripped from customer surfaces
   (RA-compliance Lane A) — internal-only, uncommitted.

---

## 6. Active development — branch `feat/investor-holdings`

Marquee smart-money feature (Jhunjhunwala/President/LIC/MFs/FIIs from SHP named
holders + bulk/block deals with market-maker/HFT-churn exclusion).

- **Deployed but UNCOMMITTED.** Running `bharat-api` serves `/investors`,
  `/investors/{ident}`, `/deals` (401 auth-gated → registered) from working-tree
  code. Migrations 021 + 022 applied (192,638 + 1,558 rows).
- Uncommitted files: `api/routes/investor_holdings.py`, `kb/marquee.py`,
  `kb/market_makers.py`, `kb/sync/investor_holdings_sync.py`,
  `kb/sync/market_deals_sync.py`, `frontend/src/pages/Investors.tsx`,
  migrations 021/022, plus edits to `main.py`, `models.py`, `manage.py`,
  `NavSidebar.tsx`, `api.ts`, `DossierModal.tsx`.
- Untracked in repo root: `PITCH_DECK_HANDOFF.md`, `new_frontend_handoff.zip`,
  `exports/derivative_ticks_backup_2026-07-02.csv`.

---

## 7. LLM / pipeline architecture (unchanged from Jun 27)

- All LLM stages on **GCP Vertex AI** via `bharat_terminal/llm` wrapper
  (provider-agnostic). Dossiers + analysis on Gemini (`gemini-3.1-pro-preview`).
- **Grounding layer** (`bharat_terminal/grounding`) + **MCP server** (:8002)
  expose DB-grounding tools (financials/prices, taxonomy + sector_financials)
  and web search.
- **API** (:8000) serves companies, dossiers, impact feed, sentiment,
  earnings-surprises (PEAD), investors/deals, and Google OAuth gated by the
  `app_users` allowlist (7 users).
- **Frontend** (React + Vite, :80/:443) — Taperead terminal UI, live.

---

## 8. Things to triage (not fixed here)

- Stale dossier generation (§5.1) — highest-signal item; the project's
  characteristic failure mode is stale data silently producing confident output.
- DCF refresh AttributeError (§5.2).
- `industry_nse`, `description_200w`, company `embedding` still 100% NULL — no
  semantic search backing.
- `feat/investor-holdings` deployed-but-uncommitted (§6) — divergence between
  running prod and git HEAD.
- Open compliance items (RA Lane A): DCF fair-value + "1,800+ dossiers" login
  claim — dossiers table actually holds 469.
