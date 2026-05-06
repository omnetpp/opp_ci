# Architecture

## Overview

```
opp_ci CLI / Web UI
        │
        ├── scheduler       ←  matrix expansion (versions × platforms × tests)
        ├── executor        ←  runs tests (direct or via opp_env)
        ├── database        ←  SQLite (local) or PostgreSQL (cloud)
        └── opp_repl        ←  actual test execution (JSON structured output)
```

## Components

### CLI (`opp_ci/cli.py`)

Click-based command-line interface. Commands for running tests, managing matrices, and querying results. See [CLI Reference](cli_reference.md).

### Scheduler (`opp_ci/scheduler.py`)

Expands matrix configurations into individual test jobs. Handles multi-dimensional cross-products across:
- Project versions
- Build modes (release, debug)
- OS (name × version)
- Compiler (name × version)
- Test types

Supports both combined strings (e.g. "Ubuntu 24.04") and structured cross-product (separate name/version lists).

### Executor (`opp_ci/executor.py`)

Responsible for invoking test commands. Two modes:

- **Direct mode** (`OPP_CI_USE_OPP_ENV=0`): calls `opp_run_smoke_tests` etc. directly with `--output-format json`. Assumes the current environment has opp_repl and the simulation project available.
- **opp_env mode** (`OPP_CI_USE_OPP_ENV=1`): calls `opp_env install <project>` then `opp_env run <project> -c "<cmd>"`. Provides full Nix-based isolation.

In direct mode, the executor:
1. Passes `--output-format json` to opp_repl
2. Parses the last line of stdout as JSON (structured test details)
3. Strips the JSON line from stored stdout
4. Returns both raw output and parsed details

### Database (`opp_ci/db/`)

- **`models.py`** — SQLAlchemy ORM models: `Project`, `TestMatrix`, `TestRun`, `TestResult`
- **`connection.py`** — engine and session factory, handles both SQLite and PostgreSQL
- **`migrations/`** — Alembic migration environment

### Web UI (`opp_ci/web/`)

- **`app.py`** — FastAPI application with Jinja2 templates, ANSI-to-HTML filter
- **`rollup.py`** — Hierarchical result aggregation for summary views
- **`templates/`** — HTML templates for dashboard, runs, results, run detail

### Config (`opp_ci/config.py`)

Environment variable-based configuration:

| Variable | Default | Description |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | SQLAlchemy database URL |
| `OPP_CI_USE_OPP_ENV` | `0` | Enable opp_env/Nix mode |

## Data Model

### Project

Registered project with tier, GitHub info, dependency list. Seeded from catalog.

### TestMatrix

Named matrix configuration. Stores expansion config (versions, modes, platforms, test types). Expanded into jobs by the scheduler.

### TestRun

A single invocation of a test. Tracks:
- Project, test type, build mode
- Platform: os, os_version, compiler, compiler_version
- Status (queued/running/passed/failed/error), timing, trigger source
- Optional link to parent matrix

### TestResult

Outcome of a test run:
- Result code (PASS/FAIL/ERROR)
- Raw stdout/stderr (with ANSI codes, rendered as colored HTML in web UI)
- Structured details (JSON): per-test breakdown with parameters, durations, reasons

## Execution Flow

### Single test

```
opp_ci run --project fifo --test smoke --skip-install
    │
    ├── create TestRun record (status=running)
    ├── install_project()    ← no-op in direct mode or --skip-install
    ├── run_test()           ← subprocess: opp_run_smoke_tests --output-format json
    │     ├── capture stdout (progress text + JSON on last line)
    │     ├── parse last line as JSON → details
    │     └── strip JSON line from stdout
    ├── create TestResult record (result_code, stdout, stderr, details)
    └── update TestRun (status=passed/failed, duration)
```

### Matrix run

```
opp_ci run-matrix --matrix inet-default --skip-install
    │
    ├── load TestMatrix from DB
    ├── expand_matrix() → list of jobs
    ├── install_project() for each unique project version
    └── for each job:
          ├── create TestRun record
          ├── run_test()
          ├── create TestResult record
          └── update TestRun status
```
