# Concepts

`opp_ci` is the orchestration layer of a three-tool stack that tests
OMNeT++ simulation projects across version, dependency, platform, and
test matrices.

This document is both the conceptual overview and the vocabulary
reference: it explains what each concept *is*, what role it plays, and
how it relates to the others. For the layered architecture see
[architecture.md](architecture.md).

---

## The three-tool stack

| Tool | Role |
|---|---|
| **opp_env** | Installs and manages versions of supported simulation projects in isolated Nix environments. Provides the project catalog, version list, dependency graph (`required_projects`), smoke test commands, and build commands. Invoked as `opp_env install <pkg-version>` and `opp_env run <pkg-version> -c <cmd>`. |
| **opp_repl** | Runs the tests (smoke, fingerprint, statistical, feature, chart, …) inside the environment set up by `opp_env`. Always prints human-readable text to stdout. opp_ci's executor talks to opp_repl two ways: by importing `opp_repl.test.*` and calling the test functions in-process (the direct path; reads per-test details from the returned object's `to_dict()`), or as a subprocess where the wrapper script's exit code is the verdict. |
| **opp_ci** | The orchestrator: expands test matrices, schedules jobs, invokes `opp_env` and `opp_repl`, stores results in the database, integrates with GitHub. No test logic of its own. |

The boundary is strict: opp_ci never duplicates test logic, and opp_repl
never owns the environment. Anything reproducible about a CI run is
either in opp_env's recipe or in the matrix config.

---

## Process roles

### Coordinator

The single `opp_ci serve` instance that owns the database. Exposes the
REST API at `/api/*` and the web UI. Workers and `--remote` CLI clients
all talk to one coordinator. Identified by `OPP_CI_COORDINATOR_URL`.

### Worker

