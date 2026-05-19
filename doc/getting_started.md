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
2. Import `opp_repl.test.smoke.run_smoke_tests` and call it in-process (no Nix/opp_env needed)
3. Read structured per-test details from the returned result's `to_dict()`
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

## Selecting an Execution Environment

Each test run declares *how* it should execute via two orthogonal axes:

- `--isolation none|podman` — run on the worker's host, or inside a Podman
  container built for a specific OS + compiler combination. Podman is used
  rather than Docker because it runs rootless by default (no daemon, no
  privileged setup) while remaining CLI-compatible.
- `--toolchain none|nix` — use whatever compiler is installed on the host
  (or inside the container), or pull the toolchain from Nix via `opp_env`.

The four combinations:

| isolation | toolchain | Behavior |
|---|---|---|
| none | none | direct on worker, host packages (no Nix, no container) |
| none | nix | `opp_env run …` on the worker (today's default behavior) |
| podman | none | container with apt/dnf-installed compiler (`opp-ci-runner:host-…`) |
| podman | nix | container with Nix + opp_env inside (`opp-ci-runner:nix-…`) |

If neither flag is given, both default to `none` — i.e. just run on the host.

Example: test INET on Ubuntu 26.04 + clang 22 in a container

```bash
opp_ci image build --os ubuntu --os-version 26.04 \
                   --compiler clang --compiler-version 22 --toolchain host
opp_ci run --project inet-4.5 --test smoke \
           --isolation podman --toolchain none \
           --os Ubuntu --os-version 26.04 --compiler clang --compiler-version 22
```

Same axes work in matrix configs (lists are cross-producted):

```bash
opp_ci create-matrix --name inet-platforms --project inet \
    --tests smoke --builds release \
    --os Ubuntu,Fedora --os-version 26.04,42 \
    --compiler clang --compiler-version 22 \
    --isolation podman --toolchain none
```

## Worker Tags and Job Dispatch

Workers advertise their capabilities as a list of tags; the coordinator only
hands a queued run to a worker whose tags cover the run's requirements.

Recognised tag conventions:

| Tag | Meaning |
|---|---|
| `podman` | Podman installed; can pull/run `opp-ci-runner:*` images |
| `nix` | Nix + opp_env installed on the host |
| `os:<name>-<ver>` | Host OS, lowercased — e.g. `os:ubuntu-24.04`, `os:fedora-42` |
| `compiler:<name>-<ver>` | Host compiler, lowercased — e.g. `compiler:gcc-14` |

A run requires a subset of these tags depending on its execution environment:

- `isolation=podman` → `{podman}`
- `isolation=none, toolchain=nix` → `{nix, os:…, compiler:…}` (os/compiler tags only required if the run names them)
- `isolation=none, toolchain=none` → `{os:…, compiler:…}`

Register a worker with the appropriate tags:

```bash
opp_ci worker register --name worker-1 \
    --tags podman,nix,os:ubuntu-24.04,compiler:gcc-14 --concurrency 4
```

## Rebuilding the Database

To start fresh (drops all data):

```bash
rm -f opp_ci.db
opp_ci init-db
opp_ci create-matrix --name fifo-default --project fifo --builds "release,debug" --tests "smoke,fingerprint"
```
