# Getting Started

If you hit a snag, the [troubleshooting guide](troubleshooting.md) covers the
common first-run problems.

## Prerequisites

- Python 3.10+
- `opp_repl` installed and on PATH (for test commands like `opp_run_smoke_tests`)

opp_env is **only** required when:
- you use `--toolchain nix` on a run (reproducible Nix environment), or
- you run `opp_ci sync-catalog` to import the full project catalog.

For the direct host-mode runs shown below, opp_repl alone is enough.

For cloud deployment, you'll also need:
- PostgreSQL
- Nix + `opp_env` (for reproducible environments)

## Installation

```bash
# for general use
uv tool install opp_ci

# for development purposes
cd ~/workspace/opp_ci
uv tool install --with-editable ../opp_repl -e .
```

`source setenv` activates the local `.venv` (if present), exports
`OPP_CI_ROOT`, prepends the repo's `bin/` to `PATH`, and adds the repo to
`PYTHONPATH`. Re-source it in new shells.

For PostgreSQL support:
```bash
pip install -e ".[postgres]"
```

## Running Your First Test

The example uses **fifo** — a small tutorial simulation bundled with
`opp_repl`. It builds and runs without Nix/opp_env, so it's the fastest
way to verify your installation. (For real projects like `inet`, see
[Pointing opp_ci at a real project](#pointing-opp_ci-at-a-real-project)
below.)

```bash
opp_ci init-db
opp_ci run --project fifo --kind smoke --skip-install
```

This will:
1. Create `opp_ci.db` (SQLite) in the current directory
2. Get-or-create a `Test` row for the `(project=fifo, kind=smoke, …)` coordinate
3. Import `opp_repl.test.smoke.run_smoke_tests` and call it in-process (no Nix/opp_env needed)
4. Read structured per-test details from the returned result's `to_dict()`
5. Write the outcome (pass/fail, stdout, stderr, per-test details) onto the
   matching `TestRun` row in the database

The full list of test kinds (`smoke`, `fingerprint`, `statistical`,
`feature`, `speed`, `sanitizer`, `chart`, `release`, `build`, `opp`,
`all`) and what each one does is in
[test_matrix_dimensions.md](test_matrix_dimensions.md#axis-kind).

### Pointing opp_ci at a real project

For projects other than `fifo`, opp_ci's executor needs to know where each
project's working tree lives on the host. By default it looks under
`$OPP_CI_PROJECT_DIR/<project>` (default `OPP_CI_PROJECT_DIR=.`).

The typical local-dev layout is one workspace directory with sibling
checkouts:

```
~/workspace/
├── opp_ci/
├── opp_repl/
├── inet/
├── omnetpp/
└── …
```

then `export OPP_CI_PROJECT_DIR=~/workspace`. Per-project overrides
(`OPP_CI_PROJECT_DIR_INET_4_5=…`) are also available — see
[configuration.md](configuration.md#executor-project-source-location).

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
# Open http://localhost:8080
```

The web UI shows:
- Dashboard with recent activity and stats
- Runs list with filtering
- Run detail with colored stdout (ANSI→HTML) and per-test results table
- Results page with multi-dimensional filtering

### Direct DB inspection

The coordinate fields live on `tests`; the lifecycle and outcome on
`test_runs`. Join the two to see "what was run, how did it end":

```bash
sqlite3 opp_ci.db "
  SELECT r.id, t.project, t.kind, r.lifecycle, r.result_code
  FROM test_runs r
  JOIN tests t ON t.id = r.test_id;
"
```

## Test Matrices

Define a matrix to run multiple test configurations:

```bash
opp_ci create-matrix \
  --name fifo-default \
  --project fifo \
  --builds "release,debug" \
  --kinds "smoke,fingerprint"

opp_ci run-matrix --matrix fifo-default --skip-install
```

For platform-specific matrices:

```bash
opp_ci create-matrix \
  --name inet-platforms \
  --project inet \
  --os "Ubuntu 24.04,Fedora 41" \
  --compiler "gcc-14,clang-18" \
  --kinds "smoke,fingerprint"
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
| podman | none | container with apt/dnf-installed compiler (`opp-ci-runner:<slug>-none-…`) |
| podman | nix | container with Nix + opp_env, omnetpp baked in (`opp-ci-runner:<slug>-nix-omnetpp-…`) |

If neither flag is given, both default to `none` — i.e. just run on the host.

Example: test INET on Ubuntu 26.04 + clang 22 in a container

```bash
opp_ci image build --os ubuntu --os-version 26.04 \
                   --compiler clang --compiler-version 22 --toolchain host
opp_ci run --project inet-4.5 --kind smoke \
           --isolation podman --toolchain none \
           --os Ubuntu --os-version 26.04 --compiler clang --compiler-version 22
```

Same axes work in matrix configs (lists are cross-producted):

```bash
opp_ci create-matrix --name inet-platforms --project inet \
    --kinds smoke --builds release \
    --os Ubuntu,Fedora --os-version 26.04,42 \
    --compiler clang --compiler-version 22 \
    --isolation podman --toolchain none
```

## Worker Tags and Job Dispatch

Workers advertise their capabilities as a list of tags; the coordinator
only hands a queued run to a worker whose tags cover the run's
requirements. The full tag vocabulary and dispatch rules live in
[workers.md](workers.md#capability-tags) — in short:

- `podman` / `nix` — required when the run uses `--isolation podman` or
  `--toolchain nix` respectively.
- `os:<lc-name>-<version>`, `compiler:<lc-name>-<version>`,
  `arch:<lc-arch>` — required when the run names that field.

Anything outside this scheme (e.g. `linux`, `gcc-13`, `perf-counters`)
is accepted but never gates dispatch.

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
opp_ci create-matrix --name fifo-default --project fifo --builds "release,debug" --kinds "smoke,fingerprint"
```
