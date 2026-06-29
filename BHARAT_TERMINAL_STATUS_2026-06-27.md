# Bharat Terminal / Taperead — Status Report 2026-06-27

Regenerated from scratch against the live system (docker, service logs, and
`bharat-postgres`). Every claim below is backed by a query or a log line
observed on 2026-06-27 ~06:00 UTC. Items I could not verify are marked.

Status vocabulary: **OPERATIONAL** (verified producing output now) ·
**DORMANT** (running but no recent output) · **BROKEN** (erroring now) ·
**SCAFFOLDED** (schema exists, 0 rows) · **STALE** (data present but not fresh).

---

## 1. Service / container state (`docker ps` + logs)

All 13 containers report `Up`. Verified behaviour from logs:

| Container | State | Evidence |
|---|---|---|
| bharat-postgres | OPERATIONAL | Up 2 weeks (healthy), all queries below ran |
| bharat-redpanda (Kafka) | OPERATIONAL | analysis-workers group joined, partitions assigned |
| bharat-redis | OPERATIONAL | Up 2 weeks (healthy) |
| bharat-ingestion | OPERATIONAL (partial) | MCX websocket connected, 6 MCX + 51 NFO tokens; Polymarket cycling; NSE 200 OK. **BSE adapter BROKEN** (see §4) |
| bharat-analysis | OPERATIONAL | Consuming `raw.news.in`; 703 impact_reports in last 24h |
| bharat-sentiment | OPERATIONAL (degraded) | Polymarket→impact bridge emits impacts; **0 signals** due to missing `httpx` (see §4) |
| bharat-commodity | **DORMANT — needs verification** | Last log 2026-06-26 18:25 UTC; 0 lines in last 2h despite live MCX feed into ingestion. Cumulative ticks=2.57M, shocks=1 |
| bharat-kb (:8001) | OPERATIONAL | Up 13h (healthy) |
| bharat-kb-monitor | OPERATIONAL | NSE 200 OK; event calendar, ownership sweep, XBRL financials sync all running |
| bharat-api (:8000) | OPERATIONAL | `/health` → `{"status":"ok", ws_connections:2}` |
| bharat-mcp (:8002) | OPERATIONAL | MCP protocol responds on `/mcp` (session-ID handshake) |
| bharat-frontend (:80/:443) | OPERATIONAL | Up 16h (healthy) |
| bharat-prometheus / bharat-grafana (:9090/:3001) | OPERATIONAL | Up 2 weeks |

Auto-start: `bharat-terminal.service` (systemd) is `loaded active exited` —
brings the compose stack up on VM boot.

---

## 2. Database — populated tables (29 total)

Counts and freshness verified 2026-06-27. "Future" dates are forward-looking
calendars (upcoming earnings / ex-dates / scheduled items), not bad data.

| Table | Rows | Freshness | Notes |
|---|---:|---|---|
| price_points | 595,578 | 2026-06-25 | Daily NSE bhavcopy sync working (2,329 rows/day) |
| nse_filings | 54,851 | 2026-06-25 | Live |
| financial_results | 54,079 | period_end **2026-03-31** | **Dec-2024 freeze RESOLVED** — current to Q4 FY26 |
| impact_reports | 21,300 | live (703 / 24h) | Core analysis output |
| news_items | 19,680 | live (702 / 24h, 24 / 1h) | Multi-source (see §5) |
| corporate_actions | 17,503 | ex_date → 2026-08-27 | **Was SCAFFOLDED — now populated** |
| shareholding_patterns | 8,929 | quarter_end 2026-06-23 | **Was SCAFFOLDED — now populated** |
| companies | 2,367 | — | Profile-field coverage uneven (§3) |
| dossier_linkages | 2,128 | 2026-06-26 | 2nd/3rd-degree exposure edges |
| derivative_ticks | 1,859 | **2026-05-14 (STALE ~6wk)** | Not being appended to |
| result_events | 1,753 | result_date → 2026-07-09 | Earnings calendar |
| company_relationships | 1,676 | 2026-06-24 | **Was EMPTY (0) — now populated** |
| dossier_sensitivities | 1,562 | 2026-06-26 | Trigger→company betas/channels |
| signal_outcomes | 768 | 2026-06-26 | Accuracy tracking (§6) |
| participant_oi | 600 | 2026-06-25 | F&O participant OI |
| company_dossiers | 469 | 2026-06-26 | All gemini-3.1-pro-preview; avg data_quality 0.859 |
| trigger_betas | 362 | 2026-06-26 | **Was SCAFFOLDED — now populated** |
| fundamental_exposures | 123 | 2026-06-26 | **Was SCAFFOLDED — now populated** |
| sync_log | 45 | 2026-06-26 | All recent syncs `success` |
| triggers | 36 | — | Macro trigger catalog |
| macro_events | 9 | — | |
| app_users | 1 | — | Auth allowlist: `aviralv35@gmail.com` |

**Still SCAFFOLDED (0 rows):** `derivatives_eod`, `universe_membership`,
`sector_leaders`.

---

## 3. `companies` profile fields — audit landmine, partial fix

