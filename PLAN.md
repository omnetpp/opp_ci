# opp_ci — CI Support System for OMNeT++ Simulation Projects

A standalone project that orchestrates continuous testing of **any project supported by `opp_env`** — OMNeT++, INET, Simu5G, Veins, SimuLTE, and 60+ other simulation frameworks — across version/dependency/feature/platform matrices. Stores results in PostgreSQL, integrates with GitHub for branch and PR testing, uses `opp_env` for reproducible Nix-based builds, and leverages `opp_repl` as the test execution engine.

## Architecture Overview

```
GitHub (webhooks/API)
        │
        ▼
   opp_ci service          ←──  CLI for manual control
        │
        ├── job scheduler   ←──  version × feature × platform matrix
        ├── opp_env          ←──  reproducible Nix-based environment setup & builds
        ├── opp_repl        ←──  actual test execution (via OppEnvSimulationRunner)
        └── PostgreSQL      ←──  test results, job state, history
                │
                ▼
          Web UI (later)
```

## Role of Each Tool

- **opp_env** — installs and manages versions of all supported simulation projects (OMNeT++, INET, Simu5G, Veins, etc.) in isolated Nix environments. Provides the project catalog, version list, dependency graph (`required_projects`), and smoke test commands. Ensures every CI job runs in an identical, reproducible environment. Invoked via `opp_env install <pkg-version>` and `opp_env run <pkg-version> -c <cmd>`.
- **opp_repl** — runs tests (smoke, fingerprint, statistical, etc.) inside the environment set up by `opp_env`, using `OppEnvSimulationRunner`. No duplication of test logic.
- **opp_ci** — the orchestrator: expands test matrices, schedules jobs, invokes `opp_env` + `opp_repl`, stores results in Postgres, integrates with GitHub.

## Supported Projects (from opp_env)

`opp_ci` can test **any project in the `opp_env` catalog**. The `opp_env` database defines:
- Available versions of each project
- Dependency graph (`required_projects`) — e.g. simu5g-1.3.0 requires inet-4.5.x and omnetpp-6.1.x
- Smoke test commands per project
- Build commands, patch commands, environment setup

### Tier 1 — Active development, full test suite

| Project | Example versions | Dependencies | GitHub repo |
|---|---|---|---|
| **omnetpp** | 6.1, 6.0, 5.7, git | — | omnetpp/omnetpp |
| **inet** | 4.5, 4.4, 4.3, git | omnetpp | inet-framework/inet |
| **simu5g** | 1.3, 1.2, git | inet, omnetpp | Unipisa/Simu5G |
| **veins** | 5.3, 5.2, 5.1, git | omnetpp | sommer/veins |

### Tier 2 — Supported, smoke tests + build verification

| Project | Dependencies |
|---|---|
| **simulte** | inet, omnetpp |
| **plexe** | veins, omnetpp |
| **flora** | inet, omnetpp |
| **artery_allinone** | inet, veins, omnetpp |
| **core4inet** | inet, omnetpp |
| **nesting** | inet, omnetpp |
| ...and 50+ more | varies |

Tier assignment is configurable per project — any project can be promoted to Tier 1 by adding test matrix configs.

### How opp_env Drives the Matrix

When configuring a test run, `opp_ci` queries the `opp_env` database to:
1. **Resolve dependencies** — e.g. testing `simu5g-1.3.0` automatically includes compatible `inet` and `omnetpp` versions
2. **Validate version combos** — reject incompatible combinations based on `required_projects` constraints
3. **Generate smoke tests** — use the project's built-in `smoke_test_commands` as a baseline test
4. **Discover available versions** — `opp_env list` populates the version selectors in the web UI

## Current GitHub Actions CI Landscape

### OMNeT++ (`omnetpp/omnetpp`) — 3 workflows

