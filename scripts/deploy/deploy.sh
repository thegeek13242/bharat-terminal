#!/usr/bin/env bash
#
# deploy.sh — provision Bharat Terminal on a GCP Compute Engine instance
# via `gcloud compute ssh / scp`. No raw SSH; gcloud handles auth, IAP,
# project context, and key management.
#
# Run LOCALLY from the repo root or anywhere:
#     ./scripts/deploy/deploy.sh [instance-name] [branch]
#
# Defaults to the current dev box:
#     instance: instance-20260508-044518
#     zone:     asia-south1-c
#
# Override per-call:
#     GCP_INSTANCE=my-vm GCP_ZONE=us-central1-a ./scripts/deploy/deploy.sh
#     GCP_PROJECT=other-project ./scripts/deploy/deploy.sh
#     ./scripts/deploy/deploy.sh my-vm feat/some-branch
#
# What it does:
#   1. gcloud SSH probe + system info
#   2. Installs Docker (if missing) + git, joins user to docker group
#   3. Clones (or fast-forwards) the repo at the requested branch
#   4. Copies your local .env up to the instance (must exist locally; never via git)
#   5. Builds all images
#   6. Starts infra (postgres, redis, redpanda, redpanda-init), runs migrations
#   7. Starts application services
#   8. Prints health snapshot + the gcloud-tunnel one-liner
#
# Idempotent: re-running on an already-bootstrapped instance updates the
# branch and re-deploys without re-installing Docker.
#
# Skip Angel One MCX or Anthropic? Just leave those env vars blank in .env;
# the adapters log "credentials not set" and skip silently.

set -euo pipefail

# ── arg parsing ──────────────────────────────────────────────────────────
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage: deploy.sh [instance-name] [branch]

Defaults:
  instance-name  $GCP_INSTANCE  or instance-20260508-044518
  branch         main
  zone           $GCP_ZONE      or asia-south1-c
  project        $GCP_PROJECT   or whatever `gcloud config get-value project` returns

Examples:
  deploy.sh
  deploy.sh instance-20260508-044518 feat/mcx-commodity-research-graph
  GCP_ZONE=us-central1-a deploy.sh my-other-vm
  REPO_URL=git@github.com:thegeek13242/bharat-terminal.git deploy.sh