A separate `opp_ci worker start` process that polls the coordinator for
queued jobs, runs them via the [executor](#executor), and posts results
back. Workers may live on the coordinator host, on dedicated self-hosted
hardware, or in the cloud. They need only outbound connectivity — no
inbound port is required. The coordinator is the source of truth for a
worker's name, tags, and concurrency; the worker fetches them at start.

### Executor

The in-process component ([opp_ci/executor.py](../opp_ci/executor.py))
that actually invokes test commands. Lives both inside workers and
inside the CLI for local runs. Selects one of four combinations of
[Isolation](#isolation) × [Toolchain](#toolchain): direct host,
opp_env-on-host, Podman, and Podman-with-opp_env. Always returns
`(result_code, stdout, stderr, details_json)` to its caller.

### Scheduler

The matrix-expansion engine
([opp_ci/scheduler.py](../opp_ci/scheduler.py)). Turns a
[TestMatrix](#testmatrix) config into a list of job dicts via
cross-product over its [axes](#axis). For each job it looks up (or
creates) the matching [Test](#test) and inserts a queued
[TestRun](#testrun) parented to a single [TestMatrixRun](#testmatrixrun)
row; workers consume the TestRuns.

### REST API client

[opp_ci/client.py](../opp_ci/client.py) (`OppCiClient`). Python wrapper
used by `--remote` CLI calls and by other tools. Authenticates with an
[ApiToken](#apitoken). See [python_client.md](python_client.md).

---

## Domain model (database)

The objects below are the SQLAlchemy models in
[opp_ci/db/models.py](../opp_ci/db/models.py). They are the persistent
shape of every other concept on this page. For a field-level
reference (every column, type, default, and relationship) see
[data_model.md](data_model.md).

### Project

A simulation codebase that can be tested. Carries `opp_env_name`,
`github_owner/repo`, `git_url`, and `dependency_names`. Mirrors an entry
in opp_env's catalog when one exists. See the
[supported projects](#supported-projects) section below.

### Version

A specific version of a [Project](#project): either a released label
(`6.1`, `4.5`) or a moving target (`git`, `master`). Holds the
`opp_env_version`, the `git_ref`, and `resolved_dependencies` — a JSON
map of dep-name → dep-version pinned at this version (e.g.
`{"omnetpp": "6.1"}`). Versions are seeded by `opp_ci sync-catalog`
from opp_env.

### OS

A `(name, version, arch)` triple, e.g. `Ubuntu / 24.04 / x86_64`.
Referenced by [TestMatrix](#testmatrix) configs and recorded on
[TestRun](#testrun) rows.

### Compiler

A `(name, version)` pair, e.g. `gcc / 13`. Referenced by
[TestMatrix](#testmatrix) configs and recorded on [TestRun](#testrun)
rows. When [Toolchain](#toolchain) is `nix`, the pair must be one that
opp_env understands.

### TestMatrix

A JSON configuration describing the cross-product to test. Attached to
a [Project](#project). Defines which [axes](#axis) to vary (`kinds`,
`modes`, `versions`, `refs`, `deps`, `os`, `compiler`, `isolation`,
`toolchain`, `features`). Expanded by the [scheduler](#scheduler) into
[jobs](#job) that are persisted as [Test](#test) + [TestRun](#testrun)
rows under one [TestMatrixRun](#testmatrixrun) umbrella.

The `name` is **optional**: a named matrix is reusable and can be run
by name; an anonymous one (name = NULL, e.g. an ad-hoc inline run) is a
one-shot. Named matrices are unique; any number may be anonymous.
Anonymous matrices display as `(anonymous #id)`.

### Test

A deduped row holding the immutable coordinate of "what is being
tested" — `project`, `kind`, `mode`, the platform stack
(`os`/`distro`/`flavor` + their versions, `arch`, `compiler`/version),
the execution environment (`isolation`, `toolchain`), and `opp_file`.
A SHA-256 `coord_hash` over that field set is the dedup key: matrix
expansion looks the row up and inserts a new one only on first sight.
A single `name` column is the only mutable field and sits outside the
hash — it is an optional, editable label (unique when set) that lets a
test be found and re-run by name, independent of dedup. Expected
outcomes live in
[ExpectedTestResult](#expectedtestresult), keyed by `test_id`, *not*
on the Test row. Every [TestRun](#testrun) points at exactly one
`Test` via `test_id`.

### ExpectedTestResult

An append-only edit log of expected outcomes per [Test](#test). Each
row carries an `expected_result_code` ([TestResultCode](#testresultcode)
or NULL for an explicit *retraction*), an optional
`expected_result_description`, `reason`, `set_by`, and `set_at`.
"Current expectation" is the row with the highest `set_at`; no row at
all means "no expectation ever declared". Edits apply forward-only:
historical [TestVerdict](#testverdict) cells pin the specific row
that was in force at recording time via `expectation_id`, so old
matrix-run rollups stay reconstructible after a re-grade.

### TestMatrixRun

One row per submission of a [TestMatrix](#testmatrix) — the umbrella
that groups the child [TestRuns](#testrun) and [TestVerdicts](#testverdict)
spawned from a single expansion. Carries the [Trigger](#trigger)
source (`tag` for release-tag pushes; see
[Release-tag trigger](#release-tag-trigger)), a `ref` snapshot of the
triggering tag, the GitHub linkage fields, and a **stored** rollup —
counter columns (`pass_count`/`fail_count`/`error_count`,
`expected_count`/`unexpected_count`/`unknown_count`, `cache_hit_count`,
`total_count`) plus `actual_summary` and the three-state
[Verdict](#verdict) — refreshed transactionally as each child cell
finalizes. `completed_at` is set when every cell is done.

### TestVerdict

One row per cell of a [TestMatrixRun](#testmatrixrun). Pins the
[TestRun](#testrun) whose outcome this cell attributes (a fresh row
on a cache miss, a pre-existing finished row on a [Cache hit](#cache)
— marked by `cache_hit=True`) plus the
[ExpectedTestResult](#expectedtestresult) row in force at recording
time. Carries the three-state [Verdict](#verdict) and `recorded_at`.
The cell's lifecycle (queued / running / finished / cancelled) is
derived from the underlying `TestRun.lifecycle` — not stored — so
there is only ever one source of truth for "is it done yet".

### Verdict

Three-state grade for a [TestVerdict](#testverdict) (and, rolled up,
for its parent [TestMatrixRun](#testmatrixrun)):

- **EXPECTED** — actual outcome matched the expectation in force at
  recording time. Release-ready.
- **UNEXPECTED** — actual diverged from the expectation (wrong
  outcome, or unexpected ERROR). Regression candidate.
- **UNKNOWN** — actual known, but no expectation existed (or the most
  recent ExpectedTestResult was a retraction). Declare an expectation
  to characterise the cell.

The rollup verdict on a `TestMatrixRun` is `UNEXPECTED` if any cell
is, else `UNKNOWN` if any cell is, else `EXPECTED`. "Release-ready"
is then a one-liner: `TestMatrixRun.verdict == EXPECTED` on the row
triggered by the release tag.

### TestRun

For an exhaustive field-by-field reference (CLI flag, REST field,
defaults, validation, and lifecycle), see
[single_test_parameters.md](single_test_parameters.md).

One row per attempt to run a [Test](#test) — the unit of work. Points
at its `Test` via `test_id` (so all coordinate fields like `project` /
`kind` / `os` / `compiler` are read off the joined Test row), and
optionally at its [TestMatrixRun](#testmatrixrun) via `matrix_run_id`
(NULL for ad-hoc CLI / REST runs). Carries the per-attempt context
(`git_ref`, `commit_sha`, `version`, `resolved_deps`), the
[TestRunLifecycle](#testrunlifecycle), the [Worker](#worker-model) that
ran it, the timestamps, and — once the lifecycle reaches `finished` —
the outcome columns (`result_code`, `stdout`, `stderr`, `details`).
Also carries a `system_snapshot` JSON blob captured at run start.

### TestRunLifecycle

State-machine enum on TestRun:

- `queued` — inserted by scheduler / CLI; awaiting a worker.
- `running` — claimed by a worker via `/api/workers/poll`.
- `finished` — worker reported a result; `result_code` is populated.
- `cancelled` — user-cancelled while still queued. (Running runs are
  left to finish — the worker can't be interrupted.)
- `timed_out` — coordinator's watchdog reclaimed the run after the
  worker stopped heartbeating.

### TestResultCode

Outcome enum stored in `TestRun.result_code` (populated iff
`lifecycle == finished`), in
[ExpectedTestResult](#expectedtestresult)`.expected_result_code`, and
as the `actual_summary` rollup on
[TestMatrixRun](#testmatrixrun): `PASS`, `FAIL`, `ERROR`, `SKIPPED`.
`ERROR` distinguishes infrastructure failure (build, env, worker
crash) from genuine test failure.

### effective_status

A view-side property on `TestRun` that collapses the two enums into a
single label for templates and rollup: returns the `result_code`
value (`"PASS"` / `"FAIL"` / `"ERROR"` / `"SKIPPED"`) when the run is
finished, otherwise the `lifecycle` value (`"queued"` / `"running"` /
`"cancelled"` / `"timed_out"`).

### Worker (model)

Persistent registration record (separate from the running
[Worker process](#worker)): `name`, auto-generated `token`, `tags` JSON,
`concurrency`, `status` (`online`/`offline`/`busy`), `last_heartbeat`,
`current_job_count`. The coordinator updates `status` based on
heartbeat freshness.

### ApiToken

A bearer token created via `opp_ci token create`. Carries a
[Role](#role). Authenticates REST API calls. Separate from worker
tokens, but checked through the same auth path
([opp_ci/auth.py](../opp_ci/auth.py)).

### AutoTestRule

A "if this kind of event matching this pattern hits this project, run
this matrix" record. Fields: `project_id`, `rule_type`
(`branch`/`pr`/`tag`), `pattern` (fnmatch glob), `matrix_id`
(nullable — null means smoke-only). Evaluated by the
[webhook receiver](#webhook-receiver). Tag-rule matches set
`TestMatrixRun.trigger="tag"` and `TestMatrixRun.ref=<tag-name>`; see
[Release-tag trigger](#release-tag-trigger).

### Release-tag trigger

The standard "is this release ready to publish?" workflow. A
maintainer pushes a release tag (e.g. `v4.5.3`); the GitHub webhook
matches any [AutoTestRule](#autotestrule) with `rule_type=tag` and a
fitting `pattern`; the bound matrix expands into a fresh
[TestMatrixRun](#testmatrixrun) with `trigger="tag"` and
`ref="v4.5.3"`. The [Cache](#cache) absorbs unchanged cells; new ones
run on workers. When every cell finalizes, the stored
[Verdict](#verdict) is the answer — `EXPECTED` ⇒ ship; `UNEXPECTED` ⇒
a regression to investigate; `UNKNOWN` ⇒ at least one cell isn't yet
characterised, declare an expectation and re-run. The project page's
"Latest release run" card surfaces this without a click.

### Cache

Content-addressable result cache on [TestRun](#testrun). At submit
time each cell's
[`cache_fingerprint`](data_model.md#cache-fingerprint) is computed
over its full input set — coordinates, resolved git SHA, resolved dep
SHAs, version. A finished `TestRun` with the same fingerprint is
reused: a new [TestVerdict](#testverdict) is inserted with
`cache_hit=True` pointing at that prior run, graded immediately
against the currently-in-force [expectation](#expectedtestresult). No
new `TestRun` row is created. Re-running an unchanged matrix is
near-instant; moving refs (`master`, `inet-git`) are detected via the
SHA in the fingerprint and re-executed. `--no-cache` on
`opp_ci run-matrix` (and `no_cache` on `POST /api/matrix-runs`)
forces a fresh `TestRun` per cell. Expectations are deliberately
*not* in the cache key — a cached cell still grades against today's
expectation, not the one that was in force when the cached run
finished.

---

## Supported projects

`opp_ci` can test any project in the opp_env catalog. The actively
developed core (omnetpp, inet, simu5g, veins) ships in the seed
catalog; the remaining ~60 opp_env projects (simulte, plexe, flora,
artery_allinone, core4inet, nesting, …) are imported on demand by
`opp_ci sync-catalog`.

| Project | Example versions | Dependencies |
|---|---|---|
| omnetpp | 6.1, 6.0, 5.7, git | — |
| inet | 4.5, 4.4, 4.3, git | omnetpp |
| simu5g | 1.3, 1.2, git | inet, omnetpp |
| veins | 5.3, 5.2, 5.1, git | omnetpp |

How heavily a project is tested is determined by the
[TestMatrix](#testmatrix) entries attached to it and any
[AutoTestRule](#autotestrule) triggers — see
[GitHub Integration](github_integration.md). Newly imported projects
get a default `build + smoke` matrix on the
[reference platform](#reference-platform), which you can extend or
replace without any code change.

---

## Test-matrix concepts

A matrix is a named cross-product over independent axes. The standard
axes:

| Axis | Examples |
|---|---|
| Target project | omnetpp, inet, simu5g, veins, simulte, … |
| Target version | `master`, `git`, or any released version |
| Dependency versions | auto-resolved or manually pinned |
| Build mode | release, debug |
| OS type | Ubuntu, Fedora, macOS, Windows |
| OS version | per OS |
| Compiler type | gcc, clang |
| Compiler version | per compiler |
| Isolation | none, podman |
| Toolchain | none, nix |
| Features | INET feature flags — *reserved; not yet implemented by `expand_matrix()`* |
| Kinds | the test kinds defined in `COMMAND_MAP` (`smoke`, `fingerprint`, …) — see [test_matrix_dimensions.md](test_matrix_dimensions.md#axis-kind) for the canonical list |

Not every combination is tested — the matrix config defines which axes
to cross. Matrix expansion happens in
[scheduler.expand_matrix()](../opp_ci/scheduler.py). For an
axis-by-axis reference (config syntax, defaults, validation rules,
interactions) see
[test_matrix_dimensions.md](test_matrix_dimensions.md).

### Axis

One dimension of a [TestMatrix](#testmatrix). Each present axis
multiplies the job count; an axis omitted from the config contributes a
single implicit value.

### Job

A single expanded matrix entry — i.e. a [TestRun](#testrun) row with
status `queued`. The two terms are used interchangeably in the docs.

### Matrix expansion

The act of cross-producting a [TestMatrix](#testmatrix)'s axes into a
list of jobs. Performed by
[scheduler.expand_matrix()](../opp_ci/scheduler.py).

### Cross-product / Cartesian indicator

When the [rollup](#rollup) merges runs into summary rows, it checks
whether the varying dimensions form a complete cross-product. The
indicator (`●`/`○`) tells the reader at a glance whether the merged
group is dense or sparse.

### Kind

The kind of test the job runs (`smoke`, `fingerprint`, `statistical`,
`feature`, `chart`, `speed`, `sanitizer`, `release`, `opp`, `build`,
`all`) — the canonical list is in
[test_matrix_dimensions.md](test_matrix_dimensions.md#axis-kind) and
mirrors `executor.COMMAND_MAP`. Determines what opp_repl is told to
do. Stored on the [Test](#test) row as the `kind` column; the matrix
axis is `kinds:`, the CLI flag is `--kind` (or `--kinds`), and the
REST / Python-client field is `kind`. (Before the phase-1 schema
cutover this was called `test` everywhere.) Each kind can produce one
or many results inside the same TestRun: the structured per-test
breakdown lives in `TestRun.details` (JSON), not in separate rows.

### Mode

Build mode — typically `release` or `debug`. Crossed independently of
other axes.

### Reference platform

The platform spec used for auto-generated default matrices, configured
via `OPP_CI_REFERENCE_PLATFORM` (default `Ubuntu 24.04/gcc-13`). Newly
imported projects get a `build + smoke` matrix on this platform.

### How opp_env drives the matrix

When configuring a test run, opp_ci queries the opp_env catalog (via
the `opp_env_adapter` module) to:

1. **Resolve dependencies** — testing `simu5g-1.3.0` automatically
   includes compatible `inet` and `omnetpp` versions.
2. **Validate version combos** — incompatible combinations are rejected
   based on opp_env's `required_projects` constraints.
3. **Generate smoke tests** — the project's built-in
   `smoke_test_commands` are used as the baseline test.
4. **Discover available versions** — `opp_env info --raw` is parsed by
   `opp_env_adapter` to populate the version selectors in the web UI and
   matrix configs.

`opp_ci sync-catalog` upserts the discovered projects and versions into
opp_ci's database and generates default matrices for new entries.

---

## Execution environment

### Isolation

How the job's filesystem and OS are isolated from the host. `none` =
direct subprocess on the worker; `podman` = inside a Podman container
image chosen by the most-specific platform level (`flavor`, else
`distro`, else `os`) plus the compiler coordinates. Image building is
driven by `opp_ci image build` / `image build-matrix`.

### Toolchain

Where the C++ toolchain comes from. `none` = the worker's installed
compilers and opp_repl; `nix` = `opp_env install` then `opp_env run`,
giving a fully reproducible build environment. Orthogonal to
[Isolation](#isolation): the four combinations are all valid.

### Capability tag

A string on a [Worker](#worker-model) declaring what it can do. The
dispatcher only honours a specific scheme — `podman`, `nix`,
`os:<lc>[-<ver>]`, `distro:<lc>-<ver>`, `flavor:<lc>-<ver>`,
`compiler:<lc>-<ver>`, `arch:<lc>` — anything else is documentation. See
[workers.md](workers.md#capability-tags) for the full table and
dispatch rules. Set at registration time (`--tags` or `--auto-tags`).

### Reproducible build (Nix environment)

A build performed under `Toolchain=nix`, i.e. inside an opp_env-managed
Nix store. Guarantees the same compiler, libraries, and dependency
versions for every CI run that selects that environment.

---

## Dependency model

### required_projects

opp_env's name for a project version's hard dependencies. Read by
[opp_ci/dependency.py](../opp_ci/dependency.py) via `opp_env info`.

### Resolved dependencies

The fully pinned dep-name → dep-version map associated with a
[Version](#version) or a [TestRun](#testrun) (column
`resolved_dependencies` / `resolved_deps`). Either taken from the
Version, computed by `dependency.resolve()` against opp_env, or
overridden by a [Pin](#pin).

### Pin

A `--pin <dep>=<ver>` override that forces a specific dependency
version for the duration of a run or matrix. Overlays
`resolved_dependencies`. Repeatable.

---

## Trigger and lifecycle

### Trigger

Why a [TestRun](#testrun) exists. Recorded on the row as one of:

- `manual` — created via `opp_ci run` / `run-matrix` locally.
- `remote` — created via `--remote` (REST submitter).
- `webhook` — created by the [webhook receiver](#webhook-receiver).
- `schedule` — created by a periodic scheduler (reserved).

### Heartbeat

Periodic ping from a worker to the coordinator
(`POST /api/workers/heartbeat`). Updates `last_heartbeat`. If no
heartbeat arrives within `OPP_CI_WORKER_HEARTBEAT_TIMEOUT` seconds, the
worker is marked `offline` and any in-flight jobs are reclaimed for
re-dispatch.

### Poll loop

Worker side: periodic `POST /api/workers/poll` to ask for the next
queued job (interval `OPP_CI_WORKER_POLL_INTERVAL`). On finish the
worker posts the outcome to `POST /api/workers/result`.

---

## Authentication

### Role

The privilege level carried by an [ApiToken](#apitoken) or worker
token. Four levels, monotonically increasing:

| Role | Includes |
|---|---|
| `readonly` | View runs, results, workers |
| `submitter` | + Submit runs |
| `worker` | + Poll, heartbeat, report results |
| `admin` | + Register workers, manage tokens, manage rules |

Implementation in [opp_ci/auth.py](../opp_ci/auth.py).

### Remote mode

The CLI flag `--remote` switches commands from in-process operation to
REST calls against the coordinator. Uses `OPP_CI_COORDINATOR_URL` and
`OPP_CI_API_TOKEN`. The same Click command tree serves both modes.

---

## GitHub integration

### Webhook receiver

`POST /api/github/webhook`
([opp_ci/github/webhook.py](../opp_ci/github/webhook.py)). Verifies
`X-Hub-Signature-256` against `OPP_CI_GITHUB_WEBHOOK_SECRET`, dispatches
`push`/`pull_request`/`ping`, matches the event against the project's
[AutoTestRules](#autotestrule), and queues runs.

### Commit status

A pending/success/failure/error indicator that opp_ci posts to GitHub
for the head SHA of each run. Set to `pending` on enqueue and updated
to the final state when the worker reports results. Identified by the
configurable context string `OPP_CI_GITHUB_STATUS_CONTEXT` (default
`opp_ci`).

### PR comment

A markdown comment posted to the PR by opp_ci, refreshed in place via a
hidden HTML marker so successive runs update one comment instead of
spamming new ones.

### Git note

A summary of CI results attached to the tested commit under
`refs/notes/ci`. Delivered indirectly: opp_ci writes the payload, then
triggers the target repo's `ci-notes.yml` workflow which pushes the
note. Visible to developers via `git log --notes=ci` after fetching the
notes ref. See [git_notes.md](git_notes.md).

### ci-notes.yml workflow

A GitHub Action installed in target repos. Fetches pending notes from
`GET /api/notes/{owner}/{repo}` and pushes them under `refs/notes/ci`
using the built-in `GITHUB_TOKEN`. Lets opp_ci stay free of any
`Contents: Write` permission on target repos.

### Webhook secret

The HMAC-SHA256 key configured both in the GitHub webhook setting and
as `OPP_CI_GITHUB_WEBHOOK_SECRET` on the coordinator. The receiver
rejects events whose signature doesn't match.

### Token model (two-token)

opp_ci uses two separate GitHub tokens:

- `OPP_CI_GITHUB_TOKEN` — for posting commit statuses and PR comments
  (classic `repo` scope or fine-grained equivalent).
- `OPP_CI_GITHUB_ACTIONS_TOKEN` — for `workflow_dispatch` only
  (`Actions: Write`). opp_ci never holds `Contents: Write` itself.

---

## Catalog and seeding

### opp_env catalog

The full list of projects and versions known to opp_env. opp_ci mirrors
it into its own DB.

### Core seed catalog

The actively developed projects (`omnetpp`, `inet`, `simu5g`, `veins`)
populated by `opp_ci seed-projects` from
[opp_ci/catalog.py](../opp_ci/catalog.py).

### sync-catalog

Command (`opp_ci sync-catalog`) that walks the opp_env catalog and
upserts every project + version it finds, then generates a default
`build + smoke` matrix on the [reference platform](#reference-platform)
for any newly imported project.

### smoke_test_commands

opp_env-supplied minimal "did it build and run" command list for each
project. Used as the baseline test in the auto-generated default
matrix.

---

## Result presentation

### Rollup

The aggregation algorithm in
[opp_ci/web/rollup.py](../opp_ci/web/rollup.py) that merges TestRuns
sharing the same status into summary rows, marking each dimension as
constant or varying and indicating whether the varying dimensions form
a full cross-product (see
[Cartesian indicator](#cross-product--cartesian-indicator)).

### Primary / extra dimensions

A rollup distinction: **primary dimensions** (project, test, mode,
os, distro, distro_version, flavor, compiler, compiler_version,
git_ref) are always considered; **extra dimensions** (os_version,
flavor_version, isolation, toolchain, commit_sha, version)
participate in classification but only show as columns when they
actually vary on the page.

### ANSI-preserving storage

opp_ci stores raw stdout/stderr (including ANSI escape codes) in the
database. A Jinja filter converts them to colored HTML at render time.
This keeps the DB representation lossless and human-replayable.

---

## How the concepts connect

A **Project** has many **Versions** and many **TestMatrices**.
**AutoTestRules** bind a Project + GitHub event pattern to a
TestMatrix. The **scheduler** expands a TestMatrix's **axes** into
**jobs**, persisting each one as a queued **TestRun** parented to a
deduped **Test** row and grouped under one **TestMatrixRun** umbrella.
**Workers** poll the **coordinator** for queued TestRuns, run them via
the **executor** (under chosen **Isolation** × **Toolchain**), and
post the outcome (`result_code`, `stdout`, `stderr`, `details`) back
onto the same TestRun row. Each TestMatrixRun carries a **trigger**
explaining why it exists (manual / remote / webhook / schedule) and,
when GitHub-triggered, the linkage fields the **GitHub integration**
uses to post a **commit status** and a **PR comment**; when the batch
finishes, the `ci-notes.yml` **workflow** is dispatched to deliver a
**git note** to the developer. **ApiTokens** and worker tokens, gated
by **role**, control who can do which of these things. The **rollup**
then summarises many TestRuns into the page-level grid.

---

## What opp_ci improves over per-project GitHub Actions

Historically each project (omnetpp, inet, simu5g, …) has run its own
hand-rolled GitHub Actions workflows. Common limitations of that setup:

- **Hardcoded version coupling** — INET workflows check out
  `omnetpp/omnetpp` at a fixed `omnetpp-6.x` branch.
- **Duplicated environment setup** — each workflow re-installs system
  packages, builds omnetpp, then builds inet.
- **No cross-version testing** — testing INET against multiple OMNeT++
  versions requires N hand-written workflows.
- **No historical tracking** — results are scoped to a single Actions
  run; trends and regression hunting are impossible.
- **PR-level feedback is thin** — typically only fingerprint and
  build-linux run on PRs.
- **No speed tests** — GitHub-hosted runners have no hardware
  performance counters.

opp_ci addresses all of these:

- **Decoupled versions** — any omnetpp × inet (× …) combination via
  matrix configs.
- **One environment, reused** — `opp_env` builds an environment once
  per (project-version, dependency-versions) tuple; multiple tests
  share it.
- **Cross-version matrices** — test INET against multiple OMNeT++
  versions in a single matrix.
- **Postgres history** — every run, every result, every dimension
  stored; trends and regressions are queries.
- **Full PR feedback** — webhook-driven full-matrix runs, status
  checks, PR comments.
- **Self-hosted workers** — speed tests with perf counters become
  possible.

---

## Design decisions

- **opp_env for reproducible builds** — Nix-based isolation guarantees
  every CI job gets the exact same dependencies.
- **opp_repl is the test engine** — opp_ci orchestrates; opp_repl
  builds and runs tests. No test logic is duplicated.
- **Structured results from opp_repl in-process** — human-readable text
  goes to stdout; on the direct path (isolation=none, toolchain=none)
  opp_ci imports the test function and reads per-test details from the
  returned object's `to_dict()`. Subprocess paths use the wrapper's exit
  code as the verdict. ANSI codes are stored raw in the DB and converted
  to colored HTML at render time.
- **Postgres for persistence** — structured querying of historical
  results, easy aggregation for dashboards. SQLite is supported for
  local development.
- **GitHub-native** — webhooks for automation, status checks for
  feedback, fine-grained PATs for least privilege (see
  [git_notes.md](git_notes.md)).
