<p align="center">
  <img src="docs/banner.png" alt="Bharat Terminal" width="800" />
</p>

<h1 align="center">Bharat Terminal</h1>

<p align="center">
  <strong>Real-time Indian equity market intelligence — open source, self-hosted, AI-powered</strong>
</p>

<p align="center">
  <a href="https://github.com/aviralverma/bharat-terminal/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License" /></a>
  <img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/docker-compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose" />
  <img src="https://img.shields.io/badge/Claude-Haiku-blueviolet?logo=anthropic" alt="Claude Haiku" />
  <img src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black" alt="React 18" />
  <a href="https://github.com/aviralverma/bharat-terminal/stargazers"><img src="https://img.shields.io/github/stars/aviralverma/bharat-terminal?style=social" alt="Stars" /></a>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> ·
  <a href="#-architecture">Architecture</a> ·
  <a href="#-what-it-does">What It Does</a> ·
  <a href="#-api-reference">API</a> ·
  <a href="#-contributing">Contributing</a>
</p>

---

Bharat Terminal ingests live news from **8 Indian financial sources**, runs it through a **5-stage LangGraph LLM pipeline** (relevance → entity extraction → impact scoring → graph propagation → trade signals), and streams structured results to a **Bloomberg-style terminal UI** — all in under 5 seconds per article, entirely on your own machine.

> **No SaaS fees. No data leaving your machine. One `docker compose up`.**

---

## What It Does

| Feature | Detail |
|---------|--------|
| **Live news ingestion** | NSE/BSE filings + 6 RSS feeds, circuit-breaker adapters, Redpanda (Kafka-compatible) |
| **AI analysis pipeline** | LangGraph DAG, Claude Haiku, ≤5s end-to-end P95 latency |
| **Company knowledge base** | ~5,000 NSE/BSE companies, DCF models, analyst consensus, pgvector entity resolution |
| **Impact graph** | 2-hop relationship propagation — news about RIL ripples to Jio, RRVL, IOC |
| **Trade signals** | Direction, conviction, position size, stop-loss rationale per signal |
| **Zero telemetry** | All data stays local — Postgres + Redis + Kafka, no cloud required |
| **Observability** | Prometheus + Grafana dashboards out of the box |

---

## Quick Start

