#!/usr/bin/env bash
# One-command unit-test runner — the pre-deploy gate.
#
# Runs pytest inside a THROWAWAY container from the papertrade image
# (python 3.12 + the full papertrade dependency surface: asyncpg, sklearn,
# pandas, aiokafka, anthropic, google-genai). The host has no venv and host
# python is 3.14 — tests only ever run in-container.
#
# Safety by construction:
#   * --no-deps + --rm: never starts or touches the running services.
#   * DATABASE_URL / REDIS_URL / KAFKA are overridden to unresolvable hosts,
#     so any test that accidentally reaches for real infra fails loudly
#     instead of silently touching production.
#   * MATERIALITY_ARTIFACT points at a nonexistent path so the gate's
#     fail-open branch is deterministic (no 53MB joblib load in unit tests).
#   * PAPER_* knobs are pinned to their documented defaults so test
#     arithmetic is independent of whatever .env sets for the live pilot.
#
# Usage:
#   scripts/test.sh                 # whole suite
#   scripts/test.sh tests/papertrade/test_exit_reason.py -k squareoff -x
set -euo pipefail
cd "$(dirname "$0")/.."

exec docker compose run --rm --no-deps \
  -v "$PWD/bharat_terminal:/app/bharat_terminal:ro" \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/infra:/app/infra:ro" \
  -v "$PWD/requirements/test.txt:/app/requirements-test.txt:ro" \
  -v "$PWD/pyproject.toml:/app/pyproject.toml:ro" \
  -e DATABASE_URL="postgresql://tests-must-not-touch-db:1/invalid" \
  -e REDIS_URL="redis://tests-must-not-touch-redis:1/0" \
  -e KAFKA_BOOTSTRAP_SERVERS="tests-must-not-touch-kafka:1" \
  -e KB_SERVICE_URL="http://tests-must-not-touch-kb:1" \
  -e MATERIALITY_ARTIFACT="/nonexistent/materiality.joblib" \
  -e PAPER_C0_MODE="off" \
  -e PAPER_SLIPPAGE_BPS="10" \
  -e PAPER_NOTIONAL_PER_TRADE="100000" \
  -e PAPER_CAPITAL="1000000" \
  -e PAPER_GROSS_CAP="0" \
  -e PAPER_STOP_PCT="5.0" \
  -e PAPER_TRAIL_SIGMA="2.0" \
  -e PAPER_PEAD_MAX_GAP_PCT="2.0" \
  -e PAPER_MAX_EVENT_AGE_S="900" \
  -e PAPER_HEADWIND_MIN_MAG="4" \
  -e PAPER_SHORT_HOLD_DAYS="5" \
  -e PAPER_PEAD_HOLD_DAYS="20" \
  -e PAPER_ORDERWIN_HOLD_DAYS="5" \
  -e PAPER_ENABLE_HEADWIND_SHORT="1" \
  -e PAPER_ENABLE_PEAD_LONG="1" \
  -e PAPER_ENABLE_ORDERWIN_LONG="1" \
  -e PAPER_ENABLE_TAILWIND_LONG="0" \
  --entrypoint bash papertrade -c \
  "pip install -q --root-user-action=ignore -r /app/requirements-test.txt \
     && cd /app && python -m pytest ${*:-tests} -q"
