#!/usr/bin/env bash
#
# migrate-volumes.sh — OPTIONAL: ship local docker volumes to the remote.
#
# Use this only if you want to preserve local DB / Grafana / Redpanda state
# instead of starting fresh. For a dev-server transfer, fresh is usually
# cleaner — the kb-seed flow rebuilds the NIFTY 50 universe in ~10 min.
#
# Run LOCALLY:
#     ./scripts/deploy/migrate-volumes.sh <user@host[:port]>
#
# Local docker volumes are typically named "<project>_<volume>", where
# <project> is the directory containing docker-compose.yml. We detect both
# local and remote project names automatically.

set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <user@host[:port]>"
  exit 1
fi

REMOTE_RAW="$1"
host="${REMOTE_RAW%:*}"
port="${REMOTE_RAW##*:}"
[ "$port" = "$REMOTE_RAW" ] && port=22

ssh_opts=(-p "$port" -o StrictHostKeyChecking=accept-new)
scp_opts=(-P "$port" -o StrictHostKeyChecking=accept-new)

REMOTE_DIR="${TARGET_DIR:-bharat-terminal}"
LOCAL_PROJECT="$(basename "$(git rev-parse --show-toplevel)")"
REMOTE_PROJECT="$(basename "$REMOTE_DIR")"

LOGICAL=(postgres_data redpanda_data prometheus_data grafana_data)

echo "==> Volume migration"
echo "    local project:  $LOCAL_PROJECT"
echo "    remote project: $REMOTE_PROJECT"

WORK=$(mktemp -d)
trap "rm -rf $WORK" EXIT

echo "==> Stopping local stack for consistent snapshot"
docker compose down

# Archive each volume that exists locally
for name in "${LOGICAL[@]}"; do
  src="${LOCAL_PROJECT}_${name}"
  if docker volume inspect "$src" >/dev/null 2>&1; then
    echo "  → archiving $src"
    docker run --rm -v "$src:/from:ro" -v "$WORK:/to" alpine \
      tar -czf "/to/${name}.tar.gz" -C /from .
  else
    echo "  - $src not present locally, skipping"
  fi
done

# Stop the remote stack before swapping data
echo "==> Stopping remote stack"
ssh "${ssh_opts[@]}" "$host" "cd $REMOTE_DIR && docker compose down" || true

echo "==> Uploading archives"
for f in "$WORK"/*.tar.gz; do
  [ -f "$f" ] || continue
  scp "${scp_opts[@]}" "$f" "$host:/tmp/"
done

# Restore each archive into the remote-named volume
for name in "${LOGICAL[@]}"; do
  archive="${name}.tar.gz"
  [ -f "$WORK/$archive" ] || continue
  dst="${REMOTE_PROJECT}_${name}"
  echo "  → restoring into $dst"
  ssh "${ssh_opts[@]}" "$host" "set -e
    docker volume create '$dst' >/dev/null
    docker run --rm -v '$dst':/to -v /tmp:/from alpine sh -c '
      rm -rf /to/* /to/..?* /to/.[!.]* 2>/dev/null || true
      tar -xzf /from/$archive -C /to'
    rm /tmp/$archive"
done

echo "==> Restarting remote stack"
ssh "${ssh_opts[@]}" "$host" "cd $REMOTE_DIR && docker compose up -d"

echo "✓ Volume migration complete"
