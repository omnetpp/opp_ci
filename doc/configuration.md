# Configuration

All configuration is via environment variables, read by
`opp_ci/config.py` at process start.

## Core

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | SQLAlchemy connection string. Use `postgresql://user:pass@host/db` in production. |
| `OPP_CI_COORDINATOR_URL` | auto-detected `http://<host-ip>:8080` | Coordinator API base URL (bare origin; the CLI appends `/api`). Read by `--remote` CLI mode and by workers. |
| `OPP_CI_API_TOKEN` | *(empty)* | Bearer token used by `--remote` CLI calls. |
| `OPP_CI_REMOTE` | `0` | Default for the `--remote` flag. Set to `1` to make every command remote without typing `--remote`; override per-command with `--local`. See [Remote CLI Control](remote_cli.md). |
| `OPP_CI_HTTP_DEBUG` | `0` | When `1`, allow `urllib3` request logging under `--verbose` (off by default so bearer tokens don't leak into scrollback). |
| `OPP_CI_REFERENCE_PLATFORM` | `Ubuntu 24.04/gcc-13` | Default platform spec for auto-generated default matrices. |

## Executor: project source location

The executor uses these to resolve where each project's working tree
lives on the worker. Relevant under `--isolation none` (direct host)
and `--isolation podman` with a host-mounted tree.

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_PROJECT_DIR` | `.` | Parent directory: each project's tree is expected at `$OPP_CI_PROJECT_DIR/<project>`. |
| `OPP_CI_PROJECT_DIR_<PROJECT>` | *(empty)* | Per-project override, takes precedence over `OPP_CI_PROJECT_DIR/<project>`. The suffix is the project name upper-cased with `-` replaced by `_` (e.g. `OPP_CI_PROJECT_DIR_INET_4_5`). |
| `OPP_CI_CACHE_DIR` | `~/.cache/opp_ci/clones` | Where the executor caches GitHub clones (for `.opp` files that declare `github_owner`/`github_repository`). |

## Container-runtime contract

These are set by the executor and read inside containers — usually no
need to set them by hand. Documented here so the docker entrypoint
contract is explicit.

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_INSTALL_PROJECTS` | *(empty)* | Comma-separated list of opp_env projects the entrypoint should `opp_env install` before running the test command. |
| `OPP_ENV_GIT_REF` | *(empty)* | Specific git ref to pin a `*-git` opp_env project to, when one is named. |

## Coordinator (web server)

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_COORDINATOR_HOST` | `127.0.0.1` | Bind host for `opp_ci coordinator start`. Overridden by `--host`. |
| `OPP_CI_COORDINATOR_PORT` | `8080` | Bind port for `opp_ci coordinator start`. Overridden by `--port`. |

## Workers

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_WORKER_TOKEN` | *(empty)* | Token used by `opp_ci worker start` when `--token` is omitted (e.g. when run from a systemd unit). |
| `OPP_CI_WORKER_POLL_INTERVAL` | `10` | Seconds between worker job polls. |
| `OPP_CI_WORKER_HEARTBEAT_INTERVAL` | `30` | Seconds between worker heartbeats. |
| `OPP_CI_WORKER_HEARTBEAT_TIMEOUT` | `120` | Seconds before a silent worker is marked offline and its in-flight runs reclaimed. |
| `OPP_CI_WORKER_REAP_INTERVAL` | *(half the heartbeat timeout, min 15)* | Seconds between coordinator reaper sweeps. Each sweep marks stale workers offline, reclaims their orphaned `running` runs, and expires unserviceable `queued` runs. |
| `OPP_CI_MAX_RECLAIMS` | `2` | Times a `running` run is re-queued after its worker goes dark before it is retired to `timed_out`/`ERROR` as a poison pill. |
| `OPP_CI_QUEUE_UNSERVICEABLE_TIMEOUT` | `300` | Seconds a `queued` run may wait with no enabled worker able to serve its capability tags before it is expired to `timed_out`/`ERROR`. Serviceable-but-starved runs (right tags, fleet busy/offline) are never auto-expired. `0` disables the sweep. |

The reaper sweep runs in the coordinator (`opp_ci coordinator start`), not the worker:
it covers both dead workers and queued runs no worker can serve. See
[workers.md → Lifecycle](workers.md#lifecycle).

Note: `opp_ci worker start` also falls back to `OPP_CI_COORDINATOR_URL`
when `--coordinator` is omitted.

## GitHub integration

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_GITHUB_TOKEN` | *(empty)* | GitHub API token (takes precedence over the file). |
| `OPP_CI_GITHUB_TOKEN_FILE` | `~/.ssh/opp_ci_github_token` | File path to read the GitHub API token from. |
| `OPP_CI_GITHUB_WEBHOOK_SECRET` | *(empty)* | HMAC secret for `X-Hub-Signature-256` verification. |
| `OPP_CI_GITHUB_STATUS_CONTEXT` | `opp_ci` | Context string used when posting commit statuses. |
| `OPP_CI_GITHUB_BASE_URL` | `https://api.github.com` | GitHub API base URL (override for GitHub Enterprise). |

## Git notes (workflow_dispatch token)

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_GITHUB_ACTIONS_TOKEN` | *(empty)* | Fine-grained PAT with `Actions: Write` scope. Used to trigger the `ci-notes.yml` workflow on target repos. |
| `OPP_CI_GITHUB_ACTIONS_TOKEN_FILE` | `~/.ssh/opp_ci_github_actions_token` | File path to read the Actions PAT from. |

See [git_notes.md](git_notes.md) for the full permission model.

## Tips

- All env vars are read once on startup. Restart `opp_ci coordinator start` after
  changing them.
- The coordinator URL auto-detection picks the host's primary outbound
  IP. On multi-NIC hosts, set `OPP_CI_COORDINATOR_URL` explicitly.
- For local development, leave `OPP_CI_DATABASE_URL` unset — SQLite at
  `./opp_ci.db` is fine.
