# CLI Reference

## Global Options

```
opp_ci [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
|---|---|
| `-v`, `--verbose` | Enable debug-level logging |

## Commands

### `opp_ci init-db`

Create database tables explicitly. Not required — tables are auto-created on first `run`.

### `opp_ci run`

Run a test for a project and store the result.

```
opp_ci run --project PROJECT --test TEST_TYPE [--skip-install]
```

| Option | Required | Description |
|---|---|---|
| `--project` | yes | Project name (e.g. `inet`, `inet-4.5`, `omnetpp`, `fifo`) |
| `--test` | yes | Test type(s), comma-separated (e.g. `smoke,fingerprint`) |
| `--skip-install` | no | Skip the `opp_env install` step (opp_env mode only) |

Supported test types: `smoke`, `fingerprint`, `statistical`, `feature`, `speed`, `sanitizer`, `chart`

In direct mode (`OPP_CI_USE_OPP_ENV=0`), runs `opp_repl` commands directly with `--output-format json` to capture structured test details.

### `opp_ci run-matrix`

Expand a test matrix and run all jobs sequentially.

```
opp_ci run-matrix --matrix NAME [--skip-install]
```

| Option | Required | Description |
|---|---|---|
| `--matrix` | yes | Matrix name (must exist in DB, created via `create-matrix` or `seed-matrices`) |
| `--skip-install` | no | Skip the `opp_env install` step |

### `opp_ci create-matrix`

Create a test matrix configuration.

```
opp_ci create-matrix --name NAME --project PROJECT [OPTIONS] --tests TESTS
```

| Option | Required | Default | Description |
|---|---|---|---|
| `--name` | yes | — | Matrix name (e.g. `inet-default`) |
| `--project` | yes | — | Project name |
| `--project-versions` | no | project name | Comma-separated project versions |
| `--builds` | no | `release` | Comma-separated build modes (e.g. `release,debug`) |
| `--os` | no | — | Comma-separated OS (e.g. `Ubuntu 24.04,Fedora 41`) |
| `--os-version` | no | — | Comma-separated OS versions for cross-product with `--os` |
| `--compiler` | no | — | Comma-separated compilers (e.g. `gcc-14,clang-18`) |
| `--compiler-version` | no | — | Comma-separated compiler versions for cross-product with `--compiler` |
| `--tests` | yes | — | Comma-separated test types |

**Platform axes support two styles:**

- **Combined**: `--os 'Ubuntu 24.04,Fedora 41'` — automatically parsed into name + version
- **Structured**: `--os 'Ubuntu,Fedora' --os-version '24.04,41'` — cross-product of names × versions

Same for `--compiler` / `--compiler-version`.

**Example:**

```bash
opp_ci create-matrix \
  --name inet-full \
  --project inet \
  --project-versions "master,4.5" \
  --builds "release,debug" \
  --os "Ubuntu 24.04" \
  --compiler "gcc-14,clang-18" \
  --tests "smoke,fingerprint"
```

### `opp_ci list-matrices`

List all defined test matrices with their expanded job count.

```
opp_ci list-matrices
```

### `opp_ci seed-matrices`

Seed the database with default matrix configurations for Tier 1 projects.

```
opp_ci seed-matrices
```

### `opp_ci list-runs`

List test runs.

```
opp_ci list-runs [--project PROJECT] [--test TEST_TYPE] [--status STATUS] [--limit N]
```

| Option | Default | Description |
|---|---|---|
| `--project` | all | Filter by project name |
| `--test` | all | Filter by test type |
| `--status` | all | Filter by status: `passed`, `failed`, `error` |
| `--limit` | 20 | Maximum number of rows to display |

### `opp_ci show-run`

Show details of a specific test run including result, stdout/stderr.

```
opp_ci show-run RUN_ID
```

### `opp_ci show-results`

Display stored test results (alias for `list-runs`).

```
opp_ci show-results [--project PROJECT] [--test TEST_TYPE] [--status STATUS] [--limit N]
```

### `opp_ci seed-projects`

Seed the database with Tier 1 projects from the catalog.

```
opp_ci seed-projects
```

### `opp_ci list-projects`

List known projects with tier, dependencies, and GitHub info.

```
opp_ci list-projects
```

### `opp_ci serve`

Start the web UI server.

```
opp_ci serve [--host HOST] [--port PORT]
```

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Bind port |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | Database connection URL |
| `OPP_CI_USE_OPP_ENV` | `0` | Set to `1` to use `opp_env` for environment setup |

## Typical Workflows

### Quick local test

```bash
opp_ci init-db
opp_ci run --project fifo --test smoke --skip-install
opp_ci serve  # browse results at http://localhost:8000
```

### Matrix testing

```bash
opp_ci create-matrix --name fifo-default --project fifo --builds "release,debug" --tests "smoke,fingerprint"
opp_ci run-matrix --matrix fifo-default --skip-install
```
