# Deployment

opp_ci can run in three configurations:

- **Local-only** — SQLite + direct execution. Good for development and
  single-developer use.
- **Single host coordinator** — PostgreSQL + workers on the same VPS.
- **Hybrid** (recommended for shared use) — coordinator in the cloud,
  workers on self-hosted hardware.

## Local development

No external services needed. SQLite and direct subprocess execution.

```bash
source setenv
pip install -e .
opp_ci init-db
opp_ci create-matrix --name fifo-default --project fifo \
  --builds "release,debug" --tests "smoke,fingerprint"
opp_ci run-matrix --matrix fifo-default --skip-install
opp_ci serve
```

`opp_ci.db` lands in the working directory; the web UI listens on
`http://localhost:8080`.

## Cloud (PostgreSQL)

### Database

```bash
createdb opp_ci
export OPP_CI_DATABASE_URL="postgresql://user:pass@host/opp_ci"
opp_ci init-db
```

### Reproducible builds via opp_env

Requires Nix and `opp_env` on whichever machine runs the jobs (either
the coordinator itself if it doubles as a worker, or each remote
worker).

Pick the opp_env toolchain at run time via `--toolchain nix` on
individual runs or matrices, or set it as the matrix default.

### Web server

```bash
opp_ci serve --host 0.0.0.0 --port 8080
```

For long-running deployments, run `serve` (and `worker start`) under
systemd instead of a bare shell — see [systemd.md](systemd.md) for the
unit files, install script, and per-role configuration.

Always place behind a reverse proxy with HTTPS (Caddy or nginx + Let's
Encrypt). Example public URLs:

- Web UI: `https://ci.omnetpp.org/`
- API base: `https://ci.omnetpp.org/api/`
- Webhook receiver: `https://ci.omnetpp.org/api/github/webhook`

## Hybrid (recommended for production)

```
┌─────────────────────────────────────────────────────────┐
│  Cloud VPS (Hetzner / DigitalOcean / Lightsail / …)     │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  opp_ci web │  │  opp_ci API  │  │  PostgreSQL  │    │
│  │  (FastAPI)  │  │  + webhooks  │  │              │    │
│  └──────┬──────┘  └──────┬───────┘  └──────────────┘    │
│         │                │                              │
│  ┌──────┴────────────────┴────────┐                     │
│  │       opp_ci scheduler         │                     │
│  └────────────────┬───────────────┘                     │
└───────────────────┼─────────────────────────────────────┘
                    │  workers poll (outbound only)
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐
   │ Worker 1│ │ Worker 2│ │ Worker N│  (self-hosted or cloud)
   │ opp_env │ │ opp_env │ │ opp_env │
   │ opp_repl│ │ opp_repl│ │ opp_repl│
   │ Nix     │ │ Nix     │ │ Nix     │
   └─────────┘ └─────────┘ └─────────┘
```

- **Coordinator** (web UI + API + scheduler + Postgres) on a cheap
  cloud VPS. Always accessible, handles webhooks, serves the UI.
- **Workers** on self-hosted machines. Access to hardware perf counters
  (speed tests), no per-minute cost, can be beefy.

Workers only need **outbound** network access — they poll the
coordinator. No inbound port on workers.

See [workers.md](workers.md) for worker registration and startup.

## Hosting options

| Option | Pros | Cons | Cost |
|---|---|---|---|
| Hetzner Cloud VPS | Cheap, EU, good perf | Manual sysadmin | ~€5–20/mo |
| DigitalOcean Droplet | Simple, managed Postgres available | Slightly pricier | ~$12–24/mo |
| AWS Lightsail | Predictable pricing | AWS complexity creep | ~$10–20/mo |
| Self-hosted | Full control, perf counters | Hardware, network, uptime | One-time hardware |
| Hybrid | Cheap coordinator + powerful workers | More complex networking | Cloud + hardware |

## Access patterns

Three ways to interact with a deployed coordinator:

1. **Web browser** — `https://ci.omnetpp.org` — view results, start
   runs, manage matrices/rules/workers.
2. **Python client / CLI `--remote`** — submit runs, query results
   programmatically. See [python_client.md](python_client.md).
3. **GitHub webhooks** — GitHub posts push/PR events to
   `/api/github/webhook`, auto-triggering matching matrices. See
   [github_integration.md](github_integration.md).

## Local GitHub integration (no public coordinator)

You can receive webhooks against a local coordinator using a tunnel.

### Using GitHub CLI (recommended)

```bash
gh webhook forward \
  --repo=owner/repo \
  --events=push,pull_request \
  --url=http://localhost:8080/api/github/webhook
```

### Using ngrok / cloudflared

```bash
ngrok http 8080
# Register the printed URL in the repo's webhook settings.
```

## Security

- **HTTPS everywhere** for the coordinator — Caddy/nginx with auto-TLS.
- **Bearer token auth** with four roles (readonly / submitter / worker
  / admin). See [rest_api.md](rest_api.md#authentication).
- **Per-worker tokens** auto-generated at registration, checked on
  every poll/heartbeat/result.
- **Webhook secret** — `OPP_CI_GITHUB_WEBHOOK_SECRET` for HMAC-SHA256
  signature validation.
- **Minimum GitHub scopes** — separate tokens for statuses
  (`OPP_CI_GITHUB_TOKEN`) and notes-workflow dispatch
  (`OPP_CI_GITHUB_ACTIONS_TOKEN`, fine-grained, `Actions: Write` only).

## Environment variables

See [configuration.md](configuration.md) for the full list.

| Variable | Local default | Cloud example |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | `postgresql://ci:secret@db.example.com/opp_ci` |
| `OPP_CI_COORDINATOR_URL` | auto-detected | `https://ci.omnetpp.org` |
| `OPP_CI_GITHUB_WEBHOOK_SECRET` | *(empty)* | *(strong random hex)* |
