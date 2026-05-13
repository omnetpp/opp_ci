# Getting Started

## Prerequisites

- Python 3.10+
- `opp_repl` installed and on PATH (for test commands like `opp_run_smoke_tests`)

For cloud deployment, you'll also need:
- PostgreSQL
- Nix + `opp_env` (for reproducible environments)

## Installation

```bash
cd ~/workspace/opp_ci
python3 -m venv .venv
source setenv
pip install -e .
```

For PostgreSQL support:
```bash
pip install -e ".[postgres]"
```

## Running Your First Test

```bash
opp_ci init-db
opp_ci run --project fifo --test smoke --skip-install
```

This will:
1. Create `opp_ci.db` (SQLite) in the current directory
2. Run `opp_run_smoke_tests --output-format json` directly (no Nix/opp_env needed)
3. Parse structured JSON test details from the output
4. Store the result (pass/fail, stdout, stderr, per-test details) in the database

## Viewing Results

### CLI

```bash
opp_ci list-runs
opp_ci list-runs --project fifo --status FAIL
opp_ci show-run 1
```

### Web UI

```bash
opp_ci serve
# Open http://localhost:8000
```

The web UI shows:
- Dashboard with recent activity and stats
- Runs list with filtering
- Run detail with colored stdout (ANSI→HTML) and per-test results table
- Results page with multi-dimensional filtering

### Direct DB inspection

```bash
sqlite3 opp_ci.db "SELECT id, project, test_type, status FROM test_runs;"
```

## Test Matrices

Define a matrix to run multiple test configurations:

```bash
opp_ci create-matrix \
  --name fifo-default \
  --project fifo \
  --builds "release,debug" \
  --tests "smoke,fingerprint"

opp_ci run-matrix --matrix fifo-default --skip-install
```

For platform-specific matrices:

```bash
opp_ci create-matrix \
  --name inet-platforms \
  --project inet \
  --os "Ubuntu 24.04,Fedora 41" \
  --compiler "gcc-14,clang-18" \
  --tests "smoke,fingerprint"
```

See [CLI Reference](cli_reference.md) for the full `create-matrix` options.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | Database connection string |
| `OPP_CI_USE_OPP_ENV` | `0` | Set to `1` to run tests via `opp_env` (requires Nix) |

## Using opp_env Mode

For reproducible testing with Nix environments:

```bash
export OPP_CI_USE_OPP_ENV=1
opp_ci run --project inet-4.5 --test smoke
```

This calls `opp_env install inet-4.5` then `opp_env run inet-4.5 -c "opp_run_smoke_tests"`.

## Rebuilding the Database

To start fresh (drops all data):

```bash
rm -f opp_ci.db
opp_ci init-db
opp_ci create-matrix --name fifo-default --project fifo --builds "release,debug" --tests "smoke,fingerprint"
```
