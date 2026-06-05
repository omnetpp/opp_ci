# Troubleshooting

Common first-run and operational problems, with where to look first.

## Installation and CLI

### `opp_ci: command not found`

Either the venv isn't active or `bin/` isn't on `PATH`. Re-source the
environment:

```bash
cd ~/workspace/opp_ci
source setenv
```

`source setenv` activates the local `.venv` if present and prepends
`OPP_CI_ROOT/bin` to `PATH`. See [Getting Started](getting_started.md#installation).

### `ModuleNotFoundError: No module named 'opp_repl'`

opp_repl is a required dependency for any test command. Install it
alongside opp_ci (typically a sibling checkout):

```bash
pip install -e ~/workspace/opp_repl
```

Re-running `source setenv` after installing keeps the same venv.

### `pip install` fails on PostgreSQL extra

`pip install -e ".[postgres]"` requires the system `libpq` development
headers. On Debian/Ubuntu: `sudo apt install libpq-dev`. On Fedora:
`sudo dnf install libpq-devel`. macOS with Homebrew: `brew install libpq`.

## Running tests

### `opp_ci run --project inet ...` fails with "project tree not found"

The executor looks under `$OPP_CI_PROJECT_DIR/<project>`. If `inet` lives
at `~/workspace/inet`, set:

```bash
export OPP_CI_PROJECT_DIR=~/workspace
```

Or use a per-project override:

```bash
export OPP_CI_PROJECT_DIR_INET=~/projects/inet
```

The suffix is the project name upper-cased with `-` replaced by `_` (so
`inet-4.5` becomes `OPP_CI_PROJECT_DIR_INET_4_5`). See
[configuration.md](configuration.md#executor-project-source-location).

### A `--toolchain nix` run fails immediately

The Nix toolchain path requires `opp_env` installed and on `PATH`.
Verify with `opp_env --help`. If you don't need reproducible Nix builds,
drop `--toolchain nix` and the executor will use the host's compiler.

### A `--isolation podman` run fails to find an image

Podman runs need a pre-built image whose name matches the
(os, os_version, compiler, compiler_version) coordinates. Build it
first:

```bash
opp_ci image build --os ubuntu --os-version 24.04 \
                   --compiler gcc --compiler-version 13 --toolchain host
```

Note: `image build` takes `--toolchain {host|nix}` while `run` and
`create-matrix` take `--toolchain {none|nix}` (`host` and `none` mean
the same thing — host packages).

### Matrix runs fail "no worker can satisfy capability tags"

The matrix names a platform whose tag scheme no registered worker
advertises. List the workers' tags:

```bash
opp_ci worker list
```

Either re-register a worker with the right `os:<lc>-<ver>` /
`compiler:<lc>-<ver>` / `arch:<lc>` tags, or relax the matrix.
Dispatch rules are in [workers.md](workers.md#capability-tags).

## Database

### `opp_ci.db` is in my current directory — what is it?

That's the local SQLite database (`OPP_CI_DATABASE_URL` defaults to
`sqlite:///opp_ci.db`). It's gitignored; safe to delete to start
fresh. To use Postgres instead, set
`OPP_CI_DATABASE_URL=postgresql://...` before any `opp_ci` command.

### Schema drift after pulling new code

The phase-1 test data model cutover (Test / TestMatrix /
TestMatrixRun / TestRun split, `test` → `kind` rename, outcome
columns on `TestRun`) was applied by wiping and recreating the
database — there is no migration chain that turns a pre-cutover DB
into a post-cutover one. For local development the simplest reset
is:

```bash
rm -f opp_ci.db
opp_ci init-db
opp_ci seed-projects
```

For Postgres deployments the equivalent is to drop and recreate the
schema (or run `opp_ci reset-db --yes`, optionally with
`--preserve-tokens` to keep `api_tokens` / `workers` rows for
external systems). Subsequent schema changes are expected to land via
Alembic under `opp_ci/db/migrations/`.

## Web UI and API

### `opp_ci serve` starts but the page is unreachable

The default bind is `127.0.0.1:8080` — only reachable from the same
host. For cloud deployments use `--host 0.0.0.0` and put a reverse
proxy with TLS in front:

```bash
opp_ci serve --host 0.0.0.0 --port 8080
```

The HTML routes carry no auth — bind to localhost or front with
nginx/Caddy auth before exposing publicly. See
[web_ui.md](web_ui.md).

### `--remote` calls fail with 401

The CLI in remote mode reads `OPP_CI_API_TOKEN` and posts it as a
`Bearer` token. Create one with `opp_ci token create --name … --role submitter`
on the coordinator, then set both env vars on the client side:

```bash
export OPP_CI_COORDINATOR_URL=https://ci.example.org
export OPP_CI_API_TOKEN=<token-printed-once>
```

Note: `OPP_CI_COORDINATOR_URL` is the bare host URL (CLI appends
`/api`); `OppCiClient(url=…)` from Python takes the full `/api` URL.
See [python_client.md](python_client.md).

## GitHub webhooks

### Webhook delivery shows 403

The HMAC signature didn't match. Confirm:
- `OPP_CI_GITHUB_WEBHOOK_SECRET` on the coordinator equals the secret
  configured in the repo's GitHub webhook settings (exact bytes,
  including any trailing newline).
- Content type in GitHub is `application/json` (not the form-encoded
  alternative).

### Webhook fires but no run is queued

Either the project isn't registered, or no `AutoTestRule` matches.
Check both:

```bash
opp_ci list-projects
opp_ci rule list
opp_ci rule test-webhook --project inet --ref master --type push
```

`rule test-webhook` simulates a webhook locally so you can confirm
your patterns. See
[github_integration.md](github_integration.md#autotestrule).

## Workers

### Worker registered but never picks up a job

- Worker status is `offline` — the coordinator hasn't heard a heartbeat
  within `OPP_CI_WORKER_HEARTBEAT_TIMEOUT` (default 120s). Confirm
  outbound connectivity to the coordinator.
- Worker's tag set doesn't cover any queued run's requirements. Run
  `opp_ci worker detect-tags` on the worker host to see what
  `--auto-tags` would advertise and compare with the queued runs.

### Worker logs show repeated 401s

The token was revoked, or the worker is pointed at the wrong
coordinator. Tokens are per-worker, auto-generated at
`opp_ci worker register` — re-register to get a fresh one.
