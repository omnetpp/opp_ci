# CLI Reference

```
opp_ci [GLOBAL OPTIONS] COMMAND [ARGS]...
```

For full per-command flags, run `opp_ci <command> --help`.

## Global options

| Option | Description |
|---|---|
| `-v`, `--verbose` | Enable debug-level logging |
| `--remote` / `--local` | Drive a remote coordinator over the REST API instead of the local DB. Default from `OPP_CI_REMOTE`; uses `OPP_CI_COORDINATOR_URL` and `OPP_CI_API_TOKEN`. See [Remote CLI Control](remote_cli.md). |

## Remote control

Almost every command is dual-mode: with `--remote` it calls the
coordinator's REST API; without it, it runs against the local database.
Read commands need a `readonly` token, submission needs `submitter`, and
management (`delete-*`, `seed-*`, `user`, `token`, `worker register`,
`rule`) needs `admin`. Host-local commands (`init-db`, `reset-db`,
`serve`, `tls-selfsign`, `worker start`, `worker detect-tags`,
`image build`, `internal run-direct`) refuse `--remote` with a non-zero
exit. See [Remote CLI Control](remote_cli.md) for the full matrix,
role mapping, and notes (e.g. `run-matrix --remote` is named-matrix-only;
`image build-matrix --remote` reads remotely but builds locally).

## Database

| Command | Purpose |
|---|---|
| `opp_ci init-db` | Create tables. Auto-runs on first `run`, so usually optional. |
| `opp_ci reset-db --yes` | Drop and recreate all tables. Destructive. On Postgres the drop is `DROP SCHEMA public CASCADE` so any legacy tables left over from prior schemas (e.g. a pre-Phase-1 `test_results` table) get cleared too. Add `--preserve-tokens` to snapshot and restore the `api_tokens` and `workers` rows so external systems keep working. |

## Running tests

| Command | Purpose |
|---|---|
| `opp_ci run` | Run a single test for a project. Required: `--project`, `--kind`. Common: `--ref`, `--mode`, `--isolation {none\|podman}`, `--toolchain {none\|nix}`, `--os`, `--os-version`, `--arch`, `--compiler`, `--compiler-version`, `--pin <dep>=<ver>` (repeatable), `--force`, `--skip-install`. |
| `opp_ci run-matrix` | Universal matrix launcher — three input modes, choose exactly one. See below. |

`run-matrix` accepts any one of:

- **Named matrix**: `--matrix NAME` looks up a stored `TestMatrix` and expands it.
- **Spec file**: `--spec-file path.json` (or `-` to read JSON from stdin) — a JSON object with `project`, optional `name` / `opp_file`, and the same axis keys as a `TestMatrix.config`.
- **Inline axis flags**: `--project NAME` plus any of `--kinds`, `--modes`, `--refs` / `--ref`, `--versions`, `--os`, `--os-version`, `--distro`, `--distro-version`, `--flavor`, `--flavor-version`, `--compiler`, `--compiler-version`, `--arch`, `--isolation`, `--toolchain` (all comma-separated for multi-value axes).

Spec-file and inline forms persist an anonymous `TestMatrix` row named
`adhoc:<project>:<UTC-timestamp>` so the resulting `TestMatrixRun` has
a stable parent.

Cross-cutting options:

- `--no-cache` — bypass the content-addressable cache and force a fresh `TestRun` per cell. Without it, cells whose `cache_fingerprint` matches a prior finished `TestRun` reuse that observation (a `TestVerdict` cell with `cache_hit=True`) instead of re-executing.
- `--skip-install` — skip the `opp_env install` step (only relevant for `--isolation none --toolchain nix`).

The command prints the new `TestMatrixRun` id at the end, so
`opp_ci show-matrix-run <id>` is the natural follow-up.

