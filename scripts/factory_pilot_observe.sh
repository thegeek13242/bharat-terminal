#!/usr/bin/env bash
# One observational pilot cycle. Scheduled hourly; see PILOT notes below.
#
# WHAT THIS DOES AND DOES NOT DO
#   propose  : REAL. Hypotheses land in the ledger and LLM spend is recorded
#              and capped, because we want to see what the system actually
#              generates, not a simulation of it.
#   evaluate : DRY. The cycle runner reports the verdict it WOULD reach but
#              charges no alpha wealth and writes no gate_decision.
#   frontier : REAL, every CHIEF_EVERY cycles. The Chief Scientist reads the
#              deterministic family facts and records ONE allocation snapshot.
#              It runs with --apply for two reasons: submit_allocation can only
#              RECORD a recommendation (factory_writer holds no UPDATE, so
#              nothing it writes can pause or kill a family), and the spend is
#              only charged against the budget cap under --apply. A dry run
#              would still spend the tokens and simply not count them, which is
#              the worse failure.
#
# Why evaluation stays dry: alpha wealth is ONE shared budget across the whole
# hypothesis stream, not per family. Charging ~24 PEAD variants overnight would
# raise the bar for every future hypothesis from |t|>=2.92 to |t|>=4.04 and burn
# half the budget on a family already measured at t~2.2. An observational pilot
# is for watching behaviour, not for consuming a finite resource.
#
# STOPPING: delete the crontab line, or `touch /home/ec2-user/factory_pilot/STOP`.
set -uo pipefail

ROOT=/home/ec2-user/bharat-terminal
PILOT=/home/ec2-user/factory_pilot
LOG="$PILOT/observe.log"
COUNT="$PILOT/cycle_count"
MAX_CYCLES=24                       # 24h at hourly cadence, then it stops itself
CHIEF_EVERY=6                       # frontier review cadence; 0 disables it

cd "$ROOT" || exit 1
mkdir -p "$PILOT"
[ -f "$COUNT" ] || echo 0 > "$COUNT"
n=$(cat "$COUNT")

if [ -f "$PILOT/STOP" ]; then
  echo "$(date -Is)  STOP file present; skipping" >> "$LOG"; exit 0
fi
if [ "$n" -ge "$MAX_CYCLES" ]; then
  echo "$(date -Is)  reached MAX_CYCLES=$MAX_CYCLES; not running" >> "$LOG"; exit 0
fi

n=$((n + 1)); echo "$n" > "$COUNT"

{
  echo ""
  echo "════════ cycle $n/$MAX_CYCLES  $(date -Is) ════════"
  echo "──── PROPOSE (real: hypotheses written, spend recorded and capped)"
  docker compose run --rm --no-deps \
    -v "$ROOT/bharat_terminal:/app/bharat_terminal:ro" \
    -v "$ROOT/scripts:/app/scripts:ro" \
    -e BEDROCK_RPM_LIMIT=4 \
    --entrypoint python papertrade scripts/factory_alpha_scientist.py --apply 2>&1 \
    | grep -v "^time=" | grep -v "notice.*pip"

  echo "──── EVALUATE (dry: no alpha charged, no verdict written)"
  docker compose run --rm --no-deps \
    -v "$ROOT/bharat_terminal:/app/bharat_terminal:ro" \
    -v "$ROOT/scripts:/app/scripts:ro" \
    -v "/home/ec2-user/factory_snapshots:/snapshots:ro" \
    --entrypoint python papertrade scripts/factory_run_cycle.py 2>&1 \
    | grep -v "^time=" | grep -v "notice.*pip"

  if [ "$CHIEF_EVERY" -gt 0 ] && [ $((n % CHIEF_EVERY)) -eq 0 ]; then
    echo "──── FRONTIER (Chief Scientist: records an allocation, cannot apply one)"
    docker compose run --rm --no-deps \
      -v "$ROOT/bharat_terminal:/app/bharat_terminal:ro" \
      -v "$ROOT/scripts:/app/scripts:ro" \
      -e BEDROCK_RPM_LIMIT=4 \
      --entrypoint python papertrade scripts/factory_chief_scientist.py --apply 2>&1 \
      | grep -v "^time=" | grep -v "notice.*pip"
  fi

  echo "──── LEDGER"
  docker exec bharat-postgres psql -U bharat -d bharat_terminal -tAc \
    "SELECT '  hypotheses='||(SELECT count(*) FROM factory.hypotheses)
         ||' spend=\$'||COALESCE((SELECT round(sum(amount_usd),4) FROM factory.budget_ledger),0)
         ||' alpha_entries='||(SELECT count(*) FROM factory.alpha_wealth_ledger)
         ||' frontier_snapshots='||(SELECT count(*) FROM factory.frontier_snapshots)
         ||'  (alpha must stay flat during an observational pilot)';" 2>&1
  echo "════════ cycle $n end $(date -Is) ════════"
} >> "$LOG" 2>&1
