# Architecture

## High-level layout

```
GitHub (webhooks/API)
       │
       ▼
  opp_ci coordinator        ←──  CLI for manual control
       │                    ←──  Python client (REST API)
       ├── scheduler         ←──  matrix expansion, job queue
       ├── REST API          ←──  /api/* — workers, runs, github
       ├── web UI            ←──  FastAPI + Jinja2
       └── database          ←──  SQLite (local) or PostgreSQL (cloud)
                    ▲
                    │  poll / heartbeat / result
                    │
              ┌─────┴──────┐
              │  workers   │  ──  opp_env + opp_repl + Nix
              └────────────┘
```

The coordinator owns the database and the scheduler. Workers are
separate processes that may run on the same machine, on dedicated
self-hosted hardware, or in the cloud. See [workers.md](workers.md).

## Package layout

```
opp_ci/
├── __init__.py / __main__.py
├── cli.py              — Click CLI entry point
├── config.py           — env-var configuration
├── auth.py             — token roles, permission checks
├── client.py           — OppCiClient (Python REST wrapper)
├── scheduler.py        — matrix expansion, job dispatch
├── executor.py         — runs tests via opp_repl (direct or opp_env)
├── worker.py           — worker agent (poll/heartbeat/result loop)
├── catalog.py          — core project seed data
├── opp_env_adapter.py  — wraps opp_env CLI/API for catalog discovery
├── dependency.py       — resolve and pin dependency versions
├── compatibility.py    — pass/fail aggregation across version pairs
├── notes.py            — formatter for git note payloads
├── podman/            — Container images for isolated builds (Podman)
├── persistence.py     — get-or-create Test + create TestRun helpers
├── db/
│   ├── models.py       — SQLAlchemy models
│   ├── connection.py   — engine + session factory
│   └── migrations/     — Alembic
├── github/
│   ├── client.py       — GitHubClient (REST API v3 wrapper)
│   ├── webhook.py      — receiver, HMAC verification, dispatch
│   └── status.py       — post commit statuses / PR comments
└── web/
    ├── app.py          — FastAPI app and routes
    ├── api.py          — /api/* JSON endpoints
    ├── rollup.py       — hierarchical result aggregation
    └── templates/      — Jinja2 HTML
```

## Database schema

`opp_ci/db/models.py` — SQLAlchemy. For a field-by-field reference see
[data_model.md](data_model.md). The four `Test*` entities below were
introduced by the phase-1 test data model cutover, applied by wiping
and recreating the database; later schema changes are expected to
land via Alembic under `opp_ci/db/migrations/`.

| Model | Purpose |
|---|---|
| **Project** | name, opp_env_name, github_owner, github_repo, git_url, dependency_names |
| **Version** | project FK, opp_env_version, git_ref (branch/tag/SHA), label, `resolved_dependencies` JSON (e.g. `{"omnetpp": "6.1"}`) |
| **OS** | name, version, arch |
| **Compiler** | name, version |
| **TestMatrix** | project (by name), JSON config of versions × platforms × kinds, optional `opp_file` |
| **Worker** | name (unique), token, tags JSON, concurrency, status, last_heartbeat, current_job_count |
| **ApiToken** | token, name, role (readonly/submitter/worker/admin), enabled, created_at |
| **AutoTestRule** | project FK, rule_type (branch/pr/tag), pattern (glob), matrix FK, enabled |
| **Test** | Deduped coordinate row: plain `project` / `kind` / `mode` / `os` / `os_version` / `distro` / `distro_version` / `flavor` / `flavor_version` / `arch` / `compiler` / `compiler_version` / `isolation` / `toolchain` / `opp_file` string columns (no FK to Project / OS / Compiler — denormalised by design, so a run record survives catalog edits) keyed by SHA-256 `coord_hash`. Plus three mutable metadata columns: `name`, `expected_result_code`, `expected_result_description`. |
| **TestMatrixRun** | One row per matrix submission: matrix FK, `trigger` (manual/web/remote/webhook/schedule/rerun), `github_*` linkage fields, `created_at`. |
| **TestRun** | One attempt at a Test: test FK, optional matrix_run FK, worker FK, `git_ref`, `commit_sha`, `version`, `resolved_deps`, `lifecycle` (queued/running/finished/cancelled/timed_out), timestamps, and — populated iff lifecycle=finished — outcome columns `result_code` (PASS/FAIL/ERROR/SKIPPED), `stdout`/`stderr` (raw with ANSI), free-form `details` JSON. Also `system_snapshot` JSON captured at run start. |

