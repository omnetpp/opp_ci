# Configuration

All configuration is via environment variables, read by
`opp_ci/config.py` at process start.

## Core

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | SQLAlchemy connection string. Use `postgresql://user:pass@host/db` in production. |
| `OPP_CI_COORDINATOR_URL` | auto-detected `http://<host-ip>:8080` | Coordinator API base URL. Read by `--remote` CLI mode and by workers. |
| `OPP_CI_API_TOKEN` | *(empty)* | API token used by `--remote` CLI submissions. |
| `OPP_CI_REFERENCE_PLATFORM` | `Ubuntu 24.04/gcc-13` | Default platform spec for auto-generated default matrices. |

## Workers

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_WORKER_POLL_INTERVAL` | `10` | Seconds between worker job polls. |
| `OPP_CI_WORKER_HEARTBEAT_INTERVAL` | `30` | Seconds between worker heartbeats. |
| `OPP_CI_WORKER_HEARTBEAT_TIMEOUT` | `120` | Seconds before a worker is marked offline. |

## GitHub integration

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_GITHUB_TOKEN` | *(empty)* | GitHub API token (takes precedence over the file). |
| `OPP_CI_GITHUB_TOKEN_FILE` | `~/.ssh/github_repo_token` | File path to read the GitHub API token from. |
| `OPP_CI_GITHUB_WEBHOOK_SECRET` | *(empty)* | HMAC secret for `X-Hub-Signature-256` verification. |
| `OPP_CI_GITHUB_STATUS_CONTEXT` | `opp_ci` | Context string used when posting commit statuses. |
| `OPP_CI_GITHUB_BASE_URL` | `https://api.github.com` | GitHub API base URL (override for GitHub Enterprise). |

## Git notes (workflow_dispatch token)

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_GITHUB_ACTIONS_TOKEN` | *(empty)* | Fine-grained PAT with `Actions: Write` scope. Used to trigger the `ci-notes.yml` workflow on target repos. |
| `OPP_CI_GITHUB_ACTIONS_TOKEN_FILE` | `~/.ssh/github_actions_token` | File path to read the Actions PAT from. |

See [git_notes.md](git_notes.md) for the full permission model.

## Tips

- All env vars are read once on startup. Restart `opp_ci serve` after
  changing them.
- The coordinator URL auto-detection picks the host's primary outbound
  IP. On multi-NIC hosts, set `OPP_CI_COORDINATOR_URL` explicitly.
- For local development, leave `OPP_CI_DATABASE_URL` unset — SQLite at
  `./opp_ci.db` is fine.
