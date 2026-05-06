# Architecture

## Overview

```
opp_ci CLI / Web UI
        │
        ├── executor        ←  runs tests (direct or via opp_env)
        ├── database        ←  SQLite (local) or PostgreSQL (cloud)
        └── opp_repl        ←  actual test execution commands
```

## Components

### CLI (`opp_ci/cli.py`)

Click-based command-line interface. Entry point for running tests and querying results.

### Executor (`opp_ci/executor.py`)

Responsible for invoking test commands. Two modes:

- **Direct mode** (`OPP_CI_USE_OPP_ENV=0`): calls `opp_run_smoke_tests` etc. directly. Assumes the current environment has opp_repl and the simulation project available.
- **opp_env mode** (`OPP_CI_USE_OPP_ENV=1`): calls `opp_env install <project>` then `opp_env run <project> -c "<cmd>"`. Provides full Nix-based isolation.

### Database (`opp_ci/db/`)

- **`models.py`** — SQLAlchemy ORM models: `TestRun`, `TestResult`
- **`connection.py`** — engine and session factory, handles both SQLite and PostgreSQL
- **`migrations/`** — Alembic migration environment

### Config (`opp_ci/config.py`)

Environment variable-based configuration:

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | SQLAlchemy database URL |
| `OPP_CI_USE_OPP_ENV` | `0` | Enable opp_env/Nix mode |

## Data Model

### TestRun

A single invocation of a test. Tracks project, test type, status, timing.

### TestResult

Outcome of a test run. Stores result code (PASS/FAIL/ERROR), stdout, stderr.

## Execution Flow

```
opp_ci run --project inet --test smoke
    │
    ├── create TestRun record (status=running)
    ├── install_project()    ← no-op in direct mode
    ├── run_test()           ← subprocess call
    ├── create TestResult record
    └── update TestRun (status=passed/failed, duration)
```
