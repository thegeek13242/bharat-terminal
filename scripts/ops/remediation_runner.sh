#!/bin/bash
# Single-instance guard (2026-08-22): duplicate loops appeared after session
# restarts and doubled quota burn / alerts. flock on a lock file next to the
# script makes a second launch exit immediately.
exec 9>"$(dirname "$0")/.$(basename "$0").lock"
flock -n 9 || { echo "$(basename "$0") already running — exiting"; exit 0; }
# Path-B remediation runner (founder decision 2026-08-20): restructure the
# structure-failed docs on HAIKU (daily-quota-paced) and sweep guidance
# behind them on SONNET 4.5 — alternating chunks, OFF-MARKET-HOURS ONLY so
# the live pipeline's Haiku (stage2) / Sonnet (filing_summary) needs are
# never contended during trading. Loops until both candidate sets are
# empty. Soft budget stop at $870 (hard $900 lives in budget_watch.sh).
# Persistent copy (scripts/ops) after two /tmp wipes by instance crashes.
OPS="$(cd "$(dirname "$0")" && pwd)"
LOG="$OPS/remediation_runner.log"
REPO=/home/ec2-user/bharat-terminal
cd "$REPO" || exit 1
log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
# Blocked window widened to 05:00-18:00 IST on weekdays (2026-08-21): the
# Haiku and Sonnet 4.5 daily-token quotas are SHARED with the live pipeline
# (stage2 / filing_summary) — this morning the overnight sweep left
# filing_summary 429'ing for an hour. Stopping at 05:00 gives the buckets
# hours to recover before the 09:15 open.
# 2026-09-01 (founder option 1): restructure moved BACK to Haiku for cost
# ($0.05/doc vs Sonnet-1M $0.16 — Path B would have hit the $900 cap ~Sep 6
# with ~1,000 docs undone). Haiku's day-bucket is only safe now that the
# guidance sweep has drained off it, so weekdays are restricted to the
# 00:00-05:00 IST window: AFTER the evening filing wave that caused the
# Aug 24/25 stage2 429s, and well BEFORE market open. Weekends unrestricted.
in_market_window() { local d h; d=$(TZ=Asia/Kolkata date +%u); h=$(TZ=Asia/Kolkata date +%H); [ "$d" -le 5 ] && [ "$h" -ge 5 ]; }
# One restructure chunk + one guidance chunk per NIGHT (window id = the IST
# date the night started on), so a weekend never turns into a 60-hour
# quota-draining loop.
night_id() { local h; h=$(TZ=Asia/Kolkata date +%H); if [ "$h" -ge 18 ]; then TZ=Asia/Kolkata date +%F; else TZ=Asia/Kolkata date -d "yesterday" +%F; fi; }
NIGHT_MARK="$OPS/.last_night_done"
# Persisted across restarts so a relaunch never re-runs a night's chunks.
last_night_done="$(cat "$NIGHT_MARK" 2>/dev/null || true)"
budget_soft_ok() { local last; last=$(grep -oE 'est_usd=[0-9.]+' "$OPS/budget_watch.log" 2>/dev/null | tail -1 | cut -d= -f2); python3 -c "exit(0 if float('${last:-0}') < 870 else 1)"; }
counts() { docker exec bharat-postgres psql -U bharat -d bharat_terminal -tA -c "
    SELECT (SELECT count(*) FROM concall_transcripts WHERE parse_status='ok' AND structure_status != 'ok')
      || '|' || (SELECT count(*) FROM concall_transcripts ct WHERE parse_status='ok' AND structure_status='ok'
                 AND NOT EXISTS (SELECT 1 FROM transcript_guidance tg WHERE tg.transcript_id=ct.id));"; }
log "runner started (Path B: Haiku restructure + Sonnet guidance, off-hours only)"
while docker ps --format '{{.Names}}' | grep -q "transcript-watcher-run"; do log "waiting: a transcript one-shot container is still running"; sleep 300; done
while true; do
  IFS='|' read -r restruct_left guidance_left <<< "$(counts)"
  log "state: restructure_left=$restruct_left guidance_left=$guidance_left"
  if [ "${restruct_left:-1}" = "0" ] && [ "${guidance_left:-1}" = "0" ]; then log "COMPLETE: corpus fully structured + guidance-extracted"; exit 0; fi
  if in_market_window; then sleep 900; continue; fi
  # Cap to one chunk-pair per night ONLY when tomorrow is a TRADING day
  # (protect its shared Sonnet/Haiku daily quota). Weekend nights run freely
  # — quota resets daily, no live pipeline competes; that is where the sweep
  # does bulk work (2026-08-22: weekday guidance ~100/600, weekend ~all OK).
  tomorrow_trading() { local d; d=$(TZ=Asia/Kolkata date -d tomorrow +%u); [ "$d" -le 5 ]; }
  if tomorrow_trading && [ "$(night_id)" = "$last_night_done" ]; then sleep 900; continue; fi
  if ! budget_soft_ok; then log "SOFT BUDGET STOP (est >= \$870) — exiting"; exit 3; fi
  last_night_done="$(night_id)"; echo "$last_night_done" > "$NIGHT_MARK"; log "night $last_night_done: running one restructure + one guidance chunk"
  IFS='|' read -r _ guidance_left <<< "$(counts)"
  if [ "${guidance_left:-0}" != "0" ]; then
    # Guidance on Sonnet 4.5 via the 1M-context beta (2026-08-22): Bedrock
    # meters those requests under the separate "1M Context Length" quota
    # (54M tokens/day, RPM 1 until case 178741930400662-class increases
    # land) — so the sweep no longer drains the live filing_summary bucket.
    # RPM 1 => ~60 docs/hour; 500 docs fits the night window.
    out=$(docker compose run --rm --no-deps -e TRANSCRIPT_GUIDANCE_MODEL=claude-sonnet-4-5-20250929 \
          -e BEDROCK_ANTHROPIC_BETA_CLAUDE_SONNET_4_5_20250929=context-1m-2025-08-07 -e BEDROCK_RPM_LIMIT=1 \
          --entrypoint python transcript-watcher -m bharat_terminal.kb.sync.transcript_sync --extract-guidance --limit 500 2>&1 | grep -E "guidance backfill" | tail -1)
    log "guidance chunk: $out"
  fi
  if [ "${restruct_left:-0}" != "0" ]; then
    # Restructure on Sonnet 4.5 via the 1M-context bucket (founder option 2,
    # 2026-08-26): frees Haiku entirely for the live pipeline (the Mon/Tue
    # evening stage2 429s came from restructure draining the Haiku day
    # bucket). RPM 1 => ~60 docs/hour; 550 fits a night after guidance.
    out=$(docker compose run --rm --no-deps -e BEDROCK_RPM_LIMIT=3 \
          --entrypoint python transcript-watcher \
          -m bharat_terminal.kb.sync.transcript_sync --restructure --limit 400 2>&1 | grep -E "restructure|DONE" | tail -2)
    log "restructure chunk (haiku, 00-05 IST window): $out"
    if echo "$out" | grep -qE "'structured': 0[,}]" && echo "$out" | grep -qE "'structure_failed': [1-9]"; then
      log "Haiku quota dry — backing off 2h"; sleep 7200; continue
    fi
  fi
  sleep 60
done
