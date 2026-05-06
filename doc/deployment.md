# Deployment

## Local Development

No external services needed. Uses SQLite and direct command execution.

```bash
source setenv
pip install -e .
opp_ci init-db
opp_ci create-matrix --name fifo-default --project fifo --builds "release,debug" --tests "smoke,fingerprint"
opp_ci run-matrix --matrix fifo-default --skip-install
opp_ci serve
```

The database file (`opp_ci.db`) is created in the working directory. The web UI is at `http://localhost:8000`.

## Cloud Deployment (PostgreSQL + opp_env)

### Database Setup

```bash
createdb opp_ci
export OPP_CI_DATABASE_URL="postgresql://user:pass@host/opp_ci"
opp_ci init-db
```

### Enable opp_env Mode

```bash
export OPP_CI_USE_OPP_ENV=1
```

This requires Nix and `opp_env` to be installed on the machine.

### Running

```bash
opp_ci run --project inet-4.5 --test smoke
opp_ci run-matrix --matrix inet-default
```

### Web UI

```bash
opp_ci serve --host 0.0.0.0 --port 8000
```

Serves at `http://0.0.0.0:8000`. Place behind a reverse proxy (nginx/Caddy) with HTTPS for production.

## Local GitHub Integration

You can receive GitHub webhooks locally without a cloud VPS:

### Using GitHub CLI (recommended)

```bash
gh webhook forward \
  --repo=owner/repo \
  --events=push,pull_request \
  --url=http://localhost:8000/api/github/webhook
```

### Using ngrok/cloudflared

```bash
ngrok http 8000
# Register the ngrok URL as webhook in GitHub repo settings
```

### Outbound-only (no webhooks)

Post results back to GitHub without receiving events — trigger tests manually or on a schedule, then use the GitHub API to post commit statuses:

```bash
opp_ci run-matrix --matrix inet-default
# Results are posted to GitHub via API (requires token)
```

## Environment Variables

| Variable | Local default | Cloud example |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | `postgresql://ci:secret@db.example.com/opp_ci` |
| `OPP_CI_USE_OPP_ENV` | `0` | `1` |
