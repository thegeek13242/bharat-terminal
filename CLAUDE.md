# CLAUDE.md — Bharat Terminal / Taperead

You are working on Bharat Terminal (public brand: Taperead), an Indian
equity market-intelligence system. Read this file fully before doing
anything. It encodes hard rules and project-specific context that have
been violated in the past with real cost.

## What this project is

A system that ingests Indian market news + filings + commodity/FX data,
runs a multi-stage analysis pipeline, and produces pre-computed,
channel-attributed company "dossiers" that map macro triggers to 1st-,
2nd-, and 3rd-degree exposed companies. Target users: analysts at
sub-₹2,000 Cr AUM PMS/AIF firms in India.

## The single most important rule: claims must match reality

This codebase has a documented history of documentation describing
capabilities that the database and running services do not actually
have. A full audit (`BHARAT_TERMINAL_AUDIT_2026-05-24.md`) found that
the prior overview claimed features that were empty, stale, or never
run.

Therefore:
- NEVER describe something as working, populated, or operational
  unless you have verified it with a query, a log line, or a file you
  just read. "The code exists" is NOT evidence that "it works."
- When you report status, distinguish: built-and-operational vs
  built-but-dormant vs scaffolded-never-run vs documented-but-absent.
- If you cannot verify a claim, say "unable to verify" explicitly.
  Do not infer or assume.
- Treat `BHARAT_TERMINAL_AUDIT_2026-05-24.md` as the current source of
  truth about system state, NOT the README or any older overview.

## Hard constraints — do not violate

1. READ-ONLY on production data unless the task explicitly says to
   write. Default to reads. If a task would modify production data,
   config, or a running service, STOP and state what you would do and
   why, then wait for explicit confirmation.

2. NEVER print, log, echo, or write secrets. The `.env` contains
   ANTHROPIC_API_KEY, four ANGEL_ONE_* credentials, DATABASE_URL, etc.
   Report presence/absence only — never values. Never commit `.env`.

3. NEVER hardcode credentials. Use the existing connection patterns in
   `bharat_terminal/storage/` (or wherever the DB connection is
   established — find and reuse it).

4. Database access is via:
   `docker exec bharat-postgres psql -U bharat -d bharat_terminal`
   Use read-only queries. Do not run migrations or DDL without explicit
   instruction.

5. Determinism: any sampling/bootstrap/random operation uses a fixed
   seed (e.g. np.random.seed(42)) and the seed must be visible.

## Verification gates — before claiming done

Before you report a task complete, you must:
- Show the evidence (query output, log line, test result, file diff)
  that proves it works.
- For data tasks: show row counts / NULL percentages / date ranges
  before AND after.
- For any data that flows into dossiers: confirm freshness. Stale data
  silently producing confident-but-wrong output is the project's
  characteristic failure mode. Flag staleness loudly.

## Known landmines (from the audit — verify, don't trust)

- `financial_results` historically stopped at 2024-12-31. Confirm
  current coverage before using it for anything date-sensitive.
- `companies` profile fields (sector_nse, industry_nse, mcap_cr,
  description_200w, revenue_segments, embedding) have been 100% NULL.
  Check before relying on them.
- `company_relationships` has been EMPTY (0 rows). Stage-4 propagation
  traverses it. Confirm before assuming propagation works.
- NSE has returned 403 Forbidden to the VM, blocking live filings,
  prices, and result-event sync. Check whether this is currently
  blocking your task.
- The commodity shock detector has logged `shocks=0` on every cycle
  ever. If you touch it, verify whether it has ever fired.
- Several tables are scaffolded (schema exists, 0 rows): derivatives_eod,
  trigger_betas, fundamental_exposures, sector_leaders,
  universe_membership, corporate_actions, shareholding_patterns.

## Scope discipline

- Do exactly the task asked. Do NOT opportunistically "fix" or
  "improve" adjacent code, rename things, or refactor unless the task
  says so. Scope creep across sessions compounds.
- If you notice something broken outside your task, NOTE it in your
  output for the human to triage — do not fix it silently.
- Prefer the smallest change that accomplishes the task.

## Communication style

- Lead with what you verified and what you changed, backed by evidence.
- Be honest about what you did NOT do or could NOT verify.
- No optimistic summaries. "Running but producing no output for 8 days"
  is the correct way to describe a dormant service — not "running."
- If a task is built on an assumption that turns out false, STOP and
  report rather than working around it silently.

## Tech stack reference

- Python services under `bharat_terminal/{ingestion,analysis,sentiment,
  commodity,fno_engine,kb,research,api}/`
- PostgreSQL 15 + pgvector, accessed via asyncpg
- Kafka/Redpanda event bus
- React + Vite + Tailwind frontend under `frontend/src/`
- Orchestrated via docker-compose; systemd unit auto-starts a subset
- Deployed on a single GCP VM (asia-south1-c)
- LLM stages use Anthropic (migrating to a provider abstraction with
  Gemini as an option — check current state before assuming)

## When in doubt

Ask. A clarifying question is cheap. A confident wrong action on
production data or a misleading "it works" claim is expensive and has
happened before on this project.
