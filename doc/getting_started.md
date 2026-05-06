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
opp_ci run --project inet --test smoke
```

This will:
1. Create `opp_ci.db` (SQLite) in the current directory if it doesn't exist
2. Run `opp_run_smoke_tests` directly (no Nix/opp_env needed)
3. Store the result in the database

## Viewing Results

```bash
opp_ci show-results
opp_ci show-results --project inet
opp_ci show-results --status failed
```

Or inspect the SQLite database directly:
```bash
sqlite3 opp_ci.db "SELECT * FROM test_runs;"
```

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