**Prerequisites:** Docker Desktop, an [Anthropic API key](https://console.anthropic.com/)

```bash
# 1. Clone
git clone https://github.com/aviralverma/bharat-terminal.git
cd bharat-terminal

# 2. Configure
cp .env.example .env
# Edit .env and set: ANTHROPIC_API_KEY=sk-ant-api03-...

# 3. Boot (cold start ~3 min — builds images, runs migrations, seeds DB)
docker compose up --build

# 4. Open
open http://localhost:3000
```

That's it. All 11 services start automatically. When `docker compose ps` shows every container as `(healthy)`, the pipeline is live.

> **Tip:** The `kb-seed` container automatically seeds RELIANCE.NS with 7 company relationships so the impact graph has data immediately.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           BHARAT TERMINAL                               │
│                                                                         │
│  ┌──────────────┐    raw.news.in     ┌──────────────────────────────┐  │
│  │  INGESTION   │ ─────────────���────▶│      ANALYSIS PIPELINE       │  │
│  │              │    (Redpanda)      │                              │  │
│  │  NSE Filings │                   │  Stage 1: Relevance  ≤300ms  │  │
│  │  BSE Filings │                   │  Stage 2: Extraction ≤800ms  │  │
│  │  Reuters IN  │                   │  Stage 3: Impact     ≤1.5s   │  │
│  │  Econ Times  │                   │  Stage 4: Propagation≤500ms  │  │
│  │  Mint        │                   │  Stage 5: Signals    ≤800ms  │  │
│  │  MoneyControl│                   │                              │  │
│  │  NDTV Profit │                   │  LangGraph + Claude Haiku    │  │
│  │  PTI         │                   └──────────────┬───────────────┘  │
│  └──────────────┘                                  │ analysed.impact.in│
│                                                    ▼                   │
│  ┌──────────────────────┐          ┌───────────────────────────────┐  │
│  │   KNOWLEDGE BASE     │◀─────────│       API GATEWAY             │  │
│  │                      │          │                               │  │
│  │  PostgreSQL+pgvector │          │  FastAPI  :8000               │  │
│  │  ~5,000 companies    │          │  WebSocket /ws/feed           │  │
│  │  DCF models          │          │  REST endpoints               │  │
│  │  HNSW embeddings     │          └──────────────┬────────────────┘  │
│  │  FastAPI  :8001      │                         │                   │
│  └──────────────────────┘                         ▼                   │
│                                    ┌───────────────────────────────┐  │
│                                    │      REACT TERMINAL UI        │  │
│                                    │                               │  │
│                                    │  Live Feed · Impact Graph     │  │
│                                    │  Trade Ideas · Watchlist      │  │
│                                    │  Nginx  :3000                 │  │
│                                    └───────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘

Infrastructure: Redpanda :9092 · PostgreSQL :5432 · Redis :6379
Observability:  Prometheus :9090 · Grafana :3001
```

---

## Services & Ports

| Service | Port | Description |
|---------|------|-------------|
| **Frontend** | 3000 | React terminal UI (Nginx) |
| **API Gateway** | 8000 | FastAPI — REST + WebSocket `/ws/feed` |
| **KB Service** | 8001 | Company profiles, DCF, graph |
| **Redpanda Console** | 8080 | Kafka topic browser |
| **Grafana** | 3001 | Metrics dashboards (admin / bharat) |
| **Prometheus** | 9090 | Metrics scraper |
| **Redpanda** | 9092 | Kafka-compatible broker |
| **PostgreSQL** | 5432 | Knowledge base + pgvector |
| **Redis** | 6379 | Cache + watchlist storage |

---

## Analysis Pipeline

Each news article flows through a 5-stage LangGraph DAG. Irrelevant articles are short-circuited at Stage 1 without any LLM calls.

```
NewsItem (Kafka)
    │
    ▼
Stage 1: Relevance          cosine similarity vs. financial corpus
    │                        NSE/BSE filings always pass
    ├─ irrelevant ──────────▶ skip (no LLM cost)
    │
    ▼
Stage 2: Entity Extraction  Claude Haiku — named companies, sectors, macro theme
    │
    ▼
Stage 3: Impact Scoring     Claude Haiku — sentiment, magnitude 1–5, time horizon
    │                        fetches company context from KB Service
    ▼
Stage 4: Graph Propagation  2-hop traversal — supplier/customer/JV relationships
    │                        decay: hop1 × 0.6, hop2 × 0.35
    ▼
Stage 5: Signal Generation  Claude Haiku — direction, conviction, position size
    │                        only for hop_distance=0, magnitude≥3
    ▼
ImpactReport (Kafka → WebSocket → UI)
```

---

## API Reference

### REST (API Gateway — port 8000)

```
GET  /health
GET  /news/feed?limit=50&sector=BANKING
GET  /impact/{news_id}
GET  /company/{symbol}
GET  /company/search/?q=reliance
GET  /graph/{symbol}?hops=2
GET  /watchlist/
POST /watchlist/
WS   /ws/feed
```

### WebSocket event schema

```json
{
  "type": "impact_report",
  "data": {
    "relevant": true,
    "macro_theme": "EARNINGS",
    "company_impacts": [
      { "symbol": "HDFCBANK", "sentiment": "positive", "magnitude": 4, "hop_distance": 0 }
    ],
    "trade_signals": [
      { "symbol": "HDFCBANK", "direction": "long", "conviction": "high", "position_size_pct_of_portfolio": 4.0 }
    ],
    "processing_latency_ms": 2340
  }
}
```

---

## SLA Targets

| Stage | Target |
|-------|--------|
| Stage 1 — Relevance | ≤300ms |
| Stage 2 — Extraction | ≤800ms |
| Stage 3 — Impact | ≤1.5s |
| Stage 4 — Propagation | ≤500ms |
| Stage 5 — Signals | ≤800ms |
| **End-to-end P95** | **≤5s** |
| GET /company (cached) | ≤5ms |
| GET /company (DB) | ≤200ms |

---

## Configuration

Only one variable is required:

```bash
ANTHROPIC_API_KEY=sk-ant-api03-...
```

See [`.env.example`](.env.example) for all tuning options (model selection, relevance threshold, poll intervals).

---

## Development

```bash
# Run individual services locally (with infra running via docker compose)
PYTHONPATH=. python -m bharat_terminal.ingestion.main
PYTHONPATH=. python -m bharat_terminal.analysis.main
PYTHONPATH=. uvicorn bharat_terminal.kb.api:app --reload --port 8001
PYTHONPATH=. uvicorn bharat_terminal.api.main:app --reload --port 8000
cd frontend && npm install && npm run dev
```

---

## Data Sources

| Source | Type | Interval |
|--------|------|----------|
| NSE Corporate Filings | JSON API | 30s |
| BSE Corporate Filings | JSON API | 30s |
| Reuters India | RSS | 120s |
| Economic Times | RSS | 120s |
| Mint / LiveMint | RSS | 120s |
| MoneyControl | RSS | 120s |
| NDTV Profit | RSS | 120s |
| PTI (via GNews) | RSS | 180s |

NSE/BSE adapters require session cookies for full access (NSE blocks headless requests). The 6 RSS adapters work without credentials.

---

## Stack

| Layer | Technology |
|-------|------------|
| Message broker | Redpanda (Kafka-compatible) |
| Database | PostgreSQL 15 + pgvector |
| Cache | Redis 7 |
| LLM | Claude Haiku via Anthropic API |
| Pipeline | LangGraph + langchain-core |
| Embeddings | sentence-transformers (CPU, multilingual) |
| API | FastAPI + uvicorn |
| Frontend | React 18 + TypeScript + Tailwind + Vite |
| Observability | Prometheus + Grafana |
| Container | Docker Compose |

---

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

Good first issues:
- Add more company relationships to the knowledge base seed
- Add a new RSS news source adapter
- Improve the frontend graph visualisation (replace table view with Sigma.js)
- Write integration tests for the analysis pipeline

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  Built with ❤️ for the Indian markets community
</p>