Environment overrides:
  GCP_INSTANCE  GCE instance name (default: instance-20260508-044518)
  GCP_ZONE      GCE zone          (default: asia-south1-c)
  GCP_PROJECT   GCP project       (default: gcloud's active project)
  REPO_URL      git URL  (default: https://github.com/thegeek13242/bharat-terminal.git)
  TARGET_DIR    remote install path relative to $HOME (default: bharat-terminal)
  SKIP_BUILD    1 to skip docker compose build (only re-deploy services)
  RUN_SEED      1 to run kb-seed after migrations (NIFTY 50, ~10 min)
EOF
  exit 1
fi

GCP_INSTANCE="${1:-${GCP_INSTANCE:-instance-20260508-044518}}"
BRANCH="${2:-main}"
GCP_ZONE="${GCP_ZONE:-asia-south1-c}"
GCP_PROJECT="${GCP_PROJECT:-}"
REPO_URL="${REPO_URL:-https://github.com/thegeek13242/bharat-terminal.git}"
TARGET_DIR="${TARGET_DIR:-bharat-terminal}"
SKIP_BUILD="${SKIP_BUILD:-0}"
RUN_SEED="${RUN_SEED:-0}"

# Build a project flag if the user pinned one; otherwise rely on gcloud's
# active project.
gcloud_project_args=()
[ -n "$GCP_PROJECT" ] && gcloud_project_args=(--project "$GCP_PROJECT")

if ! command -v gcloud >/dev/null 2>&1; then
  echo "✗ gcloud not found. Install Google Cloud SDK first:"
  echo "    https://cloud.google.com/sdk/docs/install"
  exit 1
fi

# ── locate repo root + .env ──────────────────────────────────────────────
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")"
LOCAL_ENV="$REPO_ROOT/.env"
if [ ! -f "$LOCAL_ENV" ]; then
  echo "✗ ERROR: no .env at $LOCAL_ENV"
  echo "  Copy from .env.example and fill in secrets, then re-run."
  exit 1
fi

# Refuse to ship a .env that's *literally* the example (no edits → no creds)
if diff -q "$LOCAL_ENV" "$REPO_ROOT/.env.example" >/dev/null 2>&1; then
  echo "⚠  Your .env is identical to .env.example."
  echo "   Anthropic + Angel One adapters will be inactive on the remote."
  echo "   Continue anyway? [y/N] "
  read -r ans
  [[ "$ans" =~ ^[Yy]$ ]] || exit 1
fi

# ── gcloud wrappers ──────────────────────────────────────────────────────
# Each invocation is a fresh connection — no ControlMaster multiplexing
# concerns, so usermod -aG docker takes effect on the very next call.
run_remote() {
  gcloud compute ssh "$GCP_INSTANCE" \
    --zone="$GCP_ZONE" \
    ${gcloud_project_args[@]+"${gcloud_project_args[@]}"} \
    --command="$1"
}

run_remote_scp_to() {
  # $1 = local path, $2 = remote path (relative to $HOME)
  gcloud compute scp \
    --zone="$GCP_ZONE" \
    ${gcloud_project_args[@]+"${gcloud_project_args[@]}"} \
    "$1" "$GCP_INSTANCE:$2"
}

# Run a remote command that uses docker, dispatching through the right
# privilege wrapper based on DOCKER_MODE (set in the probe below).
DOCKER_MODE="direct"
run_remote_docker() {
  local cmd="$1"
  case "$DOCKER_MODE" in
    direct)
      run_remote "$cmd"
      ;;
    sg)
      local escaped="${cmd//\\/\\\\}"; escaped="${escaped//\"/\\\"}"
      run_remote "sg docker -c \"$escaped\""
      ;;
    sudo)
      local escaped="${cmd//\\/\\\\}"; escaped="${escaped//\"/\\\"}"
      run_remote "sudo bash -c \"$escaped\""
      ;;
    *)
      echo "✗ unknown DOCKER_MODE=$DOCKER_MODE" >&2
      return 1
      ;;
  esac
}

echo "──────────────────────────────────────────────────────────────────"
echo " instance: $GCP_INSTANCE"
echo " zone:     $GCP_ZONE"
echo " project:  ${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null || echo '(gcloud default)')}"
echo " branch:   $BRANCH"
echo " repo:     $REPO_URL"
echo "──────────────────────────────────────────────────────────────────"

# ── 1/7 SSH probe ────────────────────────────────────────────────────────
echo "==> [1/7] gcloud SSH probe"
run_remote 'echo "  connected as $(whoami) on $(hostname); $(uname -srm)"; \
  awk "/MemTotal/ {printf \"  RAM: %.1f GB\n\", \$2/1024/1024}" /proc/meminfo; \
  df -h / | awk "NR==2 {printf \"  disk: %s free of %s\n\", \$4, \$2}"'

# ── 2/7 Prereqs ──────────────────────────────────────────────────────────
echo "==> [2/7] Install prerequisites"
run_remote 'set -e
  if ! command -v docker >/dev/null; then
    echo "  installing docker…"
    curl -fsSL https://get.docker.com | sudo sh
  fi
  # Always (re-)apply group join — idempotent if already a member, and
  # critical when docker was installed by a previous deploy run that
  # never made it past this step.
  sudo usermod -aG docker "$(whoami)" || true
  # Ensure sg is available for the group-fallback path (passwd package
  # provides it on Debian/Ubuntu; on minimal images it may be missing).
  command -v sg  >/dev/null || sudo apt-get install -y -qq passwd >/dev/null 2>&1 || true
  command -v git >/dev/null || sudo apt-get install -y -qq git
  echo "  docker:  $(docker --version 2>/dev/null || echo MISSING)"
  echo "  compose: $(docker compose version --short 2>/dev/null || echo MISSING)"
  echo "  git:     $(git --version)"
  echo "  sg:      $(command -v sg 2>/dev/null || echo MISSING)"'