| Field | Coverage |
|---|---|
| mcap_cr | 2,197 / 2,367 (93%) |
| sector_nse | 750 / 2,367 (32%) |
| industry_nse | 0 (100% NULL) |
| revenue_segments | 0 (100% NULL) |
| description_200w | 0 (100% NULL) |
| embedding | 0 (100% NULL) |

So the "100% NULL profile fields" landmine is **partially** resolved: market
cap and financial/DCF fields are well populated, but sector is only one-third
covered, and `industry_nse`, `revenue_segments`, `description_200w`, and the
pgvector `embedding` are still entirely empty. Semantic search over companies
is therefore **not** backed by embeddings yet.

---

## 4. Bugs observed live (note, do not silently fix)

1. **BSE filings adapter BROKEN.** `bharat-ingestion` logs
   `[BSE] Error fetching announcements: 'str' object has no attribute 'get'`
   repeatedly (6 occurrences in 30 min). BSE corporate announcements are not
   being ingested. NSE path is unaffected.
2. **Sentiment→signal bridge degraded.** `bharat-sentiment` logs
   `No module named 'httpx'` in `analysis.signal_gates` symbol resolution →
   every candidate signal dropped as "unresolvable symbol". Bridge emits
   impacts but **0 trade signals**. Missing dependency in that container image.
3. **investing.com 403.** `INVESTING_IN` source returns HTTP 403 on some
   article fetches (still ingesting 14/24h, so partially working).

---

## 5. Ingestion sources (news_items, last 24h)

| Source | Count |
|---|---:|
| NSE_FILINGS | 481 |
| PTI | 56 |
| MINT | 53 |
| ECONOMIC_TIMES | 45 |
| POLYMARKET_SENTIMENT | 43 |
| INVESTING_IN | 14 |
| MARKETS_FEED | 8 |
| REUTERS_INDIA | 2 |

MCX/F&O market data flows via AngelOne websocket (SNAP_QUOTE, 6 MCX + 51 NFO
tokens). Daily NSE bhavcopy + XBRL financials sync via kb-monitor.

---

## 6. Analysis / signal quality

- **impact_reports:** 703 in last 24h, 649 flagged `relevant`. Only 22 carried
  non-empty `trade_signals` — most relevant news produces impact mapping
  without an actionable trade.
- **company_dossiers:** 469 companies, all generated by `gemini-3.1-pro-preview`,
  avg `data_quality` 0.859 (range 0.80–0.95), 467 regenerated in the last 7 days.
- **signal_outcomes (accuracy tracking, OPERATIONAL):** 599 `partial`,
  86 `pending`, 69 `no_price_data`, 14 `complete`. 5-day directional hit rate
  ≈ 54.5% (partial) / 58.3% (complete). Small N — directional only.

---

## 7. LLM / pipeline architecture

- All LLM stages on **GCP Vertex AI** via `bharat_terminal/llm` wrapper
  (provider-agnostic). Dossiers + analysis on Gemini (`gemini-3.1-pro-preview`),
  no Claude calls on Vertex. Verified: every dossier `model_used` = Gemini.
- **Grounding layer** (`bharat_terminal/grounding`) + **MCP server** (:8002)
  expose DB-grounding tools (financials/prices, taxonomy + sector_financials)
  and web search; MCP responds on `/mcp`.
- **API** (`bharat_terminal/api`, :8000) serves companies, dossiers, impact
  feed, sentiment, signal-accuracy, graph/linkages, watchlist, and Google OAuth
  (`/auth/google`, `/auth/me`) gated by the `app_users` allowlist.
- **Frontend** (React + Vite, served on :80/:443) — Taperead terminal UI, live.

---

## 8. Audit-landmine scorecard (vs CLAUDE.md / prior audits)

| Landmine | Prior state | Now |
|---|---|---|
| financial_results frozen at 2024-12-31 | stale | **RESOLVED** — current to 2026-03-31 |
| NSE 403 Forbidden to VM | blocking | **RESOLVED** — 200 OK in kb-monitor logs |
| company_relationships EMPTY | 0 rows | **RESOLVED** — 1,676 rows |
| commodity shock detector `shocks=0` always | never fired | **RESOLVED** — shocks=1 logged |
| companies profile fields 100% NULL | all NULL | **PARTIAL** — mcap/financials filled; sector 32%; industry/desc/embedding still NULL |
| Scaffolded tables (corporate_actions, shareholding_patterns, trigger_betas, fundamental_exposures) | 0 rows | **RESOLVED** — populated |
| Scaffolded: derivatives_eod, universe_membership, sector_leaders | 0 rows | **STILL EMPTY** |

---

## 9. Things to triage (not fixed here)

- `bharat-commodity` produced no log output for ~12h while MCX ticks flow into
  ingestion — verify the consumer isn't hung (classic "running but silent"
  failure mode). `derivative_ticks` is stale since 2026-05-14, consistent with
  this.
- BSE filings adapter crash (`'str' object has no attribute 'get'`).
- Sentiment container missing `httpx` → zero signals from the Polymarket bridge.
- `industry_nse`, `revenue_segments`, `description_200w`, company `embedding`
  remain 100% NULL — no semantic/vector search backing yet.
- `new_frontend_handoff.zip` is an untracked file in the repo root.