| Workflow | Trigger | Platform | What it does |
|---|---|---|---|
| `build_tests.yml` | push/PR to `master`, `omnetpp-6.x` | Ubuntu 24.04 | Compile-only build tests (`make test_build`) |
| `main_tests.yml` | push/PR to `master`, `omnetpp-6.x` | Ubuntu 24.04 | Full build + test suite: common, core, envir, featuretool, anim, models, makemake, makemake2, fingerprint, sqliteresultfiles, scave_results_api, scave_charttemplates, scave_analysis, scave_multi_project, scave_workspace |
| `build_release.yml` | push/PR to `master`, `omnetpp-6.x` | Ubuntu 24.04 + matrix | Build release packages → test install natively (Ubuntu, macOS, Windows) + in Docker (Ubuntu, Fedora, Arch, openSUSE, AlmaLinux, Debian) |

### INET (`inet-framework/inet`) — 11 workflows (1 disabled)

| Workflow | Trigger | Mode | What it does |
|---|---|---|---|
| `build-linux.yml` | push/PR to `master` (src/** only) | debug, release | Native Linux build |
| `build-macos.yml` | weekly (Saturday) | debug, release | Cross-build macOS via Docker (`ci-inet` image) |
| `build-windows.yml` | weekly (Saturday) | debug, release | Cross-build Windows via Docker (`ci-inet` image) |
| `fingerprint-tests.yml` | push/PR to `master` | debug, release | Fingerprint tests, 4-way parallel split |
| `statistical-tests.yml` | push to `master` | release | Statistical tests, uploads `.diff` artifacts |
| `chart-tests.yml` | push to `master` | release | Chart tests, uploads image diff artifacts |
| `feature-tests.yml` | weekly (Saturday) | release | Feature tests, 16-way parallel split |
| `module-tests.yml` | push to `master` | debug | Module tests (`inet_run_module_tests`) |
| `unit-tests.yml` | push to `master` | debug | Unit tests (`inet_run_unit_tests`) |
| `other-tests.yml` | push to `master` | debug | Packet, queueing, protocol tests (matrix of 3 test dirs) |
| `validation-tests.yml` | push to `master` | release | Validation tests |
| `speed-tests.yml` | **disabled** | release | Speed tests — requires perf counters unavailable on GH runners |

### Key Patterns in Current CI

- All INET workflows check out `omnetpp/omnetpp` at `omnetpp-6.x` alongside INET — **hardcoded version coupling**
- Shared ccache across workflows via `actions/cache`
- Cross-platform builds use Docker image `ghcr.io/inet-framework/ci-inet:6.3.0-251029`
- Fingerprint tests split 4 ways, feature tests split 16 ways for parallelism
- Speed tests impossible on GitHub-hosted runners (no hardware perf counters) — a good candidate for self-hosted / `opp_ci` runners
- Each workflow independently installs system packages and builds omnetpp+inet — **lots of duplication**
- Only fingerprint tests and build-linux run on PRs; most tests only run on push to master

### What opp_ci Improves

- **Eliminate version coupling** — test any omnetpp × inet version combination via matrix configs
- **Eliminate duplication** — `opp_env` handles environment setup once; `opp_repl` handles all test execution
- **Enable speed tests** — run on self-hosted hardware with perf counters
- **Cross-version testing** — test INET against multiple OMNeT++ versions, and vice versa
- **Historical tracking** — Postgres stores all results; trends, regressions, and comparisons over time
- **PR-level feedback** — run the full test suite on PRs, not just fingerprints and builds

## Staged Development Plan

Development is split into stages that each deliver a usable increment. Each stage builds on the previous one. Later stages can be re-prioritized as needs evolve.

---

### Stage 1 — Local single-project smoke test (MVP) ✅

**Goal**: Run a single test of a single opp_env project from the command line on the local machine, store the result in the database.

- [x] Create project skeleton: `pyproject.toml`, `opp_ci/` package (see [Project Structure](#project-structure))
- [x] Minimal DB schema — `TestRun` and `TestResult` tables + `Project`, `Platform`, `TestMatrix` (see [Database Schema](#database-schema))
- [x] Config from env vars: `OPP_CI_DATABASE_URL` (default: SQLite for local use, PostgreSQL in the cloud)
- [x] Direct mode (`OPP_CI_USE_OPP_ENV=0`): run `opp_repl` test commands directly (passes `--load @opp -p <project>`)
- [x] opp_env mode (`OPP_CI_USE_OPP_ENV=1`): call `opp_env install <pkg-version>` + `opp_env run <pkg-version> -c <cmd>`
- [x] CLI: `opp_ci run --project <name> --test smoke` (supports comma-separated test types)
- [x] Store result (pass/fail, duration, stdout/stderr) in the database
- [x] CLI: `opp_ci list-runs`, `opp_ci show-run <id>`, `opp_ci show-results`
- [x] CLI: `opp_ci seed-projects`, `opp_ci list-projects`
- **Deliverable**: can run `opp_ci run --project inet-4.5 --test smoke` and query the result with `opp_ci show-results`

---

### Stage 2 — Web UI: read-only results ✅

**Goal**: Browse test results via local web server (and later in the cloud).

- [x] FastAPI + Jinja2 server-rendered pages (`opp_ci/web/`)
- [x] Run locally with `opp_ci serve` — connects to the same SQLite/PostgreSQL database as the CLI
- [x] Dashboard (`/`): recent activity, summary stats
- [x] Test runs list (`/runs`): filterable table with status, duration
- [x] Test run detail (`/runs/{run_id}`): results, stdout/stderr
- [x] Test results search (`/results`): multi-dimensional filter + summary/detailed display modes
- [x] Comparison page (`/compare`): side-by-side diff of two runs or branches
- **Deliverable**: run `opp_ci serve`, open `http://localhost:8000` to browse results locally

#### Result Filter and Display Modes

The results page (`/results`) provides a **multi-dimensional filter** where every stored dimension can be independently constrained:

| Filter dimension | Examples |
|---|---|
| Project | inet, omnetpp, simu5g, veins, ... |
| Project version | 4.5, 4.6, master, git |
| Dependency versions | omnetpp: 6.1, 6.0; inet: 4.5 |
| OS type / version | Ubuntu 24.04, Fedora 41, macOS 15 |
| Compiler type / version | gcc-14, clang-18 |
| Build mode | release, debug |
| Test type | smoke, fingerprint, statistical, ... |
| Result status | PASS, FAIL, ERROR, SKIP |

Dimensions left unset act as wildcards — the query returns all matching results regardless of that dimension's value.

Results are displayed in **two switchable formats**:

1. **Detailed view** — one row per stored result. Every dimension value and metadata (duration, timestamp, stdout link) is shown. Suitable for drilling down into individual failures.

2. **Summary view** — rows are collapsed across the unfiltered dimensions to produce a compact digest:
   - If all results for a collapsed group share the same status (e.g. all PASS), the group is shown as **a single line** with the common status.
   - If statuses differ, the line shows a short breakdown (e.g. "18 PASS, 2 FAIL") with expand/drill-down.
   - Grouping is hierarchical: first by project+version, then by test type, then by remaining dimensions — so the summary stays compact even when many platform/compiler variants are tested.
   - Example: searching for "INET 4.6" without fixing OS, compiler, omnetpp version, or test type might return 200 result rows in detailed view, but in summary view collapses to a handful of lines like:
     ```
     inet 4.6 / smoke        — PASS (all 12 combinations)
     inet 4.6 / fingerprint  — 46 PASS, 2 FAIL  [expand]
     inet 4.6 / statistical  — PASS (all 8 combinations)
     ```

---

### Stage 3 — Multiple test types, project versions, and git branches ✅

**Goal**: Support all test types for Tier 1 projects, test specific versions/branches, query results from CLI.

- [x] Executor supports all test types: smoke, fingerprint, statistical, feature, speed, sanitizer, chart, release, build, all (via `COMMAND_MAP`)
- [x] `Project` table with seed data from catalog (`opp_ci seed-projects`)
- [x] `Version` table and version resolution
- [x] Dependency resolution: query `opp_env` `required_projects` to auto-resolve compatible dependency versions
- [x] Git branch/tag/commit support: test a specific git ref of a project
  - CLI: `opp_ci run --project inet --ref topic/my-feature --test smoke`
  - Executor checks out the specified ref before building/testing
  - For opp_env projects: use `opp_env run <project>-git -c <cmd>` with appropriate git ref
  - For local/direct mode: `git checkout <ref>` in the project working copy
- [x] Version labels: map human-readable names (e.g. "master", "4.5", "topic/my-feature") to git refs
- [x] Track tested ref (branch, tag, or SHA) in `TestRun` for result filtering and history
- [x] Support testing multiple refs in a single matrix: `--refs "master,topic/my-feature"`
- [x] Dependency version pinning: test inet branch X against a specific omnetpp version
  - [x] Version model stores `resolved_dependencies` JSON for pinning
  - [x] CLI: `opp_ci run --pin omnetpp=6.1` validates and pins dependency versions
  - [x] CLI: `opp_ci resolve-deps inet-4.5 --pin omnetpp=6.0.2` for standalone resolution
- [x] CLI: `opp_ci run --project <name> --test fingerprint,smoke` — comma-separated test types
- [x] CLI: `opp_ci list-runs`, `opp_ci show-run <id>`, `opp_ci show-results --project <name> --test <type> --status <status>`
- **Deliverable**: can test any Tier 1 project at any git ref with any test type, browse results via CLI

---

### Stage 4 — Test matrices and platform support ✅

**Goal**: Define and run multi-dimensional test matrices across versions, platforms, and build modes.

- [x] `Platform` and `TestMatrix` tables in DB schema
- [x] Matrix expansion: scheduler expands a matrix config into individual jobs (`expand_matrix`)
- [x] Platform axes: 4 separate dimensions — os, os_version, compiler, compiler_version
- [x] Dual-mode platform axes: combined strings (e.g. "Ubuntu 24.04") auto-parsed, or structured (separate name/version lists cross-producted)
- [x] Build mode axis: debug, release
- [x] Version matrix: test a project against multiple dependency versions
- [ ] Feature axis: INET feature flags
- [x] Sequential local execution; jobs stored in DB (status: queued → running → passed/failed/error)
- [x] CLI: `opp_ci run-matrix --matrix <name>`, `opp_ci create-matrix`, `opp_ci list-matrices`, `opp_ci seed-matrices`
- [x] CLI `create-matrix` args: `--name`, `--project`, `--project-versions`, `--builds`, `--os`, `--os-version`, `--compiler`, `--compiler-version`, `--tests`
- [x] Structured JSON test results from `opp_repl` (`--result-file`) parsed and stored in `TestResult.details`; human-readable text kept in stdout
- [x] ANSI escape codes stored raw in DB, converted to colored HTML at render time
- [x] Run detail page shows per-test results table (test name, result, duration, reason)
- [x] Results page filters and rollup updated for all 4 platform dimensions
- **Deliverable**: can define a matrix like "inet master × omnetpp {6.1, 6.0} × {debug, release} × fingerprint" and run it

---

### Stage 5 — Remote workers and deployment

**Goal**: Deploy the coordinator to a cloud VPS, run jobs on remote workers.

- Worker agent: `opp_ci worker start --coordinator <url> --token <token>` (see [Worker Registration](#worker-registration))
- Workers poll coordinator for jobs, report results back via REST API
- Worker capability tags (os, arch, compilers, perf-counters) matched to job requirements
- Deploy coordinator (scheduler + API + Postgres) to a cloud VPS (see [Deployment Architecture](#deployment-architecture))
- Python client library for remote job submission (see [Python Client](#python-client-from-your-machine))
- CLI works both locally and against remote coordinator: `opp_ci --remote run --project inet-4.5 --test smoke`
- Token-based authentication (see [Security](#security))
- **Deliverable**: coordinator running on a VPS, workers on self-hosted machines, jobs submitted remotely

---

### Stage 6 — GitHub integration

**Goal**: Automatically test on push/PR, post status checks back to GitHub.

- **AutoTestRule** model in DB — configures which branches/PRs are automatically tested:
  - project FK
  - rule_type: `branch`, `pr`, `tag`
  - pattern: glob/regex for matching (e.g. `master`, `topic/*`, `*` for all PRs)
  - matrix FK: which test matrix to run when triggered
  - enabled: bool
  - Example rules:
    - "test inet master with full matrix on every push"
    - "test inet PRs with smoke only"
    - "test omnetpp tags matching `v6.*` with release matrix"
- Webhook receiver at `/api/github/webhook` (see [GitHub integration](#phase-3--github-integration-details))
- Listen for `push` and `pull_request` events on configured repos
- Match incoming events against AutoTestRule patterns → enqueue matching matrix jobs
- GitHub API client: post commit statuses, PR comments with result summaries (see [GitHub API client](#phase-3--github-integration-details))
- Reuse token from `~/.ssh/github_repo_token` (same as opp_repl)
- **Deliverable**: push to inet master → tests auto-triggered → green/red status check on GitHub

---

### Stage 7 — Web UI: actions and admin

**Goal**: Start tests, manage matrices, and administer workers from the web.

- Start test run page (`/runs/new`): form to select project, versions, platforms, test types, features, trigger mode; validate version compatibility via opp_env
- Matrix configuration page (`/matrices`): create/edit/clone/delete matrix templates, link to webhooks
- Admin page (`/admin`): worker status, system health, token management, project registration
- Re-run and cancel actions from run detail and runs list pages
- **Deliverable**: full web-based management of the CI system

---

### Stage 8 — Tier 2 projects and ecosystem

**Goal**: Extend testing to all opp_env projects.

- Auto-import all projects from opp_env catalog as Tier 2 (see [Tier 2 projects](#tier-2--supported-smoke-tests--build-verification))
- Default matrix for Tier 2: build + smoke test on a single reference platform
- Promote projects to Tier 1 by adding custom matrix configs
- Nightly scheduled runs for Tier 2 projects
- Cross-project compatibility reports: which versions of project X work with which versions of project Y
- **Deliverable**: 60+ projects tested automatically, compatibility dashboard

---

### Summary

| Stage | What you get | Key components |
|---|---|---|
| 1 | Run one smoke test, store in DB | executor, minimal DB, CLI | ✅ done |
| 2 | Web results browsing (local + cloud) | FastAPI, dashboard, runs list, search, comparison | ✅ done |
| 3 | All test types, multiple projects | test types, project catalog, dependency resolution | ✅ done |
| 4 | Multi-dimensional matrices | matrix expansion, platform/compiler axes, scheduler | ✅ done |
| 5 | Remote execution | worker agent, coordinator deployment, Python client |
| 6 | GitHub automation | webhooks, status checks, PR comments |
| 7 | Web management | start runs, manage matrices, admin |
| 8 | Full ecosystem | Tier 2 projects, nightly runs, compatibility reports |

---

## Implementation Details

### Database Schema

`opp_ci/db/models.py` — SQLAlchemy models:

- **Project** — name, opp_env_name, github_owner, github_repo, git_url, tier (1/2), dependency_names
- **Version** — project FK, opp_env_version, git_ref (branch/tag/SHA), label, resolved_dependencies (JSON: {dep_project: dep_version})
- **OS** — name, version, arch
- **Compiler** — name, version
- **TestMatrix** — project FK, list of version combos + platforms + features
- **AutoTestRule** — project FK, rule_type (branch/pr/tag), pattern (glob), matrix FK, enabled
- **TestRun** — matrix entry, git_ref, version, timestamp, status (queued/running/passed/failed/error), triggerer (manual/webhook/schedule)
- **TestResult** — run FK, test_type (smoke/fingerprint/statistical/…), test_name, result_code, duration, stdout/stderr (raw with ANSI codes), details (JSON: structured per-test results from opp_repl)

Migrations via Alembic (`opp_ci/db/migrations/`). Connection pool in `opp_ci/db/connection.py`, config from env vars.

### Configuration

`opp_ci/config.py` — YAML/TOML config file for: DB connection, GitHub tokens, project definitions, default matrices. Environment variable overrides.

### Phase 3 — GitHub Integration Details

- **Webhook receiver** (`opp_ci/github/webhook.py`) — listen for `push` and `pull_request` events, map to matrix configs, enqueue jobs, post status checks
- **GitHub API client** (`opp_ci/github/client.py`) — reuse token from `~/.ssh/github_repo_token`, post commit statuses, PR comments with result summaries, query PR metadata

### Web UI Pages

Server-rendered via FastAPI + Jinja2 (`opp_ci/web/`):

- **Dashboard** (`/`) — project health badges, recent activity, quick-start links
- **Project** (`/projects/{project}`) — summary, per-branch status, history chart, run button
- **Runs list** (`/runs`) — filterable/sortable table, bulk actions (cancel, re-run)
- **Run detail** (`/runs/{run_id}`) — metadata (incl. OS, compiler info), per-test results table from structured details, colored stdout/stderr (ANSI→HTML), re-run buttons
- **Results search** (`/results`) — multi-dimensional filter (every axis independently constrainable), two display modes: **Detailed** (one row per result) and **Summary** (collapsed compact digest), CSV export
- **Matrix heatmap** (`/matrix/{project}`) — interactive heatmap, switchable axes, drill-down
- **Comparison** (`/compare`) — side-by-side diff of runs or branches, regression detection
- **Start run** (`/runs/new`) — project/version/platform/test selector, matrix templates, version validation
- **Matrix config** (`/matrices`) — CRUD for matrix templates, webhook linking
- **Admin** (`/admin`) — worker status, system health, tokens, project registration

## Test Matrices (examples)

| Dimension | Values |
|---|---|
| Target project | omnetpp, inet, simu5g, veins, simulte, ... (any opp_env project) |
| Target version | `master`, `git`, or any released version from `opp_env list` |
| Dependency versions | auto-resolved from `opp_env` `required_projects`, or manually pinned |
| OS type | Ubuntu, Fedora, macOS, Windows |
| OS version | Ubuntu: 22.04, 24.04; Fedora: 40, 41; macOS: 14, 15; Windows: 11 |
| Compiler type | gcc, clang |
| Compiler version | gcc: 12, 13, 14; clang: 16, 17, 18 |
| Build mode | release, debug |
| Features | TCP_lwIP, TCP_NSC, VoIPStream, … (INET feature flags) |
| Test types | build, fingerprint, statistical, chart, feature, module, unit, packet, queueing, protocol, validation, smoke, sanitizer, speed |

Not every combination is tested — the matrix config defines which axes to cross.

## Deployment Architecture

### Components and Where They Run

```
┌─────────────────────────────────────────────────────────┐
│  Cloud VPS (e.g. Hetzner, DigitalOcean, AWS Lightsail)  │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  opp_ci web  │  │  opp_ci API  │  │  PostgreSQL  │   │
│  │  (FastAPI)   │  │  + webhooks  │  │              │   │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘   │
│         │                 │                              │
│  ┌──────┴─────────────────┴───────┐                     │
│  │       opp_ci scheduler         │                     │
│  │  (picks jobs from queue,       │                     │
│  │   dispatches to workers)       │                     │
│  └────────────────┬───────────────┘                     │
└───────────────────┼─────────────────────────────────────┘
                    │ job dispatch (REST / message queue)
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐
   │ Worker 1 │ │ Worker 2 │ │ Worker N │  (self-hosted or cloud)
   │ opp_env  │ │ opp_env  │ │ opp_env  │
   │ opp_repl │ │ opp_repl │ │ opp_repl │
   │ Nix      │ │ Nix      │ │ Nix      │
   └─────────┘ └─────────┘ └─────────┘
```

### Three Access Patterns

1. **Web browser** → `https://ci.omnetpp.org` — view results, start test runs, manage matrices
2. **Python client from your machine** → `opp_ci submit --project inet --ref master` or Python API: `opp_ci.client.submit_run(...)` — talks to the REST API
3. **GitHub webhooks** → GitHub sends push/PR events to `https://ci.omnetpp.org/api/github/webhook` — auto-triggers test runs

### Hosting Options

| Option | Pros | Cons | Cost |
|---|---|---|---|
| **Hetzner Cloud VPS** | Cheap, EU-based, good perf, easy setup | Manual sysadmin | ~€5–20/mo for coordinator; workers extra |
| **DigitalOcean Droplet** | Simple, good docs, managed Postgres available | Slightly pricier | ~$12–24/mo |
| **AWS Lightsail** | Predictable pricing, easy scaling | AWS complexity creep | ~$10–20/mo |
| **Self-hosted server** | Full control, can double as worker with perf counters for speed tests | Requires hardware, network, uptime | One-time hardware cost |
| **Hybrid** | Coordinator in cloud, workers on self-hosted hardware | More complex networking | Cloud + hardware |

**Recommended: Hybrid approach**
- **Coordinator** (web UI + API + scheduler + Postgres) on a cheap cloud VPS (Hetzner/DigitalOcean) — always accessible, handles webhooks, serves the web UI
- **Workers** on self-hosted machines — access to hardware perf counters for speed tests, no per-minute cost for long-running test suites, can be beefy machines

### Network Setup

- **Domain**: `ci.omnetpp.org` (or similar) pointing to the cloud VPS
- **HTTPS**: Let's Encrypt via Caddy or nginx reverse proxy
- **Webhook URL**: `https://ci.omnetpp.org/api/github/webhook` — registered in GitHub repo settings
- **API URL**: `https://ci.omnetpp.org/api/` — used by Python client and web frontend
- **Worker connection**: workers poll the coordinator for jobs (outbound-only, no inbound ports needed on workers)

### Python Client (from your machine)

```python
from opp_ci.client import OppCiClient

ci = OppCiClient(url="https://ci.omnetpp.org/api", token="...")

# Submit a test run
run = ci.submit_run(
    project="inet",
    omnetpp_ref="master",
    inet_ref="topic/my-feature",
    test_types=["fingerprint", "statistical"],
    modes=["release"],
)

# Check status
ci.get_run(run.id)

# Query results
results = ci.search_results(project="inet", test_type="fingerprint", status="failed")
```

### Worker Registration

Workers register with the coordinator on startup and poll for jobs:

```bash
# On the worker machine (self-hosted)
opp_ci worker start \
    --coordinator https://ci.omnetpp.org/api \
    --token <worker-token> \
    --tags "linux,amd64,perf-counters" \
    --concurrency 4
```

Workers self-describe their capabilities (OS, arch, compilers, features) — the scheduler matches jobs to compatible workers.

### Security

- **API authentication**: token-based (separate tokens for admin, submitter, worker, read-only)
- **Webhook verification**: GitHub webhook secret for HMAC signature validation
- **Worker auth**: per-worker tokens, revocable from admin page
- **HTTPS everywhere**: coordinator behind Caddy/nginx with auto-TLS

## Key Design Decisions

- **opp_env for reproducible builds** — Nix-based isolation ensures every CI job gets the exact same dependencies, regardless of host.
- **opp_repl is the test engine** — opp_ci orchestrates; opp_repl builds and runs tests. No duplication of test logic. opp_repl always prints human-readable text to stdout; structured JSON results go to `--result-file` (a temp file read by opp_ci).
- **Postgres for persistence** — structured querying of historical results, easy aggregation for dashboards.
- **Start minimal** — CLI + DB first, web later. Get value from structured result storage immediately.
- **GitHub-native** — webhooks for automation, status checks for feedback, same token infrastructure as opp_repl.

## Project Structure

```
opp_ci/
├── README.md
├── pyproject.toml
├── alembic.ini
├── opp_ci/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py
│   ├── cli.py
│   ├── client.py
│   ├── catalog.py
│   ├── dependency.py
│   ├── scheduler.py
│   ├── executor.py
│   ├── worker.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── connection.py
│   │   └── migrations/
│   │       └── env.py
│   ├── github/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   └── webhook.py
│   └── web/
│       ├── __init__.py
│       ├── api.py
│       ├── app.py
│       └── templates/
│           ├── base.html
│           ├── dashboard.html
│           ├── project.html
│           ├── runs.html
│           ├── run_detail.html
│           ├── results.html
│           ├── matrix.html
│           ├── compare.html
│           ├── run_new.html
│           ├── matrices.html
│           └── admin.html
└── tests/
    └── __init__.py
```
