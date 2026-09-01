#!/bin/bash
# Single-instance guard (2026-08-22): duplicate loops appeared after session
# restarts and doubled quota burn / alerts. flock on a lock file next to the
# script makes a second launch exit immediately.
exec 9>"$(dirname "$0")/.$(basename "$0").lock"
flock -n 9 || { echo "$(basename "$0") already running — exiting"; exit 0; }
# E-series backfill budget watcher v2 ($900 hard, warn $720). Persistent
# copy (scripts/ops) after two /tmp wipes by instance crashes. Estimator:
# structure priced at Haiku ($1/$5, ~4k out) ONLY for structure_status='ok'
# (structured_at records attempts, not successes); guidance priced at
# Sonnet 4.5 ($3/$15, ~3k out) per doc that has guidance rows. Transcript
# LLM calls are not in llm_call_log, so this estimate is the budget's
# source of truth. On breach: alert via guardrails AND pause the watcher.
LOG="$(dirname "$0")/budget_watch.log"
BUDGET=900; WARN=720; warned=0; prev_g=-1
echo "$(date -u +%FT%TZ) budget watcher v2 started (warn=\$$WARN hard=\$$BUDGET)" >> "$LOG"
while true; do
  row=$(docker exec bharat-postgres psql -U bharat -d bharat_terminal -tA -c "
    WITH t AS (SELECT ct.char_count, ct.structure_model, (ct.structure_status='ok') AS structured,
               EXISTS (SELECT 1 FROM transcript_guidance tg WHERE tg.transcript_id=ct.id) AS has_guidance
               FROM concall_transcripts ct WHERE ct.news_id LIKE 'BSE_ARCH_%')
    SELECT coalesce(round(sum(CASE WHEN structured AND structure_model LIKE '%sonnet%' THEN ((char_count/4.0+1500)*3.00+4000*15.00)/1e6 WHEN structured THEN ((char_count/4.0+1500)*1.00+4000*5.00)/1e6 ELSE 0 END
                            + CASE WHEN has_guidance THEN ((char_count/4.0+1500)*3.00+3000*15.00)/1e6 ELSE 0 END)::numeric,2),0)
      || '|' || count(*) || '|' || count(*) FILTER (WHERE has_guidance)
      || '|' || (SELECT count(*) FROM concall_transcripts c2 WHERE c2.parse_status='ok' AND c2.structure_status='ok'
                 AND NOT EXISTS (SELECT 1 FROM transcript_guidance tg WHERE tg.transcript_id=c2.id))
    FROM t;" 2>>"$LOG")
  IFS='|' read -r est done guided sweep_left <<< "$row"
  ts=$(date -u +%FT%TZ)
  echo "$ts est_usd=$est backfill_done=$done guided=$guided sweep_remaining=$sweep_left" >> "$LOG"
  if python3 -c "exit(0 if float('${est:-0}') >= $BUDGET else 1)"; then
    docker exec bharat-kb-monitor python -c "from bharat_terminal.alerts import get_monitor; get_monitor().alert_now('BACKFILL_BUDGET_BREACH', 'E-series backfill+sweep est spend \$${est} >= \$${BUDGET}. transcript-watcher PAUSED; stop the remediation runner too. guided=${guided} sweep_remaining=${sweep_left}.')" >> "$LOG" 2>&1
    docker pause bharat-transcript-watcher >> "$LOG" 2>&1
    echo "$ts BREACH: est \$${est} — watcher paused, exiting" >> "$LOG"; exit 2
  fi
  if [ "$warned" = "0" ] && python3 -c "exit(0 if float('${est:-0}') >= $WARN else 1)"; then
    warned=1
    docker exec bharat-kb-monitor python -c "from bharat_terminal.alerts import get_monitor; get_monitor().alert_now('BACKFILL_BUDGET_WARN', 'E-series backfill+sweep est spend \$${est} crossed 80% of \$${BUDGET}. guided=${guided} sweep_remaining=${sweep_left}.')" >> "$LOG" 2>&1
    echo "$ts WARN fired at \$${est}" >> "$LOG"
  fi
  if [ "${sweep_left:-1}" = "0" ] && [ "$guided" = "$prev_g" ]; then
    echo "$ts COMPLETE: est \$${est}, guided=${guided}" >> "$LOG"; exit 0
  fi
  prev_g=$guided; sleep 900
done
