#!/usr/bin/env bash
# Daily signal evaluator — ingest new trade signals + evaluate forward returns.
# Self-limiting: auto-stops after END_DATE so the "run for 1 week" job cleans
# itself up (remove the crontab line too once done).
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin

END_DATE="2026-07-07"            # last day this should run (1 week from 2026-06-30)
TODAY="$(date +%Y-%m-%d)"
if [[ "$TODAY" > "$END_DATE" ]]; then
    exit 0
fi

cd /home/aviralverma/bharat-terminal
echo "===== signal-eval run $(date -u +'%Y-%m-%dT%H:%M:%SZ') ====="
docker compose run --rm --no-deps kb-migrate \
    python -m bharat_terminal.research.score_signals all --since-days 30
