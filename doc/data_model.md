# Data Model

This guide describes the persistent data model of opp_ci: every table,
its columns, and how the tables relate. It is the field-level reference
for the schema sketched in
[architecture.md](architecture.md#database-schema) and named in
[concepts.md](concepts.md#domain-model-database).

The authoritative source is [opp_ci/db/models.py](../opp_ci/db/models.py)
(SQLAlchemy). The database backend is selected by `OPP_CI_DATABASE_URL`
(default `sqlite:///opp_ci.db`; production uses PostgreSQL). Phase 1 of
the [test data model redesign](../plan/done/test-data-model-phase-1-schema.md)
split what used to be a single `test_runs` row into four entities
(`tests`, `test_matrices`, `test_matrix_runs`, `test_runs`) and was
applied by wiping and recreating the database — there is no migration
chain to chase. Phase 2 of the
[redesign](../plan/done/test-data-model-redesign.md) introduced two
more tables (`expected_test_results`, `test_verdicts`) and a stored
rollup on `test_matrix_runs`; that revision (`d4a8c91e7350`) ships as
an Alembic migration that backfills counters from existing rows.

---

## Tables at a glance

Grouped by role; the detail sections below appear in the same order.

**Catalog** — what can be tested:

| Table | Purpose | Key relations |
|---|---|---|
| [`projects`](#project) | Project catalog (mirrors opp_env) | parent of `versions`, `auto_test_rules` |
| [`versions`](#version) | Project version + pinned deps | child of `projects` |
| [`os_entries`](#os) | Catalog of `(name, version, arch)` triples | referenced by matrix configs |
| [`compilers`](#compiler) | Catalog of `(name, version)` pairs | referenced by matrix configs |

**Test data model** — what was run, what was expected, and how it
graded:

| Table | Purpose | Key relations |
|---|---|---|
| [`test_matrices`](#testmatrix) | Named (or anonymous) cross-product configuration | parent of `test_matrix_runs`; referenced by `auto_test_rules` |
| [`tests`](#test) | Deduped immutable coordinate row + one editable label | parent of `test_runs`, `expected_test_results`, `test_verdicts` |
| [`expected_test_results`](#expectedtestresult) | Append-only edit log of expected outcomes per `Test` | child of `tests`; pinned by `test_verdicts.expectation_id` |
| [`test_matrix_runs`](#testmatrixrun) | One row per submission of a `TestMatrix` — groups its children, owns the GitHub linkage and the stored rollup | child of `test_matrices`; parent of `test_runs` and `test_verdicts` |
| [`test_runs`](#testrun) | One row per attempt to run a `Test` — carries lifecycle + outcome + cache fingerprint | child of `tests` + `test_matrix_runs` + `workers` |
| [`test_verdicts`](#testverdict) | One row per cell of a matrix run: pins a TestRun + the expectation in force, carries the EXPECTED/UNEXPECTED/UNKNOWN verdict | child of `test_matrix_runs` + `tests` + `test_runs` + `expected_test_results` |

**Workers and automation** — what does the running, and what triggers it:

| Table | Purpose | Key relations |
|---|---|---|
| [`workers`](#worker) | Worker registrations | referenced by `test_runs` |
| [`auto_test_rules`](#autotestrule) | Event-pattern → matrix bindings | child of `projects` + `test_matrices` |

**Auth** — who is allowed to do what:

| Table | Purpose | Key relations |
|---|---|---|
| [`api_tokens`](#apitoken) | Bearer tokens for REST access | — |
| [`users`](#user) | Web-UI human users (local + GitHub) | — |

### Relationship diagram

```
projects ───┬─< versions
            ├─< auto_test_rules >── test_matrices ──< test_matrix_runs ──┐
            │                                                            │
            └── (referenced by name from tests)                          │
                                                                         │
                            tests ──< test_runs >─────────────────-──────┤
                              │           ▲                              │
                              │           │                              │
                              │        workers                           │
                              │                                          │
                              ├─< expected_test_results                  │
                              │           ▲                              │
                              │           │                              │
                              └─< test_verdicts >───────────────────-────┘

os_entries     compilers     api_tokens     users
   (standalone catalog / auth tables)
```

`<` = "has many", read left-to-right. `os_entries`, `compilers`, and
`projects` are referenced by string value from `tests` and from matrix
JSON configs, not by foreign key — see
[Denormalised columns](#denormalised-columns).

---

## Project

`projects` — a simulation codebase that can be tested. Mirrors an entry
in the opp_env catalog when one exists.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `name` | string, unique, not null | Display and CLI name (`omnetpp`, `inet`, …) |
| `opp_env_name` | string, nullable | Matching name in opp_env's catalog |
| `github_owner` | string, nullable | Owner half of the GitHub coordinate |
| `github_repo` | string, nullable | Repo half of the GitHub coordinate |
| `git_url` | string, nullable | Clone URL (used when no opp_env entry exists) |
| `dependency_names` | JSON list | Names of projects this one depends on (e.g. `["omnetpp"]`) |

Populated by `opp_ci seed-projects` (core projects) and
`opp_ci sync-catalog` (everything else opp_env knows about). See
[concepts.md → Catalog and seeding](concepts.md#catalog-and-seeding).

---

## Version

`versions` — a specific version of a Project: a released label
(`6.1`, `4.5`) or a moving target (`git`, `master`).

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `project_id` | int FK → `projects.id`, not null | |
| `opp_env_version` | string, nullable | Version label as known to opp_env |
| `git_ref` | string, nullable | Branch / tag / SHA |
| `label` | string, nullable | Display label (often equals `opp_env_version`) |
| `resolved_dependencies` | JSON, nullable | Pinned map e.g. `{"omnetpp": "6.1"}` |

`resolved_dependencies` is the version-level pinning; a TestRun may
override it via its own `resolved_deps` column. See
[concepts.md → Dependency model](concepts.md#dependency-model).

---

## OS

`os_entries` — catalog of OS coordinates referenced by matrix configs.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `name` | string, not null | e.g. `Ubuntu`, `Fedora`, `macOS`, `Windows` |
| `version` | string, nullable | e.g. `24.04`, `41` |
| `arch` | string, default `"x86_64"` | e.g. `x86_64`, `aarch64` |

Helper: `OS.label` formats as `"<name> <version>"`. Not foreign-keyed
from `tests` — see [Denormalised columns](#denormalised-columns).

---

## Compiler

`compilers` — catalog of compiler coordinates referenced by matrix
configs.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `name` | string, not null | e.g. `gcc`, `clang` |
| `version` | string, nullable | e.g. `13` |

Helper: `Compiler.label` formats as `"<name>-<version>"`. Not
foreign-keyed from `tests` — see [Denormalised columns](#denormalised-columns).

---

## TestMatrix

`test_matrices` — cross-product configuration. Expanded by the
scheduler into `Test` + `TestRun` rows under a `TestMatrixRun` umbrella.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `name` | string, unique, **nullable** | Optional identifier (`inet-default`, …). NULL = anonymous (one-shot, ad-hoc run); named matrices are reusable and unique. |
| `project` | string, not null | Project name (stored by name, not FK) |
| `opp_file` | string, nullable | Optional `.opp` file the matrix targets |
| `config` | JSON, not null | The axes (versions, modes, os, compiler, isolation, toolchain, kinds, …) |
| `created_at` | datetime, default now | |

Axis semantics and JSON shape: see
[test_matrix_dimensions.md](test_matrix_dimensions.md).

---

## Test

`tests` — a deduped row holding the immutable coordinate of "what we
test", plus a single editable display label. One `Test` is shared by
every `TestRun` that retries / reruns the same coordinate; matrix
expansion looks up the row by `coord_hash` and inserts a new one only
on first sight. Expected outcomes are no longer columns on `Test` —
they live in [`expected_test_results`](#expectedtestresult), keyed by
`test_id` and audited as an append-only log.

### Coordinate columns (immutable after creation)

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `project` | string, not null | Project name (denormalised, see below) |
| `kind` | string, not null | Entry in `executor.COMMAND_MAP` (`smoke`, `fingerprint`, …). Renamed from the legacy `test` column. |
| `mode` | string, nullable | Build mode (`release`, `debug`) |
| `os` | string, nullable | OS family — `Linux`, `Windows`, `MacOS` |
| `os_version` | string, nullable | OS version (Windows/MacOS only; NULL for Linux) |
| `distro` | string, nullable | Linux distribution (`ubuntu`, `fedora`, …). NULL for non-Linux. |
| `distro_version` | string, nullable | Distribution version. NULL when no distro. |
| `flavor` | string, nullable | Distribution variant (`kubuntu`, …). NULL when not a flavor. |
| `flavor_version` | string, nullable | Flavor version. Falls back to `distro_version` when NULL. |
| `arch` | string, nullable | CPU architecture (`amd64`, `aarch64`) |
| `compiler` | string, nullable | Compiler name |
| `compiler_version` | string, nullable | Compiler version |
| `isolation` | string, nullable | `none` / `podman`; `None` is read as `none` |
| `toolchain` | string, nullable | `none` / `nix`; `None` is read as `none` |
| `opp_file` | string, nullable | Same as TestMatrix.opp_file when set by matrix |
| `coord_hash` | string(64), unique, not null | SHA-256 hex over the canonical JSON of every coordinate column — the dedup key |
| `created_at` | datetime, default now | |

### Editable column

`name` is the only mutable column on a `Test` row. It is deliberately
excluded from `coord_hash` so renaming never produces a new row.

| Column | Type | Notes |
|---|---|---|
| `name` | string, nullable, unique-when-set | Optional human label; lets a test be found and re-run by name |

### `coord_hash` field list

Computed by `compute_test_coord_hash(coord)` in
[opp_ci/db/models.py](../opp_ci/db/models.py) as the SHA-256 of the
sorted-keys canonical JSON over:

```
project, kind, mode,
os, os_version, distro, distro_version, flavor, flavor_version, arch,
compiler, compiler_version,
isolation, toolchain,
opp_file
```

Treat this set as frozen for phase 1; adding or removing a field
re-keys every existing row.

### Relationships

- `runs` → many [`TestRun`](#testrun), `cascade="all, delete-orphan"`
  (deleting a Test cascades to its TestRuns).
- `expected_results` → many [`ExpectedTestResult`](#expectedtestresult)
  (backref from `expected_test_results.test`).
- Pointed at by [`TestVerdict.test_id`](#testverdict).

---

## ExpectedTestResult

`expected_test_results` — append-only edit log of the expected outcome
for a `Test`. The "current expectation" is the row with the highest
`set_at`; a row whose `expected_result_code` is NULL is an explicit
*retraction*, distinguishable from never-set and itself audited.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `test_id` | int FK → `tests.id`, not null, indexed | The Test this expectation describes |
| `expected_result_code` | enum [`TestResultCode`](#testresultcode), nullable | PASS / FAIL / ERROR / SKIPPED, or NULL = retraction |
| `expected_result_description` | text, nullable | Free-form note |
| `reason` | text, nullable | Why the expectation was set (issue link, justification) |
| `set_by` | string, nullable | Account name (CLI sets `"cli"`, REST records the API token name, web records the user's `display_name`) |
| `set_at` | datetime, not null, indexed | When the row was inserted |

Edits are inserts; nothing is ever updated. Use
`persistence.get_current_expectation(session, test_id)` to read the
current expectation in one ordered query. Use
`persistence.insert_expectation(session, …)` (or one of its CLI / REST
front-ends — see [cli_reference.md](cli_reference.md) and
[rest_api.md](rest_api.md)) to record an edit.

### Relationships

- `test` → one [`Test`](#test) (backref `Test.expected_results`).
- Pointed at by [`TestVerdict.expectation_id`](#testverdict) — every
  recorded verdict pins the specific `ExpectedTestResult` row that was
  in force at recording time, so historical verdicts stay stable when
  the expectation is later edited.

---

## TestMatrixRun

`test_matrix_runs` — one row per submission of a `TestMatrix`. Groups
the per-cell [`TestVerdict`](#testverdict)s spawned from a single
matrix expansion so they can be tracked, cancelled, or queried as a
unit. Owns the GitHub linkage that, before phase 1, lived on every
`TestRun` row, and the stored rollup that, before phase 2, was
recomputed in app code on every render.

### Identity / triggering

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `matrix_id` | int FK → `test_matrices.id`, not null | The matrix this submission expanded. Anonymous / ad-hoc matrices still get a row (with `name = NULL`). |
| `trigger` | string, default `"manual"` | `manual` / `cli` / `web` / `remote` / `webhook` / `tag` / `schedule` / `rerun`. `"tag"` is set by the webhook handler for tag-push events; the project page's "Latest release run" card filters on it. |
| `ref` | string, nullable | Git ref / tag the run was triggered against (set for `trigger="tag"`; left empty for branch/PR pushes) |
| `github_owner` | string, nullable | GitHub repository owner |
| `github_repo` | string, nullable | GitHub repository name |
| `github_commit_sha` | string, nullable | Head SHA of the triggering event |
| `github_pr_number` | int, nullable | PR number, when the trigger was a `pull_request` event |
| `github_status_url` | string, nullable | The `statuses_url` to post commit-status updates to |
| `created_at` | datetime, default now | |
| `completed_at` | datetime, nullable | Set to the finished-at of the last child cell when every cell has reached `finished` / `cancelled` / `timed_out` |

### Rollup counters

Updated transactionally as each child [`TestVerdict`](#testverdict)
finalizes (via `persistence.recompute_matrix_run_rollup`). The UI and
REST surface read them as constant-time O(1) columns; the legacy
app-side rollup is gone.

| Column | Type | Notes |
|---|---|---|
| `pass_count` | int, not null, default `0` | Number of cells whose `TestRun.result_code == PASS` |
| `fail_count` | int, not null, default `0` | Number of cells whose `TestRun.result_code == FAIL` |
| `error_count` | int, not null, default `0` | Number of cells whose `TestRun.result_code == ERROR` |
| `expected_count` | int, not null, default `0` | Cells whose verdict is `EXPECTED` |
| `unexpected_count` | int, not null, default `0` | Cells whose verdict is `UNEXPECTED` |
| `unknown_count` | int, not null, default `0` | Cells whose verdict is `UNKNOWN` (no expectation existed at recording time) |
| `cache_hit_count` | int, not null, default `0` | Cells reusing a pre-existing `TestRun` via the content-addressable cache |
| `total_count` | int, not null, default `0` | Total cells in the matrix run |
| `actual_summary` | enum [`TestResultCode`](#testresultcode), nullable | Worst actual outcome across cells (PASS < FAIL < ERROR) |
| `verdict` | enum [`TestVerdictKind`](#testverdictkind), nullable | Rolled-up verdict; see rules below. NULL while no cell has finalized. |

### Verdict rollup rules

Evaluated in order on every counter update:

1. `UNEXPECTED` — at least one cell's verdict is UNEXPECTED.
2. `UNKNOWN` — no UNEXPECTED cells, but at least one cell finalized
   with no expectation in force at recording time.
3. `EXPECTED` — every cell had an expectation in force at recording
   time *and* the actual matched it. This is the release-ready state.

Release-readiness for tag-triggered matrix runs is then a one-liner:
`TestMatrixRun.verdict == EXPECTED` on the row triggered by the
release tag.

### Relationships

- `matrix` → one [`TestMatrix`](#testmatrix) (backref `TestMatrix.matrix_runs`).
- `test_runs` → many [`TestRun`](#testrun) (children of this submission;
  cache hits do not create a new TestRun, so the count here can be
  smaller than `total_count`).
- `verdicts` → many [`TestVerdict`](#testverdict), `cascade="all, delete-orphan"` (one per cell).

---

## TestRun

`test_runs` — one row per attempt to run a `Test`. Carries the
per-attempt context (commit, version, deps, worker, timing), the
lifecycle state, and — once `lifecycle == finished` — the outcome.

### Identity / parent FKs

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `test_id` | int FK → `tests.id`, not null | The coordinate being run. All coordinate fields (`project`, `kind`, `os`, …) are read off the joined `Test` row. |
| `matrix_run_id` | int FK → `test_matrix_runs.id`, nullable | Null for ad-hoc single-test submissions (CLI `opp_ci run`, `POST /api/runs`); set for every child of a matrix submission. |
| `worker_id` | int FK → `workers.id`, nullable | Set when a worker claims the run via `/api/workers/poll`. |

### Per-attempt context

| Column | Type | Notes |
|---|---|---|
| `commit_sha` | string, nullable | Resolved head SHA — set by the worker once `git_ref` resolves to a concrete commit |
| `git_ref` | string, nullable | Branch / tag the run targets |
| `version` | string, nullable | Version label (matrix-set) |
| `resolved_deps` | JSON, nullable | Pinned dep map for this run |

### Lifecycle

| Column | Type | Notes |
|---|---|---|
| `lifecycle` | enum [`TestRunLifecycle`](#testrunlifecycle), not null, default `queued` | The state machine. Set to `running` when a worker claims the row, `finished` on worker result, `cancelled` by a user action, `timed_out` by the watchdog. |
| `created_at` | datetime, default now | Insert time |
| `started_at` | datetime, nullable | Set at claim time by `/api/workers/poll` (not at insert) |
| `finished_at` | datetime, nullable | Set on worker result |
| `duration_seconds` | float, nullable | Reported by the worker |

### Outcome (populated iff `lifecycle == finished`)

| Column | Type | Notes |
|---|---|---|
| `result_code` | enum [`TestResultCode`](#testresultcode), nullable | `PASS` / `FAIL` / `ERROR` / `SKIPPED` |
| `stdout` | text, nullable | Raw, ANSI codes preserved |
| `stderr` | text, nullable | Raw, ANSI codes preserved |
| `details` | JSON, nullable | Free-form per-test breakdown from opp_repl (`to_dict()`); populated only on the direct-import executor path |

### Best-effort system context

| Column | Type | Notes |
|---|---|---|
| `system_snapshot` | JSON, nullable | Captured at claim time; posted by the worker via `POST /api/workers/snapshot`. On Postgres, TOAST keeps the blob out-of-line and lazy-loaded so it costs nothing on queries that don't touch it. No retention policy in phase 1. |

### Cache fingerprint

| Column | Type | Notes |
|---|---|---|
| `cache_fingerprint` | string, nullable, indexed | SHA-256 hex of every input that can change the outcome — coordinates, `git_ref` (best-effort resolved to a SHA via the GitHub client when a token is configured), `version`, and `resolved_deps`. Computed by `opp_ci.fingerprint.compute_cache_fingerprint(job)` at submit time. A subsequent enqueue with the same fingerprint reuses this row instead of creating a new `TestRun`, and the new `TestVerdict` cell records `cache_hit=True`. Expectations are deliberately *not* part of the key — cache hits grade against the currently-in-force expectation. |

### View-side accessors

`TestRun` exposes Python properties that delegate to the joined `Test`
row so existing templates and rollup code can read `run.project`,
`run.kind`, `run.os`, `run.compiler`, etc. directly without writing the
join out by hand. There is also:

- `run.matrix_id` — proxies `matrix_run.matrix_id` (None for ad-hoc).
- `run.github_owner` / `run.github_repo` / `run.github_commit_sha` /
  `run.github_pr_number` / `run.trigger` — all delegate to
  `matrix_run`, so they are None for ad-hoc runs.
- `run.effective_status` — combines `lifecycle` and `result_code`
  into the single label used by templates / rollup: returns the
  `result_code` value (`"PASS"` / `"FAIL"` / `"ERROR"` / `"SKIPPED"`)
  when `lifecycle == finished`, otherwise the `lifecycle` value
  (`"queued"` / `"running"` / `"cancelled"` / `"timed_out"`).

Note: `run.test` is the SQLAlchemy relationship returning the `Test`
row, **not** the test kind — use `run.kind` (or `run.test.kind`) for
the kind string.

### Relationships

- `test` → one [`Test`](#test) (backref `Test.runs`, `cascade="all, delete-orphan"`).
- `matrix_run` → one [`TestMatrixRun`](#testmatrixrun) (backref `TestMatrixRun.test_runs`).
- `worker` → one [`Worker`](#worker) (backref `Worker.test_runs`).
- `verdicts` → many [`TestVerdict`](#testverdict) — a single TestRun
  can back many cells when cache hits attribute later matrix runs to
  the same observation.

---

## TestVerdict

`test_verdicts` — one row per cell of a `TestMatrixRun`. Pins the
`TestRun` whose outcome this cell attributes (a freshly-queued row on
a cache miss, a pre-existing finished row on a hit) plus the
`ExpectedTestResult` row in force at recording time. The cell has at
most one promotion event (verdict written) and is then frozen; its
lifecycle is derived from the underlying `TestRun.lifecycle` rather
than stored, so there is only ever one source of truth for "is it
done yet".

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `matrix_run_id` | int FK → `test_matrix_runs.id`, not null, indexed | Parent matrix run |
| `test_id` | int FK → `tests.id`, not null, indexed | The Test coordinate this cell represents |
| `test_run_id` | int FK → `test_runs.id`, not null, indexed | The TestRun whose outcome this cell attributes |
| `expectation_id` | int FK → `expected_test_results.id`, nullable | The expectation row in force at recording time. NULL ⇔ no expectation existed when the verdict landed |
| `verdict` | enum [`TestVerdictKind`](#testverdictkind), nullable | `EXPECTED` / `UNEXPECTED` / `UNKNOWN`. NULL until the underlying `TestRun` finalizes (stays NULL forever if the run is cancelled). |
| `recorded_at` | datetime, nullable | When the verdict was written: `TestRun.finished_at` for miss-then-execute, insertion time for cache hits |
| `created_at` | datetime, not null | When the cell was inserted (= matrix submit time) |
| `cache_hit` | bool, not null, default `false` | True iff this cell reused a pre-existing TestRun via the content-addressable cache |

### Verdict computation

Computed by `persistence.compute_verdict_kind(actual_code,
expectation)`:

- `None` — actual not yet known (TestRun still queued/running).
- `UNKNOWN` — actual known, no expectation existed (or the most recent
  ExpectedTestResult row was a retraction).
- `EXPECTED` — actual matches `expectation.expected_result_code`.
- `UNEXPECTED` — actual diverges from `expectation.expected_result_code`
  (covers unexpected ERROR as well).

### Snapshot semantics

Because each verdict pins a specific `ExpectedTestResult` row via
`expectation_id`, later edits to the expectation log do *not*
retroactively change historical verdicts or the parent matrix run's
counters. A `TestMatrixRun` is a snapshot of "what we knew when this
ran"; the [matrix-run detail page](web_ui.md#matrix-run-detail) and
CLI ([`opp_ci show-matrix-run`](cli_reference.md)) surface this
explicitly.

### Relationships

- `matrix_run` → one [`TestMatrixRun`](#testmatrixrun) (backref `TestMatrixRun.verdicts`).
- `test` → one [`Test`](#test).
- `test_run` → one [`TestRun`](#testrun) (backref `TestRun.verdicts`).
- `expectation` → one [`ExpectedTestResult`](#expectedtestresult) (nullable).

---

## TestVerdictKind

Python `enum.Enum`, stored as the SQL `Enum` type on
`test_verdicts.verdict` and `test_matrix_runs.verdict`.

| Value | Meaning |
|---|---|
| `EXPECTED` | Actual outcome matched the expectation in force at recording time. Release-ready. |
| `UNEXPECTED` | Actual diverged from the expectation (wrong outcome, or unexpected ERROR). Regression candidate. |
| `UNKNOWN` | No mismatch, but no expectation existed when the verdict landed — declare an expectation to characterise the cell. |

---

## TestRunLifecycle

Python `enum.Enum`, stored as the SQL `Enum` type on
`test_runs.lifecycle`. Always set.

| Value | Meaning |
|---|---|
| `queued` | Inserted by scheduler / CLI; awaiting a worker |
| `running` | Claimed by a worker |
| `finished` | Worker reported a result — `result_code` is now populated |
| `cancelled` | A user action cancelled the run. Only valid transition from `queued`; running runs are left to finish (the worker can't be interrupted). |
| `timed_out` | The coordinator's watchdog reclaimed the run after the worker stopped heartbeating |

---

## TestResultCode

Python `enum.Enum`, stored as the SQL `Enum` type on
`test_runs.result_code`, `expected_test_results.expected_result_code`,
and `test_matrix_runs.actual_summary`.

| Value | Meaning |
|---|---|
| `PASS` | All sub-tests passed |
| `FAIL` | At least one sub-test failed |
| `ERROR` | Infrastructure failure (build, env, worker crash) |
| `SKIPPED` | The run was deliberately not executed (reserved) |

---

## Worker

`workers` — persistent registration record for a worker agent.
Separate from the running worker *process*; the row is what the
coordinator uses to dispatch jobs.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `name` | string, unique, not null | Worker identifier |
| `token` | string, unique, not null | Auto-generated bearer token (`secrets.token_urlsafe(32)`) |
| `tags` | JSON list | Capability tags (`["linux", "amd64", "podman", "nix"]`) |
| `concurrency` | int, default `1` | Max simultaneous jobs |
| `status` | string, default `"offline"` | `online` / `offline` / `busy` |
| `last_heartbeat` | datetime, nullable | Updated by `/api/workers/heartbeat` |
| `registered_at` | datetime, default now | |
| `current_job_count` | int, default `0` | Live counter; not authoritative on restart |

Helper: `Worker.is_available` returns true when `status == "online"`
and `current_job_count < concurrency`. Tag semantics and dispatch
rules: see [workers.md](workers.md#capability-tags).

---

## AutoTestRule

`auto_test_rules` — "when this kind of GitHub event matching this
pattern hits this project, run this matrix."

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `project_id` | int FK → `projects.id`, not null | |
| `rule_type` | string, not null | `branch`, `pr`, or `tag` |
| `pattern` | string, not null | fnmatch glob (`master`, `topic/*`, `*`) |
| `matrix_id` | int FK → `test_matrices.id`, nullable | Null = smoke-only baseline |
| `enabled` | int, default `1` | `0`/`1` flag |

Evaluated by the webhook receiver — see
[github_integration.md](github_integration.md).

---

## ApiToken

`api_tokens` — bearer tokens for REST API authentication. Created via
`opp_ci token create`; checked by [opp_ci/auth.py](../opp_ci/auth.py).

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `token` | string, unique, not null | Auto-generated (`secrets.token_urlsafe(32)`) |
| `name` | string, not null | Human label |
| `role` | string, not null, default `"readonly"` | `readonly` / `submitter` / `worker` / `admin` |
| `enabled` | bool, default `true` | |
| `created_at` | datetime, default now | |

Role semantics: see
[concepts.md → Role](concepts.md#role) and
[rest_api.md → Authentication](rest_api.md#authentication). Worker
tokens (stored on the `workers` table) are checked through the same
auth path but are separate rows.

---

## User

`users` — human users of the web UI. Either `github_user_id` is set
(GitHub OAuth login) or `username` is set (local password login), or
both (a local account that has linked a GitHub identity).

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `github_user_id` | int, unique, nullable | Numeric GitHub user ID |
| `github_username` | string, nullable | GitHub `@handle` (for display) |
| `username` | string, unique, nullable | Local-login username |
| `password_hash` | string, nullable | For local-login users only |
| `role` | string, not null, default `"readonly"` | Same scale as [ApiToken.role](#apitoken) |
| `role_locked` | bool, not null, default `false` | If true, OAuth login won't recompute role from GitHub team membership |
| `enabled` | bool, not null, default `true` | |
| `created_at` | datetime, default now | |
| `last_login_at` | datetime, nullable | |
| `last_role_sync_at` | datetime, nullable | Last time the role was recomputed from GitHub |

Helper: `User.display_name` prefers `@github_username`, falls back to
`username`, finally `user#<id>`. Login flow: see
[web-login.md](web-login.md).

---

## Cross-cutting design notes

### Denormalised columns

`Test` records the project, OS, compiler, and platform coordinates by
**string value**, not by foreign key. This is deliberate:

- A historical Test row stays meaningful after the catalog is edited,
  OS rows are renamed, or projects are removed.
- Reporting and aggregation queries (see
  [rollup](concepts.md#rollup)) can group by string equality without
  joins back to catalog tables.

The cost is that consistency between catalog tables and `Test` columns
is by convention, not by referential integrity. Treat the catalog
tables (`projects`, `versions`, `os_entries`, `compilers`) as the
*current* truth and the `Test` string columns as the *historical*
truth.

### Cascade behaviour

Two cascades are declared:

- Deleting a `Test` cascades to its `TestRun`s
  (`cascade="all, delete-orphan"`).
- A `TestMatrixRun` does **not** cascade to its `TestRun` children at
  the ORM level; orphan handling on matrix deletion is a manual
  operation today.

Everything else uses SQL-level FK behaviour (default `NO ACTION`), so
dropping a referenced worker / matrix / project requires manual
cleanup.

### JSON columns

| Table.column | Shape |
|---|---|
| `projects.dependency_names` | list of strings |
| `versions.resolved_dependencies` | object: dep-name → version |
| `test_matrices.config` | object describing axes (see [test_matrix_dimensions.md](test_matrix_dimensions.md)) |
| `workers.tags` | list of strings (capability tags) |
| `test_runs.resolved_deps` | object: dep-name → version |
| `test_runs.details` | opp_repl per-test breakdown (`to_dict()`) |
| `test_runs.system_snapshot` | object: best-effort host facts captured at run start (hostname, OS, Python version, …) |

On SQLite the column type degrades to TEXT with JSON encoding; on
PostgreSQL it is native `JSON` / `JSONB`. Use SQLAlchemy's JSON access
(not raw SQL) when querying, so both backends behave the same.

### Timestamps

All timestamps are naive `datetime` in UTC, populated by
`datetime.datetime.utcnow`. There is no timezone column. Treat every
DB timestamp as UTC and convert at render time.

### Token generation

`Worker.token` and `ApiToken.token` are auto-generated by
`secrets.token_urlsafe(32)` at row insert. Tokens are stored
*verbatim* (not hashed) because the system is single-tenant and the
DB itself is the trust boundary — losing the DB already means losing
everything.

---

## Persistence helpers

Common write paths against the new model live in
[opp_ci/persistence.py](../opp_ci/persistence.py), so every call site
(web routes, REST API, CLI, github webhook) stays consistent:

| Helper | Purpose |
|---|---|
| `job_to_coord(job, *, project, opp_file)` | Project an `expand_matrix` job dict (or form-field dict) down to the `TEST_COORD_FIELDS` keys |
| `get_or_create_test(session, coord)` | Look up the matching `Test` by `coord_hash`, creating it on first sight |
| `create_matrix_run(session, *, matrix_id, trigger, ref, github_*)` | Create one `TestMatrixRun` for a matrix submission |
| `create_test_run(session, *, test_id, matrix_run_id, …, cache_fingerprint)` | Create a queued `TestRun` (fingerprint optional; see [opp_ci/fingerprint.py](../opp_ci/fingerprint.py)) |
| `get_current_expectation(session, test_id)` | Most recent [`ExpectedTestResult`](#expectedtestresult) row for `test_id`, or `None` |
| `insert_expectation(session, *, test_id, expected_result_code, …)` | Append a new expectation row. `expected_result_code=None` records an explicit retraction. |
| `compute_verdict_kind(actual_code, expectation)` | Three-state verdict for one cell (returns a [`TestVerdictKind`](#testverdictkind) or None when actual is not yet known) |
| `create_test_verdict(session, *, matrix_run_id, test_id, test_run_id, expectation, verdict, recorded_at, cache_hit)` | Insert a `TestVerdict` row |
| `finalize_verdict_for_run(session, run_id)` | Promote pending TestVerdict rows attached to `run_id` and refresh the parent rollup. Called from the worker result handler and the local executor right after the TestRun outcome is known. |
| `recompute_matrix_run_rollup(session, matrix_run_id)` | Recompute the counter / verdict / `actual_summary` / `completed_at` columns from the child verdicts. |
| `enqueue_job(session, job, *, project, opp_file, matrix_run_id, use_cache, cache_fingerprint)` | End-to-end: get-or-create-`Test`, look up the cache, then either reuse the existing `TestRun` (cache hit) or create a fresh queued one. **Returns** `(TestRun, TestVerdict \| None)`. The verdict is `None` only when `matrix_run_id` was not provided. |
| `capture_system_snapshot()` | Best-effort dict of host facts to write into `TestRun.system_snapshot` |

---

## Schema changes after phase 1

The phase-1 cutover was applied by wiping and recreating the database,
not by running a migration. The phase-2 redesign (this revision)
*does* ship as an Alembic migration (`d4a8c91e7350`), and it backfills
counters on existing matrix runs and migrates any legacy
`tests.expected_result_*` values into one initial `ExpectedTestResult`
row each.

Subsequent schema changes are expected to land via Alembic migrations
under [opp_ci/db/migrations/](../opp_ci/db/migrations/) (config in
`alembic.ini`), but the current `models.py` does not depend on any
historical migration revision — it is the source of truth.
