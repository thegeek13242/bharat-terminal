# Remote deploy (GCP)

Two scripts for shipping Bharat Terminal to a Google Cloud Platform
Compute Engine instance via `gcloud compute ssh / scp`. No raw SSH —
gcloud handles auth, IAP tunnelling, project context, and key management.

## Prerequisites locally

- [`gcloud`](https://cloud.google.com/sdk/docs/install) installed and
  authenticated (`gcloud auth login`, `gcloud config set project <id>`).
- A filled `.env` at the repo root (the script refuses to ship the
  `.env.example` defaults without confirmation).
- `git` and `docker` CLI.

## Prerequisites on the GCE instance

- Ubuntu / Debian image (the script uses apt). For RHEL/Fedora, swap the
  package manager line in `deploy.sh` step 2/7.
- ≥ 8 GB RAM, ≥ 30 GB disk (sentence-transformers model + Python images
  + Redpanda + Postgres add up).
- Outbound network path to GitHub, Anthropic API, Polymarket Gamma, and
  (optionally) Angel One. GCE default egress is sufficient.

The script handles Docker installation; you don't need to provision
anything beyond the base image.

## One-command transfer

The default target is the live dev VM:

```bash
./scripts/deploy/deploy.sh
# → instance=instance-20260508-044518, zone=asia-south1-c, branch=main
```

Override the instance / zone / branch:

```bash
./scripts/deploy/deploy.sh instance-20260508-044518 feat/mcx-commodity-research-graph
GCP_ZONE=us-central1-a ./scripts/deploy/deploy.sh my-other-vm
GCP_PROJECT=other-project-id ./scripts/deploy/deploy.sh
```

To also run `kb-seed` (NIFTY 50 backfill, ~10 min) on first boot:

```bash
RUN_SEED=1 ./scripts/deploy/deploy.sh
```

To skip the `docker compose build` step on a re-deploy (useful when only
config changed):

```bash
SKIP_BUILD=1 ./scripts/deploy/deploy.sh
```

## What the script does (in order)

1. **gcloud SSH probe** — confirms reachability, prints RAM/disk on the
   remote.
2. **Prerequisites** — installs Docker via `get.docker.com` if missing;
   installs git; joins your user to the `docker` group; ensures `sg` is
   present for the group-fallback path.
3. **Docker access probe** — three-tier fallback (direct → `sg docker -c`
   → `sudo`) so the script proceeds without requiring you to log out and
   back in after the group join.
4. **Repo** — clones (or `git pull --ff-only`s) at the requested branch.
5. **`.env`** — copies your local `.env` to `<TARGET>/.env`, mode 600.
   Never goes through git.
6. **Build** — `docker compose build` (skippable with `SKIP_BUILD=1`).
7. **Infra + migrations** — postgres, redis, redpanda, redpanda-init,
   then `kb-migrate`. Optionally `kb-seed` if `RUN_SEED=1`.
8. **App services** — kb-service, ingestion-worker, analysis-worker,
   sentiment-worker, commodity-engine, api-gateway, frontend, prometheus,
   grafana.

## Reaching the UI

Services bind to `localhost` on the remote — the script does **not** open
GCE firewall ports (intentional). Tunnel them back to your laptop via
`gcloud compute ssh`:

```bash
gcloud compute ssh instance-20260508-044518 --zone=asia-south1-c -- \
    -L 3000:localhost:3000 \
    -L 8000:localhost:8000 \
    -L 8080:localhost:8080 \
    -L 3001:localhost:3001 \
    -L 9090:localhost:9090
```

Note the `--` — anything after it is passed through to the underlying ssh
command, which is how gcloud lets you set `-L` port forwards.

Then open `http://localhost:3000` etc. in your browser.

For a permanent public URL, add an HTTPS load balancer + IAP — that's a
separate concern and shouldn't be wired into this dev-deploy flow.

## Volume migration (optional)

`deploy.sh` always provisions a **fresh** Postgres / Redpanda / Grafana /
Prometheus. If you specifically want to preserve your local data, run
this **after** the first `deploy.sh` succeeds:

```bash
./scripts/deploy/migrate-volumes.sh
```

This stops both stacks, archives each named volume locally, ships them
via `gcloud compute scp`, and restores into the remote-named volumes.
Both sides are restarted at the end.

For most dev-server transfers, fresh is fine — `docker compose run --rm
kb-seed` rebuilds the NIFTY 50 universe in ~10 min.

## Useful follow-ups

```bash
ZONE_FLAG="--zone=asia-south1-c"
INSTANCE="instance-20260508-044518"

# Tail a single service
gcloud compute ssh "$INSTANCE" $ZONE_FLAG --command='cd bharat-terminal && docker compose logs -f sentiment-worker'

# Status snapshot
gcloud compute ssh "$INSTANCE" $ZONE_FLAG --command='cd bharat-terminal && docker compose ps'

# Run the NIFTY 50 backfill (one-shot)
gcloud compute ssh "$INSTANCE" $ZONE_FLAG --command='cd bharat-terminal && docker compose run --rm kb-seed'

# Seed the research graph
gcloud compute ssh "$INSTANCE" $ZONE_FLAG --command='cd bharat-terminal && docker compose --profile research run --rm research-runner seed'
```

## Troubleshooting

- **`gcloud: command not found`** — install the
  [Cloud SDK](https://cloud.google.com/sdk/docs/install).
- **`Permission denied (publickey)`** — gcloud auto-provisions a key on
  first use. Run `gcloud compute ssh <instance> --zone=<zone>` once
  manually to let it complete that handshake, then re-run the deploy.
- **`The instance may not have an active external IP`** — your project
  uses IAP-only access. gcloud detects this and tunnels through IAP
  automatically; just be patient on the first connection.
- **`docker: permission denied`** — should be impossible after the script
  runs (the three-tier fallback handles it), but if you see it during
  manual debugging: log out, log back in, retry. Or use `sudo docker`.
- **`waiting for redpanda to be healthy…` hangs** — usually low RAM on
  the GCE instance. `docker compose logs redpanda` will show OOM. Bump
  the machine type to ≥ `e2-standard-4`.
- **`alembic upgrade head` fails on `pgvector`** — confirm the postgres
  image is `pgvector/pgvector:pg15` (it is in `docker-compose.yml` —
  don't override it on the remote).
- **MCX adapter logs `credentials not set`** — expected if `.env` lacks
  `ANGEL_ONE_*`. The rest of the stack still works.
- **Anthropic 401** — confirm `ANTHROPIC_API_KEY` actually flowed through
  to the container:
  `gcloud compute ssh $INSTANCE $ZONE_FLAG --command='cd bharat-terminal && docker compose exec analysis-worker env | grep ANTHROPIC'`.
  If empty, the local `.env` wasn't updated before deploy.