Supported test kinds (comma-separated for `--kind`) — see the canonical
list in [test_matrix_dimensions.md](test_matrix_dimensions.md#axis-kind):
`smoke`, `fingerprint`, `statistical`, `feature`, `speed`, `sanitizer`,
`chart`, `release`, `build`, `opp`, `all`.

## Matrices

| Command | Purpose |
|---|---|
| `opp_ci create-matrix` | Create a named matrix. Required: `--name`, `--project`, `--kinds`. Axes: `--project-versions`, `--builds`, `--os` [`--os-version`], `--arch`, `--compiler` [`--compiler-version`], `--refs`, `--ref-range`, `--deps`, `--isolation`, `--toolchain`, `--opp-file`. `--replace` overwrites an existing matrix of the same name. |
| `opp_ci list-matrices` | List matrices with expanded job count. |
| `opp_ci seed-matrices` | Seed default matrices for the core projects. |

Platform axes accept two styles:

- **Combined**: `--os 'Ubuntu 24.04,Fedora 41'` — auto-parsed into name + version
- **Structured**: `--os 'Ubuntu,Fedora' --os-version '24.04,41'` — cross-product

## Runs and results

| Command | Purpose |
|---|---|
| `opp_ci list-runs` | List runs. Filters: `--project`, `--ref`, `--kind`, `--status`, `--limit`. `--status` matches `TestRun.lifecycle` (`queued` / `running` / `finished` / `cancelled` / `timed_out`) or a `TestResultCode` (`PASS` / `FAIL` / `ERROR` / `SKIPPED`). |
| `opp_ci show-run RUN_ID` | Run detail + stdout/stderr (read off the TestRun row directly). |
| `opp_ci show-results` | Same filters as `list-runs`; presents stored outcomes. |
| `opp_ci delete-run RUN_ID --yes` | Delete a single run. |
| `opp_ci delete-runs` | Bulk delete. Filters: `--project`, `--ref`, `--kind`, `--status`, `--before YYYY-MM-DD`, `--yes`. |

## Matrix runs and verdicts

Each `TestMatrixRun` carries an O(1) rollup with a three-state
verdict (`EXPECTED` / `UNEXPECTED` / `UNKNOWN`) computed against the
[expectation log](#expectations). The CLI mirrors the [matrix-runs
web pages](web_ui.md#matrix-runs).

| Command | Purpose |
|---|---|
| `opp_ci list-matrix-runs` | Recent `TestMatrixRun` rows with their rollup verdict. Filters: `--project`, `--verdict {EXPECTED\|UNEXPECTED\|UNKNOWN}`, `--since YYYY-MM-DD`, `--limit`. |
| `opp_ci show-matrix-run ID` | Rollup header + per-cell `TestVerdict` table for one matrix run. `--unexpected-only` filters to cells that diverged from their expectation (or have no expectation yet). |

## Expectations

Expectations live in [`expected_test_results`](data_model.md#expectedtestresult)
— an append-only log keyed by `test_id`. Editing an expectation
applies *forward only*: historical `TestVerdict` rows pin the row
that was in force at recording time and stay reconstructible.

| Command | Purpose |
|---|---|
| `opp_ci set-expectation --expect {pass\|fail\|error\|none}` | Insert one `ExpectedTestResult` row per matching `Test`. Matches on `--project NAME` and/or `--where field=value` (repeatable; e.g. `--where os=Linux --where kind=smoke`). Allowed fields are the `Test` coordinate columns. `--expect none` writes an explicit retraction (NULL code), distinguishable from never-set and itself audited. Options: `--reason`, `--set-by` (default `cli`), `--limit` (safety cap on Tests touched, default 200), `--dry-run` to preview without writing. |
| `opp_ci show-expectations --test-id N` | List the per-Test edit history (newest first). |

## Projects and versions

| Command | Purpose |
|---|---|
| `opp_ci seed-projects` | Seed the core projects from the static catalog. |
| `opp_ci seed-platforms` | Seed the `OS` and `Compiler` tables from `opp_ci/podman/platforms.yml`. Idempotent — only new (name, version) rows are inserted. |
| `opp_ci sync-catalog` | Import all opp_env projects + versions. New projects get a default matrix. |
| `opp_ci add-project --name NAME` | Manually register a project not in opp_env. Options: `--github owner/repo`, `--git-url`, `--opp-env-name`, `--deps`. |
| `opp_ci list-projects` | Show project catalog (deps, GitHub). |
| `opp_ci add-version --project P --label L` | Register a version. Options: `--ref`, `--opp-env-version`, `--deps` (JSON). |
| `opp_ci list-versions [--project P]` | Show known versions per project. |
| `opp_ci resolve-deps PROJECT-VERSION` | Print resolved deps. `--pin dep=ver` to override. |

## Workers (`opp_ci worker ...`)

| Command | Purpose |
|---|---|
| `worker register --name N` | Register a worker, prints its token. Options: `--tags`, `--auto-tags`, `--concurrency`. |
| `worker list` | List registered workers, status, tags. |
| `worker detect-tags` | Print the capability tags this host would self-advertise (the same set used by `--auto-tags`). Useful for previewing before `worker register`. |
| `worker start --coordinator URL --token T` | Run the worker agent. Tags and concurrency are fetched from the coordinator (set at register time). Options: `--poll-interval`, `--heartbeat-interval`. |

See [workers.md](workers.md).

## API tokens (`opp_ci token ...`)

| Command | Purpose |
|---|---|
| `token create --name N --role R` | Create a token. Roles: `readonly`, `submitter`, `worker`, `admin`. Prints the token once. |
| `token list` | List tokens (values masked). |
| `token revoke TOKEN_ID` | Disable a token. |

## GitHub rules (`opp_ci rule ...`)

| Command | Purpose |
|---|---|
| `rule create --project P --type T --pattern G` | Create an AutoTestRule. `--type {branch\|pr\|tag}`, `--pattern` is a glob, `--matrix` links a matrix (smoke-only if omitted), `--replace` overwrites duplicates. |
| `rule list` | List configured rules. |
| `rule delete RULE_ID` | Delete a rule. |
| `rule test-webhook --project P --ref R` | Simulate a webhook locally for the given event. Options: `--type {push\|pr}`, `--sha`, `--pr-number`. |

See [github_integration.md](github_integration.md).

## Web server

| Command | Purpose |
|---|---|
| `opp_ci serve` | Start the FastAPI server. Options: `--host` (default `127.0.0.1`), `--port` (default `8080` — matches the default `OPP_CI_COORDINATOR_URL`). |

See [web_ui.md](web_ui.md).

## Container images (`opp_ci image ...`)

| Command | Purpose |
|---|---|
| `image build` | Build one of the bundled Podman images used for `--isolation podman` runs. `--toolchain {host\|nix}` (note: `host` here, not `none` as on `run` / `create-matrix`), `--os`, `--os-version`, `--compiler`, `--compiler-version`. |
| `image build-matrix` | Build all images required by a matrix. |

## Environment

The CLI reads its configuration from environment variables. See
[configuration.md](configuration.md).

## Typical workflows

### Local single test

```bash
opp_ci init-db
opp_ci run --project fifo --kind smoke --skip-install
opp_ci serve  # browse at http://localhost:8080
```

### Local matrix

```bash
opp_ci create-matrix --name fifo-default --project fifo \
  --builds "release,debug" --kinds "smoke,fingerprint"
opp_ci run-matrix --matrix fifo-default --skip-install
```

### Remote submission

```bash
export OPP_CI_COORDINATOR_URL=https://ci.omnetpp.org
export OPP_CI_API_TOKEN=<submitter-token>

opp_ci --remote run --project inet-4.5 --kind smoke,fingerprint --ref master \
    --mode release --isolation podman --toolchain none \
    --os Ubuntu --os-version 26.04 --arch amd64 \
    --compiler clang --compiler-version 22 --force
opp_ci --remote list-runs --project inet --status FAIL
```

`OPP_CI_COORDINATOR_URL` is the coordinator's host URL **without** the
`/api` suffix — the CLI appends it when calling the REST router. (Using
`OppCiClient` directly from Python takes the full `/api` URL; see
[python_client.md](python_client.md).)

`--remote run` forwards all the run-shaping flags (`--mode`, `--isolation`,
`--toolchain`, `--os`/`--os-version`/`--arch`, `--compiler`/`--compiler-version`,
`--force`). `--pin` is the exception: dependency resolution happens locally
before submission, so pins are not currently forwarded over `--remote`.
