#!/bin/bash
# One-shot: restart bharat-analysis AFTER the 15:30 IST close so the ~90s
# Redpanda rebalance storm (documented gotcha) never lands mid-session.
# Needed because analysis reconstructs the NewsItem in transit and an older
# image silently DROPS the new `subcategory` field, so nothing reaches
# news_items until it runs the current types.py.
LOG="$(dirname "$0")/restart_analysis.log"
log(){ echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
cd /home/ec2-user/bharat-terminal || exit 1
log "waiting for 15:35 IST close window"
until [ "$(TZ=Asia/Kolkata date +%H%M | sed 's/^0*//')" -ge 1535 ]; do sleep 120; done
log "rebuilding analysis-worker"
docker compose build analysis-worker >>"$LOG" 2>&1
docker compose up -d --force-recreate analysis-worker >>"$LOG" 2>&1
log "restarted; waiting 180s for the rebalance to settle"
sleep 180
storm=$(docker logs bharat-analysis --since 2m 2>&1 | grep -ciE "rebalanc|non-joining")
log "rebalance lines in last 2m: $storm (expected to fall to 0)"
# Verify end-to-end: a BSE filing must flow ingestion -> analysis -> relay
for i in $(seq 1 60); do
  n=$(docker exec bharat-postgres psql -U bharat -d bharat_terminal -tA -c \
      "SELECT count(*) FROM news_items WHERE subcategory IS NOT NULL" 2>/dev/null)
  if [ "${n:-0}" -gt 0 ]; then
    log "SUCCESS: subcategory flowing, $n rows"
    docker exec bharat-postgres psql -U bharat -d bharat_terminal -tA -c \
      "SELECT subcategory || ' x' || count(*) FROM news_items WHERE subcategory IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 5" >>"$LOG" 2>&1
    exit 0
  fi
  sleep 60
done
log "NOTE: no subcategory rows after 60 min — filings may be sparse post-close; recheck tomorrow"