Connection pool and engine factory in `opp_ci/db/connection.py`;
configured by `OPP_CI_DATABASE_URL`.

## Executor

`opp_ci/executor.py` invokes test commands in one of several
isolation × toolchain combinations:

| `--isolation` | `--toolchain` | What runs |
|---|---|---|
| `none` | `none` | Direct subprocess on the host using the host's installed compilers and opp_repl. |
| `none` | `nix` | `opp_env install <pkg-version>` then `opp_env run <pkg-version> -c <cmd>`. Reproducible Nix env. |
| `podman` | `none` | Run inside a Podman container with the host's project tree mounted. Image picked by `--os` / `--os-version` / `--compiler`. |
| `podman` | `nix` | Run inside Podman, with opp_env/Nix inside the container. |

Regardless of mode, the executor:

1. Obtains structured per-test results from opp_repl. The direct path
   (`isolation=none, toolchain=none`) imports `opp_repl.test.*` and calls
   the test function in-process, then inspects the returned object via
   `is_all_results_expected()` / `to_dict()` for the PASS/FAIL verdict
   and per-test details. The subprocess paths (opp_env or podman) treat
   the wrapper's exit code as the verdict — no JSON file is read back.
2. Captures the human-readable stdout/stderr with ANSI codes intact.
3. Returns `(result_code, stdout, stderr, details_json)` to the caller
   (worker or CLI). `details_json` is populated only on the direct path.

## Scheduler

`opp_ci/scheduler.py:expand_matrix()` produces the list of jobs for a
named matrix. The axes are cross-producted:

- Project versions (and resolved or pinned dependency versions)
- Build modes
- OS × OS version
- Compiler × compiler version
- Isolation × toolchain
- Kinds (test kind — `smoke`, `fingerprint`, …)

Platform axes accept two styles: combined strings (`Ubuntu 24.04`,
auto-parsed) or structured (`--os Ubuntu,Fedora --os-version 24.04,41`,
cross-producted).

For each expanded job the scheduler looks up (or creates) the matching
`Test` via `coord_hash` and inserts a queued `TestRun` parented to one
`TestMatrixRun` umbrella row. Workers pick the TestRuns up via
`/api/workers/poll`.

## Execution flow

### Single CLI run

```
opp_ci run --project fifo --kind smoke --skip-install
    │
    ├── get-or-create Test row (by coord_hash)
    ├── create TestRun (lifecycle=running) pointing at that Test
    ├── executor.install_project()  ←  no-op in direct mode / --skip-install
    ├── executor.run_test()         ←  subprocess, captures stdout + JSON details
    └── update TestRun (lifecycle=finished, result_code=PASS/FAIL/…,
                        stdout, stderr, details, duration)
```

### Matrix run (local)

```
opp_ci run-matrix --matrix inet-default
    │
    ├── load TestMatrix from DB
    ├── create TestMatrixRun (trigger=manual)
    ├── scheduler.expand_matrix() → list of job dicts
    ├── for each unique (project, version, deps): executor.install_project()
    └── for each job:
          ├── get-or-create Test (by coord_hash)
          ├── create TestRun under the TestMatrixRun
          ├── executor.run_test()
          └── update TestRun (lifecycle=finished + outcome columns)
```

### Matrix run (remote / worker pool)

```
opp_ci --remote run-matrix ...        Workers
    │                                    │
    └── POST /api/runs/matrix            │
          ├── create TestMatrixRun       │
          └── insert N TestRuns (queued) │
              + their Test rows on first sight
                                         │
                  ┌──────────────────────┘
                  ▼
          POST /api/workers/poll     → next queued TestRun (lifecycle=running)
          POST /api/workers/snapshot → optional system_snapshot at run start
          executor.run_test()
          POST /api/workers/result   → coordinator writes outcome onto the
                                       same TestRun row + posts GitHub status
```

## Web UI

FastAPI + Jinja2 in `opp_ci/web/`. See [web_ui.md](web_ui.md) for the
page map. The same FastAPI app serves both the HTML routes and the
JSON `/api/*` endpoints. ANSI-to-HTML is a Jinja filter applied at
render time so the DB keeps the raw escape codes.

## Cross-cutting concerns

- **Authentication** — bearer tokens with four roles. See
  [rest_api.md](rest_api.md#authentication).
- **GitHub** — webhooks in, commit statuses + PR comments + git notes
  out. See [github_integration.md](github_integration.md) and
  [git_notes.md](git_notes.md).
- **Configuration** — env vars only. See [configuration.md](configuration.md).
