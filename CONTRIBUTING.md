# Contributing to Bharat Terminal

Thank you for your interest in contributing! Here's how to get started.

## Quick Setup

```bash
git clone https://github.com/thegeek13242/bharat-terminal.git
cd bharat-terminal
cp .env.example .env
# Set ANTHROPIC_API_KEY in .env
docker compose up --build
```

## How to Contribute

### Reporting Bugs

Open an issue using the **Bug Report** template. Include:
- Steps to reproduce
- Expected vs. actual behaviour
- `docker compose ps` output
- Relevant container logs (`docker compose logs <service>`)

### Suggesting Features

Open an issue using the **Feature Request** template. Describe the use case — what problem does it solve for Indian equity traders?

### Submitting a Pull Request

1. Fork the repo and create a branch: `git checkout -b feat/your-feature`
2. Make your changes (see areas below)
3. Test locally: `docker compose up --build`
4. Commit with a clear message: `git commit -m "feat: add IIFL as RSS source"`
5. Open a PR against `main`

## Good First Contributions

| Area | What to do |
|------|-----------|
| **New news source** | Add an adapter in `bharat_terminal/ingestion/adapters/` — copy `rss_base.py` as a template |
| **Company data** | Expand `bharat_terminal/kb/manage.py` with more seed companies and relationships |
| **Frontend graph** | Replace the edge table in `ImpactGraph.tsx` with a Sigma.js force-directed graph |
| **Tests** | Add pytest fixtures for the analysis pipeline stages in `tests/` |
| **Docs** | Improve architecture diagrams, add a Grafana dashboard screenshot |

## Project Structure

```
bharat_terminal/
  ingestion/        # News adapters + Kafka producer
  analysis/         # LangGraph pipeline (5 stages)
  kb/               # Knowledge base API, DCF, company models
  api/              # API gateway, WebSocket relay
  types.py          # Shared Pydantic models (cross-service contract)
frontend/
  src/components/   # React components
  src/hooks/        # WebSocket, data hooks
docker/             # Per-service Dockerfiles
migrations/         # Alembic DB migrations
requirements/       # Per-service Python deps
```

## Code Style

- Python: follow existing style (no formatter enforced yet — PRs welcome)
- TypeScript: Prettier defaults (run `npm run format` in `frontend/`)
- Pydantic models in `types.py` are frozen — coordinate changes across all consumers

## Questions

Open a [Discussion](https://github.com/thegeek13242/bharat-terminal/discussions) — happy to help.
