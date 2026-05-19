# CLI Reference

```
opp_ci [GLOBAL OPTIONS] COMMAND [ARGS]...
```

For full per-command flags, run `opp_ci <command> --help`.

## Global options

| Option | Description |
|---|---|
| `-v`, `--verbose` | Enable debug-level logging |
| `--remote` | Submit to a remote coordinator instead of running locally. Uses `OPP_CI_COORDINATOR_URL` and `OPP_CI_API_TOKEN`. |

## Database

| Command | Purpose |
|---|---|
| `opp_ci init-db` | Create tables. Auto-runs on first `run`, so usually optional. |
| `opp_ci reset-db --yes` | Drop and recreate all tables. Destructive. Add `--preserve-tokens` to snapshot and restore the `api_tokens` and `workers` rows so external systems keep working. |

## Running tests

| Command | Purpose |
|---|---|
| `opp_ci run` | Run a single test for a project. Required: `--project`, `--test`. Common: `--ref`, `--mode`, `--isolation {none\|podman}`, `--toolchain {none\|nix}`, `--os`, `--os-version`, `--arch`, `--compiler`, `--compiler-version`, `--pin <dep>=<ver>` (repeatable), `--force`, `--skip-install`. |
| `opp_ci run-matrix --matrix NAME` | Expand a named matrix and run all jobs. Options: `--force`, `--skip-install`. |

Supported tests (comma-separated for `--test`) — see the canonical list
in [test_matrix_dimensions.md](test_matrix_dimensions.md#axis-test-types):
`smoke`, `fingerprint`, `statistical`, `feature`, `speed`, `sanitizer`,
`chart`, `release`, `build`, `opp`, `all`.

## Matrices

| Command | Purpose |
|---|---|
| `opp_ci create-matrix` | Create a named matrix. Required: `--name`, `--project`, `--tests`. Axes: `--project-versions`, `--builds`, `--os` [`--os-version`], `--arch`, `--compiler` [`--compiler-version`], `--refs`, `--ref-range`, `--deps`, `--isolation`, `--toolchain`, `--opp-file`. `--replace` overwrites an existing matrix of the same name. |
| `opp_ci list-matrices` | List matrices with expanded job count. |
| `opp_ci seed-matrices` | Seed default matrices for the core projects. |

Platform axes accept two styles:

- **Combined**: `--os 'Ubuntu 24.04,Fedora 41'` — auto-parsed into name + version
- **Structured**: `--os 'Ubuntu,Fedora' --os-version '24.04,41'` — cross-product

## Runs and results

| Command | Purpose |
|---|---|
| `opp_ci list-runs` | List runs. Filters: `--project`, `--ref`, `--test`, `--status`, `--limit`. |
| `opp_ci show-run RUN_ID` | Run detail + stdout/stderr. |
| `opp_ci show-results` | Same filters as `list-runs`; presents stored results. |
| `opp_ci delete-run RUN_ID --yes` | Delete a single run. |
| `opp_ci delete-runs` | Bulk delete. Filters: `--project`, `--ref`, `--test`, `--status`, `--before YYYY-MM-DD`, `--yes`. |

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
opp_ci run --project fifo --test smoke --skip-install
opp_ci serve  # browse at http://localhost:8080
```

### Local matrix

```bash
opp_ci create-matrix --name fifo-default --project fifo \
  --builds "release,debug" --tests "smoke,fingerprint"
opp_ci run-matrix --matrix fifo-default --skip-install
```

### Remote submission

```bash
export OPP_CI_COORDINATOR_URL=https://ci.omnetpp.org
export OPP_CI_API_TOKEN=<submitter-token>

opp_ci --remote run --project inet-4.5 --test smoke,fingerprint --ref master \
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
