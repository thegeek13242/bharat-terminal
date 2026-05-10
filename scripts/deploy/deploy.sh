#!/usr/bin/env bash
#
# deploy.sh — provision Bharat Terminal on a fresh remote dev server.
#
# Run LOCALLY from the repo root or anywhere:
#     ./scripts/deploy/deploy.sh <user@host[:port]> [branch]
#
# What it does:
#   1. SSH probe + system info
#   2. Installs Docker (if missing) + git, joins user to docker group
#   3. Clones (or fast-forwards) the repo at the requested branch
#   4. Copies your local .env up to the server (must exist locally; never via git)
#   5. Builds all images
#   6. Starts infra (postgres, redis, redpanda, redpanda-init), runs migrations
#   7. Starts application services (kb, ingestion, analysis, sentiment,
#      commodity, api, frontend, prometheus, grafana)
#   8. Prints health snapshot + the SSH-tunnel one-liner
#
# Idempotent: re-running on an already-bootstrapped host updates the branch
# and re-deploys without re-installing Docker.
#
# Skip Angel One MCX or Anthropic? Just leave those env vars blank in .env;
# the adapters log "credentials not set" and skip silently.

set -euo pipefail

# ── arg parsing ──────────────────────────────────────────────────────────
if [ $# -lt 1 ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage: deploy.sh <user@host[:port]> [branch]

Examples:
  deploy.sh ubuntu@dev.example.com
  deploy.sh ubuntu@1.2.3.4:2222 feat/mcx-commodity-research-graph
  REPO_URL=git@github.com:thegeek13242/bharat-terminal.git deploy.sh ubuntu@host

Environment overrides:
  REPO_URL      git URL (default: https://github.com/thegeek13242/bharat-terminal.git)
  TARGET_DIR    remote install path relative to $HOME (default: bharat-terminal)
  SKIP_BUILD    1 to skip docker compose build (only re-deploy services)
  SKIP_SEED     1 to skip the optional kb-seed step (default: skipped anyway)
  RUN_SEED      1 to run kb-seed after migrations (NIFTY 50, ~10 min)
EOF
  exit 1
fi

REMOTE_RAW="$1"
BRANCH="${2:-main}"
REPO_URL="${REPO_URL:-https://github.com/thegeek13242/bharat-terminal.git}"
TARGET_DIR="${TARGET_DIR:-bharat-terminal}"
SKIP_BUILD="${SKIP_BUILD:-0}"
RUN_SEED="${RUN_SEED:-0}"

host="${REMOTE_RAW%:*}"
port="${REMOTE_RAW##*:}"
[ "$port" = "$REMOTE_RAW" ] && port=22

# Force fresh SSH connections per call. Without this, ~/.ssh/config that
# enables ControlMaster will multiplex everything over one TCP connection
# authenticated *before* `usermod -aG docker` ran in step 2 — so later
# steps still see the old group set and fail with "permission denied …
# /var/run/docker.sock".
ssh_opts=(-p "$port" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=60 \
          -o ControlMaster=no -o ControlPath=none)
scp_opts=(-P "$port" -o StrictHostKeyChecking=accept-new \
          -o ControlMaster=no -o ControlPath=none)

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

run_remote() { ssh "${ssh_opts[@]}" "$host" "$@"; }

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
      # sg docker -c needs a single quoted string; escape backslashes and
      # double quotes so the inner script survives the trip.
      local escaped="${cmd//\\/\\\\}"; escaped="${escaped//\"/\\\"}"
      run_remote "sg docker -c \"$escaped\""
      ;;
    sudo)
      # Run the script as root via sudo bash -c. Same escaping concerns.
      local escaped="${cmd//\\/\\\\}"; escaped="${escaped//\"/\\\"}"
      run_remote "sudo bash -c \"$escaped\""
      ;;
    *)
      echo "✗ unknown DOCKER_MODE=$DOCKER_MODE" >&2
      return 1
      ;;
  esac
}

# ── 1/7 SSH probe ────────────────────────────────────────────────────────
echo "==> [1/7] SSH probe"
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
#   tier 2: sg docker -c  — user is a member but the SSH session hasn't
#                           picked up the group yet (no re-login required)
#   tier 3: sudo          — last resort; works on fresh cloud images with
#                           passwordless sudo
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
  echo "  Try one of:"
  echo "    1) Log out of all SSH sessions and reconnect, then re-run this script."
  echo "    2) Run 'sudo systemctl start docker' on the remote, then re-run."
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
scp "${scp_opts[@]}" "$LOCAL_ENV" "$host:$TARGET_DIR/.env"
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
run_remote_docker "set -e
  cd $TARGET_DIR
  docker compose up -d postgres redis redpanda
  echo '  waiting for redpanda to be healthy…'
  for i in \$(seq 1 30); do
    if docker compose ps redpanda --format json 2>/dev/null | grep -q '\"Health\":\"healthy\"'; then break; fi
    sleep 2
  done
  docker compose up -d redpanda-init
  echo '  running migrations…'
  docker compose run --rm kb-migrate
  $( [ "$RUN_SEED" = "1" ] && echo 'echo \"  seeding NIFTY 50 (this takes ~10 min)…\"; docker compose run --rm kb-seed' || echo 'echo \"  (skipping kb-seed — pass RUN_SEED=1 to populate NIFTY 50)\"' )"

# ── 7/7 App services ─────────────────────────────────────────────────────
echo "==> [7/7] Start application services"
run_remote_docker "set -e
  cd $TARGET_DIR
  docker compose up -d \\
    kb-service \\
    ingestion-worker analysis-worker sentiment-worker commodity-engine \\
    api-gateway frontend \\
    prometheus grafana
  sleep 10
  docker compose ps"

# ── done ─────────────────────────────────────────────────────────────────
cat <<EOF

────────────────────────────────────────────────────────────────────────
✓ Bharat Terminal deployed to $host:$port (branch $BRANCH)

The services bind to localhost on the remote. Tunnel ports back to your
laptop with this one-liner (paste into a new terminal):

  ssh -p $port \\
      -L 3000:localhost:3000 \\
      -L 8000:localhost:8000 \\
      -L 8080:localhost:8080 \\
      -L 3001:localhost:3001 \\
      -L 9090:localhost:9090 \\
      $host

Then open in your browser:
  • Frontend       http://localhost:3000
  • API gateway    http://localhost:8000
  • Redpanda UI    http://localhost:8080
  • Grafana        http://localhost:3001    (admin/bharat)
  • Prometheus     http://localhost:9090

Useful follow-ups:
  ssh $host 'cd $TARGET_DIR && docker compose logs -f sentiment-worker'
  ssh $host 'cd $TARGET_DIR && docker compose ps'
  ssh $host 'cd $TARGET_DIR && docker compose run --rm kb-seed'   # NIFTY 50 backfill
  ssh $host 'cd $TARGET_DIR && docker compose --profile research run --rm research-runner seed'
────────────────────────────────────────────────────────────────────────
EOF