# Probe how to reach docker on the remote. Three-tier fallback:
#   tier 1: direct        — user already in active docker group
#   tier 2: sg docker -c  — member but session hasn't picked up the group
#   tier 3: sudo          — last resort; works on fresh images w/ NOPASSWD sudo
DOCKER_MODE="direct"
if run_remote 'docker ps >/dev/null 2>&1'; then
  DOCKER_MODE="direct"
  echo "  ✓ docker accessible directly (group active)"
elif run_remote 'command -v sg >/dev/null && sg docker -c "docker ps" >/dev/null 2>&1'; then
  DOCKER_MODE="sg"
  echo "  ⚠  docker group joined but not active; using 'sg docker -c' wrapper"
elif run_remote 'sudo -n docker ps >/dev/null 2>&1'; then
  DOCKER_MODE="sudo"
  echo "  ⚠  using passwordless sudo for docker (group join needs re-login to fully activate)"
else
  echo "✗ Cannot reach docker on the remote via any of: direct / sg docker / sudo docker"
  echo "  Diagnostics:"
  run_remote '
    echo "  - whoami:        $(whoami)"
    echo "  - docker bin:    $(command -v docker || echo MISSING)"
    echo "  - sg bin:        $(command -v sg     || echo MISSING)"
    echo "  - in docker grp: $(id -nG | tr " " "\n" | grep -c "^docker$") (need 1)"
    echo "  - sudo:          $(sudo -n true 2>&1 | head -1 || echo NOPASSWD-OK)"
    echo "  - docker.sock:   $(ls -la /var/run/docker.sock 2>&1 || echo MISSING)"
  '
  echo
  echo "  Try: gcloud compute instances reset $GCP_INSTANCE --zone=$GCP_ZONE"
  echo "  …or: gcloud compute ssh $GCP_INSTANCE --zone=$GCP_ZONE --command='sudo systemctl start docker'"
  echo "  Then re-run this script."
  exit 1
fi

