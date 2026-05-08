# Remote deploy

Two scripts for shipping Bharat Terminal to a remote dev server.

## Prerequisites on the remote

- Ubuntu / Debian (the script uses apt). For RHEL/Fedora, swap the package
  manager line in `deploy.sh` step 2/7.
- ≥ 8 GB RAM, ≥ 30 GB disk (sentence-transformers model + 4 Python images
  + Redpanda + Postgres add up).
- An SSH key already authorised on the host (the script uses your default
  `~/.ssh` config).
- An outbound network path to GitHub, the Anthropic API, Polymarket Gamma,
  and (optionally) Angel One.

## Prerequisites locally

- A filled `.env` at the repo root (the script refuses to ship the
  `.env.example` defaults without confirmation).
- `git`, `ssh`, `scp`, `docker` CLI.

## One-command transfer

```bash
./scripts/deploy/deploy.sh ubuntu@dev.example.com
```

To deploy a feature branch instead of `main`:

```bash
./scripts/deploy/deploy.sh ubuntu@dev.example.com feat/mcx-commodity-research-graph
```

To also run `kb-seed` (NIFTY 50 backfill, ~10 min) on first boot:

```bash
RUN_SEED=1 ./scripts/deploy/deploy.sh ubuntu@dev.example.com
```

To skip the `docker compose build` step on a re-deploy (useful when only
config changed):

```bash
SKIP_BUILD=1 ./scripts/deploy/deploy.sh ubuntu@dev.example.com
```

## What the script does (in order)

1. **SSH probe** — confirms reachability, prints RAM/disk on the remote.
2. **Prerequisites** — installs Docker via `get.docker.com` if missing;
   installs git; joins your user to the `docker` group.
3. **Repo** — clones (or `git pull --ff-only`s) at the requested branch.
4. **`.env`** — copies your local `.env` to `<TARGET>/.env`, mode 600.
   Never goes through git.
5. **Build** — `docker compose build` (skippable with `SKIP_BUILD=1`).
6. **Infra + migrations** — postgres, redis, redpanda, redpanda-init,
   then `kb-migrate`. Optionally `kb-seed` if `RUN_SEED=1`.
7. **App services** — kb-service, ingestion-worker, analysis-worker,
   sentiment-worker, commodity-engine, api-gateway, frontend, prometheus,
   grafana.

## Reaching the UI

Services bind to `localhost` on the remote — the script does **not** open
ports publicly (intentional). Tunnel them back to your laptop:

```bash
ssh -L 3000:localhost:3000 \
    -L 8000:localhost:8000 \
    -L 8080:localhost:8080 \
    -L 3001:localhost:3001 \
    -L 9090:localhost:9090 \
    ubuntu@dev.example.com
```

Then open `http://localhost:3000` etc. in your browser.

If you'd rather expose the frontend publicly on the dev server, drop a
Caddy / Nginx reverse proxy in front of `:3000` and `:8000`. That's a
separate concern and shouldn't be wired into this dev-deploy flow.

## Volume migration (optional)

`deploy.sh` always provisions a **fresh** Postgres / Redpanda / Grafana /
Prometheus. If you specifically want to preserve your local data, run
this **after** the first `deploy.sh` succeeds:

```bash
./scripts/deploy/migrate-volumes.sh ubuntu@dev.example.com
```

This stops both stacks, archives each named volume locally, scps the
archives, and restores them into the remote-side volume names. Both
sides are restarted at the end.

For most dev-server transfers, fresh is fine — `docker compose run --rm
kb-seed` rebuilds the NIFTY 50 universe in ~10 min.

## Troubleshooting

- **`docker: permission denied`** — log out and back in (or run
  `newgrp docker`); the user→docker group join needs a fresh shell.
- **`waiting for redpanda to be healthy…` hangs** — usually low RAM.
  `docker compose logs redpanda` should show OOM. Bump the VM.
- **`alembic upgrade head` fails on `pgvector`** — confirm the postgres
  image is `pgvector/pgvector:pg15` (it is in `docker-compose.yml` —
  don't override it on the remote).
- **MCX adapter logs `credentials not set`** — expected if `.env` lacks
  `ANGEL_ONE_*`. The rest of the stack still works.
- **Anthropic 401** — confirm `ANTHROPIC_API_KEY` actually flowed through
  to the container: `docker compose exec analysis-worker env | grep
  ANTHROPIC`. If empty, the local `.env` wasn't updated before deploy.
