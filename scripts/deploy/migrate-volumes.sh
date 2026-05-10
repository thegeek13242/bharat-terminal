#!/usr/bin/env bash
#
# migrate-volumes.sh — OPTIONAL: ship local docker volumes to a GCP
# Compute Engine instance via gcloud compute scp/ssh.
#
# Use this only if you want to preserve local DB / Grafana / Redpanda state
# instead of starting fresh. For a dev-server transfer, fresh is usually
# cleaner — the kb-seed flow rebuilds the NIFTY 50 universe in ~10 min.
#
# Run LOCALLY:
#     ./scripts/deploy/migrate-volumes.sh [instance-name]
#
# Defaults: instance=instance-20260508-044518, zone=asia-south1-c.
# Override via GCP_INSTANCE / GCP_ZONE / GCP_PROJECT env vars.

set -euo pipefail

GCP_INSTANCE="${1:-${GCP_INSTANCE:-instance-20260508-044518}}"
GCP_ZONE="${GCP_ZONE:-asia-south1-c}"
GCP_PROJECT="${GCP_PROJECT:-}"

gcloud_project_args=()
[ -n "$GCP_PROJECT" ] && gcloud_project_args=(--project "$GCP_PROJECT")

REMOTE_DIR="${TARGET_DIR:-bharat-terminal}"
LOCAL_PROJECT="$(basename "$(git rev-parse --show-toplevel)")"
REMOTE_PROJECT="$(basename "$REMOTE_DIR")"

LOGICAL=(postgres_data redpanda_data prometheus_data grafana_data)

run_remote() {
  gcloud compute ssh "$GCP_INSTANCE" --zone="$GCP_ZONE" ${gcloud_project_args[@]+"${gcloud_project_args[@]}"} --command="$1"
}

run_remote_scp_to() {
  gcloud compute scp --zone="$GCP_ZONE" ${gcloud_project_args[@]+"${gcloud_project_args[@]}"} "$1" "$GCP_INSTANCE:$2"
}

echo "==> Volume migration"
echo "    instance:       $GCP_INSTANCE  (zone $GCP_ZONE)"
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
run_remote "cd $REMOTE_DIR && docker compose down" || true

echo "==> Uploading archives"
for f in "$WORK"/*.tar.gz; do
  [ -f "$f" ] || continue
  run_remote_scp_to "$f" "/tmp/"
done

# Restore each archive into the remote-named volume
for name in "${LOGICAL[@]}"; do
  archive="${name}.tar.gz"
  [ -f "$WORK/$archive" ] || continue
  dst="${REMOTE_PROJECT}_${name}"
  echo "  → restoring into $dst"
  run_remote "set -e
    docker volume create '$dst' >/dev/null
    docker run --rm -v '$dst':/to -v /tmp:/from alpine sh -c '
      rm -rf /to/* /to/..?* /to/.[!.]* 2>/dev/null || true
      tar -xzf /from/$archive -C /to'
    rm /tmp/$archive"
done

echo "==> Restarting remote stack"
run_remote "cd $REMOTE_DIR && docker compose up -d"

echo "✓ Volume migration complete"