# ── 3/7 Repo ─────────────────────────────────────────────────────────────
echo "==> [3/7] Clone or update repo (branch: $BRANCH)"
run_remote "set -e
  if [ -d \"$TARGET_DIR/.git\" ]; then
    cd \"$TARGET_DIR\"
    git fetch origin
    git checkout \"$BRANCH\"
    git pull --ff-only
  else
    git clone --branch \"$BRANCH\" \"$REPO_URL\" \"$TARGET_DIR\"
    cd \"$TARGET_DIR\"
  fi
  echo \"  HEAD: \$(git log -1 --oneline)\""

# ── 4/7 .env ─────────────────────────────────────────────────────────────
echo "==> [4/7] Copy .env (secrets — never via git)"
run_remote_scp_to "$LOCAL_ENV" "$TARGET_DIR/.env"
run_remote "chmod 600 $TARGET_DIR/.env && echo '  ✓ .env mode 600'"

# ── 5/7 Build ────────────────────────────────────────────────────────────
if [ "$SKIP_BUILD" = "1" ]; then
  echo "==> [5/7] Build images (SKIPPED via SKIP_BUILD=1)"
else
  echo "==> [5/7] Build images (10–15 min on first run)"
  run_remote_docker "cd $TARGET_DIR && docker compose build"
fi

# ── 6/7 Infra + migrations ───────────────────────────────────────────────
echo "==> [6/7] Bring up infra and run migrations"
# Substitute RUN_SEED locally and evaluate the conditional remote-side.
# Avoid parens in echoed messages — the wrap → unwrap trip strips quoting
# and the remote shell would parse '(' as a subshell start.
run_remote_docker "set -e
  cd $TARGET_DIR
  docker compose up -d postgres redis redpanda
  echo '  waiting for redpanda to be healthy...'
  for i in \$(seq 1 30); do
    if docker compose ps redpanda --format json 2>/dev/null | grep -q 'healthy'; then break; fi
    sleep 2
  done
  docker compose up -d redpanda-init
  echo '  running migrations...'
  docker compose run --rm kb-migrate
  if [ '$RUN_SEED' = '1' ]; then
    echo '  seeding NIFTY 50 universe -- this takes about 10 minutes...'
    docker compose run --rm kb-seed
  else
    echo '  skipping kb-seed -- set RUN_SEED=1 to populate NIFTY 50'
  fi"

# ── 7a/8 Install systemd auto-start unit ─────────────────────────────────
# Without this, a VM stop→start cycle leaves the stack down (Docker daemon
# starts, but `restart: unless-stopped` only restarts containers that were
# running before the daemon shutdown — not ones explicitly `compose down`d
# or new services added since last boot). The unit is idempotent: drops the
# template, substitutes WorkingDirectory + User, enables, kicks the start.
echo "==> [7a/8] Install systemd auto-start unit"
SYSTEMD_UNIT_LOCAL="$(dirname "$0")/bharat-terminal.service"
if [ ! -f "$SYSTEMD_UNIT_LOCAL" ]; then
  echo "  ✗ unit template missing at $SYSTEMD_UNIT_LOCAL"
  exit 1
fi
gcloud compute scp --zone="$GCP_ZONE" ${gcloud_project_args[@]+"${gcloud_project_args[@]}"} \
  "$SYSTEMD_UNIT_LOCAL" "$GCP_INSTANCE:/tmp/bharat-terminal.service.tmpl"
run_remote "set -e
  REMOTE_USER=\$(whoami)
  REMOTE_DIR=\$HOME/$TARGET_DIR
  sed -e \"s|{{TARGET_DIR}}|\$REMOTE_DIR|g\" \
      -e \"s|{{USER}}|\$REMOTE_USER|g\" \
      /tmp/bharat-terminal.service.tmpl | sudo tee /etc/systemd/system/bharat-terminal.service >/dev/null
  rm /tmp/bharat-terminal.service.tmpl
  sudo systemctl daemon-reload
  sudo systemctl enable bharat-terminal.service
  echo '  ✓ systemd unit installed and enabled'
  sudo systemctl is-enabled bharat-terminal.service"

# ── 7b/8 Start application services ──────────────────────────────────────
echo "==> [7b/8] Start application services"
# kb-monitor runs the event-driven KB updater: polls NSE board-meetings
# calendar at 09:00 IST, syncs companies whose results posted at 20:00 +
# 23:00 IST, refreshes bhavcopy at 16:30 weekdays, weekly Nifty500 sweep
# Saturday 02:00. Without this the KB goes stale after the initial seed.
run_remote_docker "set -e
  cd $TARGET_DIR
  docker compose up -d \\
    kb-service kb-monitor \\
    ingestion-worker analysis-worker sentiment-worker commodity-engine fno-engine \\
    api-gateway frontend \\
    prometheus grafana
  sleep 10
  docker compose ps"

# ── done ─────────────────────────────────────────────────────────────────
cat <<EOF

────────────────────────────────────────────────────────────────────────
✓ Bharat Terminal deployed to $GCP_INSTANCE (zone $GCP_ZONE, branch $BRANCH)

The services bind to localhost on the remote. Tunnel ports back to your
laptop with this one-liner (paste into a new terminal):

  gcloud compute ssh $GCP_INSTANCE --zone=$GCP_ZONE \\
      ${GCP_PROJECT:+--project=$GCP_PROJECT \\
      }-- \\
      -L 3000:localhost:3000 \\
      -L 8000:localhost:8000 \\
      -L 8080:localhost:8080 \\
      -L 3001:localhost:3001 \\
      -L 9090:localhost:9090

Then open in your browser:
  • Frontend       http://localhost:3000
  • API gateway    http://localhost:8000
  • Redpanda UI    http://localhost:8080
  • Grafana        http://localhost:3001    (admin/bharat)
  • Prometheus     http://localhost:9090

Useful follow-ups:
  gcloud compute ssh $GCP_INSTANCE --zone=$GCP_ZONE --command='cd $TARGET_DIR && docker compose logs -f sentiment-worker'
  gcloud compute ssh $GCP_INSTANCE --zone=$GCP_ZONE --command='cd $TARGET_DIR && docker compose ps'
  gcloud compute ssh $GCP_INSTANCE --zone=$GCP_ZONE --command='cd $TARGET_DIR && docker compose run --rm kb-seed'
  gcloud compute ssh $GCP_INSTANCE --zone=$GCP_ZONE --command='cd $TARGET_DIR && docker compose --profile research run --rm research-runner seed'
────────────────────────────────────────────────────────────────────────
EOF
