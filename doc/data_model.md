# Data Model

This guide describes the persistent data model of opp_ci: every table,
its columns, and how the tables relate. It is the field-level reference
for the schema sketched in
[architecture.md](architecture.md#database-schema) and named in
[concepts.md](concepts.md#domain-model-database).

The authoritative source is [opp_ci/db/models.py](../opp_ci/db/models.py)
(SQLAlchemy). Schema changes are managed by Alembic — migrations live in
[opp_ci/db/migrations/](../opp_ci/db/migrations/), config in
`alembic.ini`. The database backend is selected by `OPP_CI_DATABASE_URL`
(default `sqlite:///opp_ci.db`; production uses PostgreSQL).

---

## Tables at a glance

| Table | Purpose | Key relations |
|---|---|---|
| [`projects`](#project) | Project catalog (mirrors opp_env) | parent of `versions`, `auto_test_rules` |
| [`versions`](#version) | Project version + pinned deps | child of `projects` |
| [`os_entries`](#os) | Catalog of `(name, version, arch)` triples | referenced by matrix configs |
| [`compilers`](#compiler) | Catalog of `(name, version)` pairs | referenced by matrix configs |
| [`test_matrices`](#testmatrix) | Named cross-product configuration | referenced by `auto_test_rules`, `test_runs` |
| [`auto_test_rules`](#autotestrule) | Event-pattern → matrix bindings | child of `projects` + `test_matrices` |
| [`workers`](#worker) | Worker registrations | referenced by `test_runs` |
| [`test_runs`](#testrun) | One row per job (the unit of work) | child of `test_matrices` + `workers`; parent of `test_results` |
| [`test_results`](#testresult) | Per-individual-test outcome | child of `test_runs` |
| [`api_tokens`](#apitoken) | Bearer tokens for REST access | — |
| [`users`](#user) | Web-UI human users (local + GitHub) | — |

### Relationship diagram

```
projects ───┬─< versions
            └─< auto_test_rules >── test_matrices ──┐
                                                    │
                              workers ──┐           │
                                        ▼           ▼
                                       test_runs ───┘
                                          │
                                          └──< test_results

os_entries     compilers     api_tokens     users
   (standalone catalog/auth tables)
```

`<` = "has many", read left-to-right. `os_entries` and `compilers` are
referenced by string value from `test_runs` and from matrix JSON
configs, not by foreign key — see [Denormalised columns](#denormalised-columns).

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
from TestRun — see [Denormalised columns](#denormalised-columns).

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
foreign-keyed from TestRun — see [Denormalised columns](#denormalised-columns).

---

## TestMatrix

`test_matrices` — named cross-product configuration. Expanded by the
scheduler into TestRun rows.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `name` | string, unique, not null | CLI identifier (`inet-default`, `omnetpp-full`, …) |
| `project` | string, not null | Project name (stored by name, not FK) |
| `opp_file` | string, nullable | Optional `.opp` file the matrix targets |
| `config` | JSON, not null | The axes (versions, modes, os, compiler, isolation, toolchain, tests …) |

Axis semantics and JSON shape: see
[test_matrix_dimensions.md](test_matrix_dimensions.md).

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

## TestRun

`test_runs` — one row per queued / running / finished job. The unit
of work. For an exhaustive field-by-field reference written from the
*test author's* perspective (CLI flag, REST field, defaults,
validation, lifecycle) see
[single_test_parameters.md](single_test_parameters.md); the table
below is the *DBA's* view.

### Identity and coordinate columns

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `project` | string, not null | Project name (denormalised, see below) |
| `test` | string, not null | Entry in `executor.COMMAND_MAP` (`smoke`, `fingerprint`, …) |
| `mode` | string, nullable | Build mode (`release`, `debug`) |
| `os` | string, nullable | OS name |
| `os_version` | string, nullable | OS version |
| `arch` | string, nullable | CPU architecture (`amd64`, `aarch64`) |
| `compiler` | string, nullable | Compiler name |
| `compiler_version` | string, nullable | Compiler version |
| `isolation` | string, nullable | `none` / `podman`; `None` is read as `none` |
| `toolchain` | string, nullable | `none` / `nix`; `None` is read as `none` |
| `platform_desc` | string, nullable | Pre-rendered platform string for display |
| `git_ref` | string, nullable | Branch / tag the run targets |
| `commit_sha` | string, nullable | Resolved head SHA |
| `version` | string, nullable | Version label (matrix-set) |
| `resolved_deps` | JSON, nullable | Pinned dep map for this run |
| `opp_file` | string, nullable | Same as TestMatrix.opp_file when set by matrix |
| `matrix_id` | int FK → `test_matrices.id`, nullable | Null for ad-hoc CLI runs |

### Lifecycle columns

| Column | Type | Notes |
|---|---|---|
| `worker_id` | int FK → `workers.id`, nullable | Set when claimed |
| `status` | enum [`TestRunStatus`](#testrunstatus), default `queued` | Single source of truth for state |
| `started_at` | datetime, default now | Set at insert (not at worker-start) |
| `finished_at` | datetime, nullable | |
| `duration_seconds` | float, nullable | |
| `trigger` | string, default `"manual"` | `manual` / `remote` / `webhook` / `schedule` |

### GitHub linkage (webhook-only)

| Column | Type |
|---|---|
| `github_owner` | string, nullable |
| `github_repo` | string, nullable |
| `github_commit_sha` | string, nullable |
| `github_pr_number` | int, nullable |
| `github_status_url` | string, nullable |

These are populated only for runs created by the webhook receiver and
drive the commit-status / PR-comment posting back to GitHub. See
[github_integration.md](github_integration.md).

### Relationships

- `results` → many [`TestResult`](#testresult), `cascade="all, delete-orphan"`
  (deleting a TestRun cascades to its TestResults).
- `worker` → one [`Worker`](#worker) (backref `Worker.test_runs`).

---

## TestRunStatus

Python `enum.Enum`, stored as the SQL `Enum` type on `test_runs.status`.

| Value | Meaning |
|---|---|
| `queued` | Inserted by scheduler / CLI; awaiting a worker |
| `running` | Claimed by a worker |
| `PASS` | Terminal — every TestResult passed |
| `FAIL` | Terminal — at least one TestResult failed |
| `ERROR` | Terminal — infrastructure failure (build, env, worker crash) |

`PASS` / `FAIL` / `ERROR` are uppercase by convention to mirror
opp_repl's verdict strings.

---

## TestResult

`test_results` — per-individual-test outcome attached to a TestRun. A
single fingerprint TestRun can yield dozens of TestResult rows.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `test_run_id` | int FK → `test_runs.id`, not null | Cascade-delete with parent |
| `result_code` | string, not null | `PASS` / `FAIL` / `ERROR` / opp_repl-specific code |
| `stdout` | text, nullable | Raw, ANSI codes preserved |
| `stderr` | text, nullable | Raw, ANSI codes preserved |
| `details` | JSON, nullable | Per-test breakdown from opp_repl (`to_dict()`); populated only on the direct-import executor path |

Per-test name and duration live inside `details`, not as columns.
ANSI escapes are stored raw and converted to colored HTML by a Jinja
filter at render time — see
[concepts.md → ANSI-preserving storage](concepts.md#ansi-preserving-storage).

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

TestRun records the project, OS, compiler, and (resolved) dependency
coordinates by **string value**, not by foreign key. This is
deliberate:

- A historical run record stays meaningful after the catalog is
  edited, OS rows are renamed, or projects are removed.
- Reporting and aggregation queries (see
  [rollup](concepts.md#rollup)) can group runs by string equality
  without joins.

The cost is that consistency between catalog tables and TestRun
columns is by convention, not by referential integrity. Treat the
catalog tables (`projects`, `versions`, `os_entries`, `compilers`) as
the *current* truth and TestRun string columns as the *historical*
truth.

### Cascade behaviour

Only one cascade is declared: deleting a `TestRun` deletes its
`TestResult`s (`cascade="all, delete-orphan"`). Everything else uses
SQL-level FK behaviour (default `NO ACTION`), so dropping a referenced
worker / matrix / project requires manual cleanup or migration.

### JSON columns

| Table.column | Shape |
|---|---|
| `projects.dependency_names` | list of strings |
| `versions.resolved_dependencies` | object: dep-name → version |
| `test_matrices.config` | object describing axes (see [test_matrix_dimensions.md](test_matrix_dimensions.md)) |
| `workers.tags` | list of strings (capability tags) |
| `test_runs.resolved_deps` | object: dep-name → version |
| `test_results.details` | opp_repl per-test breakdown (`to_dict()`) |

On SQLite the column type degrades to TEXT with JSON encoding; on
PostgreSQL it is native `JSON`. Use SQLAlchemy's JSON access (not raw
SQL) when querying, so both backends behave the same.

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

## Migrations

Alembic migrations in [opp_ci/db/migrations/versions/](../opp_ci/db/migrations/versions/).
At time of writing:

| Revision | Effect |
|---|---|
| `4e2a31c0a4b1_drop_project_tier` | Removed an early `Project.tier` column |
| `11b0cd9aa9a6_add_isolation_toolchain` | Added the orthogonal isolation × toolchain axes |
| `9f1c4d2a8e10_rename_docker_to_podman` | Renamed `docker` → `podman` everywhere |
| `8a3f1d2e5b04_add_arch_to_test_runs` | Added `TestRun.arch` |
| `c5d8a4f12b30_add_users_table` | Added the `users` table |

Run `alembic upgrade head` against the configured `OPP_CI_DATABASE_URL`
to apply pending migrations.
