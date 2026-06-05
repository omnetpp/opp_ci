# Single Test Parameters

A *single test* in opp_ci is one [TestRun](concepts.md#testrun) row —
one queued/running/finished job, the smallest unit of work the system
schedules. The coordinate the run targets (`project`, `kind`, `mode`,
platform stack, …) actually lives on the joined
[Test](concepts.md#test) row, deduped by `coord_hash`; the per-attempt
fields (`git_ref`, `commit_sha`, `version`, `resolved_deps`,
lifecycle, outcome) live on the `TestRun`. Whether the row was
created by `opp_ci run`, the REST API, matrix expansion, or a
webhook, the parameter set is the same. This guide documents every
field: what it is, how it is set, what controls it, what values are
legal, and how it interacts with the other fields.

For the matrix-level (cross-product) view of the same dimensions, see
[test_matrix_dimensions.md](test_matrix_dimensions.md). For the
data-model overview see [concepts.md](concepts.md#domain-model-database)
and [architecture.md](architecture.md#database-schema).

---

## Parameters at a glance

The "Stored on" column shows where the value lands: the deduped
[Test](concepts.md#test) row (the coordinate) vs. the
[TestRun](concepts.md#testrun) row (the per-attempt context).

| Parameter | CLI flag | Stored on | Column | Default | Group |
|---|---|---|---|---|---|
| [`project`](#project) | `--project` *(required)* | `Test` | `project` | — | Required |
| [`kind`](#kind) | `--kind` *(required)* | `Test` | `kind` | — | Required |
| [`mode`](#mode) | `--mode` | `Test` | `mode` | `None` | Identity |
| [`git_ref`](#git_ref) | `--ref` | `TestRun` | `git_ref` | `None` | Identity |
| [`version`](#version) | *(matrix-only)* | `TestRun` | `version` | `None` | Identity |
| [`os`](#os-distro-and-flavor) | `--os` | `Test` | `os` | `None` | Platform |
| [`os_version`](#os-distro-and-flavor) | `--os-version` | `Test` | `os_version` | `None` | Platform |
| [`distro`](#os-distro-and-flavor) | `--distro` | `Test` | `distro` | `None` | Platform |
| [`distro_version`](#os-distro-and-flavor) | `--distro-version` | `Test` | `distro_version` | `None` | Platform |
| [`flavor`](#os-distro-and-flavor) | `--flavor` | `Test` | `flavor` | `None` | Platform |
| [`flavor_version`](#os-distro-and-flavor) | `--flavor-version` | `Test` | `flavor_version` | `None` | Platform |
| [`arch`](#arch) | `--arch` | `Test` | `arch` | `None` | Platform |
| [`compiler`](#compiler-and-compiler_version) | `--compiler` | `Test` | `compiler` | `None` | Platform |
| [`compiler_version`](#compiler-and-compiler_version) | `--compiler-version` | `Test` | `compiler_version` | `None` | Platform |
| [`isolation`](#isolation) | `--isolation` | `Test` | `isolation` | `none` | Execution |
| [`toolchain`](#toolchain) | `--toolchain` | `Test` | `toolchain` | `none` | Execution |
| [`pin`](#pin-cli--resolved_deps-column) | `--pin` *(repeatable)* | `TestRun` | `resolved_deps` | `None` | Dependencies |
| [`opp_file`](#opp_file) | *(matrix-only)* | `Test` | `opp_file` | `None` | Descriptor |
| [`skip_install`](#skip_install) | `--skip-install` | — | *(not stored)* | `false` | Behavior |

Templates and the rollup read every coordinate field straight off the
`TestRun` (`run.project`, `run.kind`, `run.os`, …) via view-side
proxy properties that delegate to the joined Test, so day-to-day
queries don't have to spell the join out.

Server-set fields not listed above — `lifecycle`, `result_code`,
`stdout`, `stderr`, `details`, `system_snapshot`, `worker_id`,
`started_at`, `finished_at`, `duration_seconds`, `commit_sha`
(on `TestRun`), `trigger`, the `github_*` columns (on the parent
`TestMatrixRun`) — are covered under
[Lifecycle fields](#lifecycle-fields-system-set) and
[GitHub linkage](#github-linkage-webhook-only).

---

## Three ways to create a single test

The same parameter set is accepted by three surfaces; pick whichever
fits your workflow:

| Surface | Entry point | Trigger value |
|---|---|---|
| CLI (local) | `opp_ci run --project … --kind …` | `manual` |
| CLI (remote) | `opp_ci --remote run --project … --kind …` | `remote` |
| REST | `POST /api/runs` (or [OppCiClient.submit_run()](../opp_ci/client.py)) | `remote` |

Matrix expansion produces single tests too — one per cell of the
cross-product — but those are described in
[test_matrix_dimensions.md](test_matrix_dimensions.md). Anything in
this guide that is *settable per run* is settable per matrix cell.

---

## Required parameters

### `project`

The opp_env project name — `inet-4.5`, `omnetpp-6.1`, `simu5g-1.3`,
`fifo`, etc. This is the install target for `opp_env install` and the
discriminator the executor uses to load the right opp_repl simulation
project.

| Aspect | Value |
|---|---|
| CLI flag | `--project` (required) |
| REST field | `project` (required) |
| Test column | `project` |
| Default | none |

The project must exist in the database. For projects not in the
opp_env catalog, register one with `opp_ci add-project` (see
[cli_reference.md](cli_reference.md#projects-and-versions)).

### `kind`

What kind of test to run. The executor uses this to dispatch into
opp_repl via [COMMAND_MAP](../opp_ci/executor.py). Renamed from the
legacy `test` field in the phase-1 schema cutover; matrix YAML axis
`kinds:`, CLI flag `--kind` / `--kinds`, REST / SDK field `kind`,
column `Test.kind`.

| Aspect | Value |
|---|---|
| CLI flag | `--kind` (required, comma-separated for multiple) |
| REST field | `kind` (required) |
| Test column | `kind` |
| Default | none |

Recognized values: `smoke`, `build`, `fingerprint`, `statistical`,
`feature`, `chart`, `speed`, `sanitizer`, `release`, `opp`, `all`.
See [Axis: kind](test_matrix_dimensions.md#axis-kind) for what each
entry point does.

Note: the CLI accepts a comma-separated list (`--kind smoke,fingerprint`)
and creates one TestRun per value, sharing the install step. The REST
API accepts one value per call.

---

## Identity parameters

These together define the `Test` row's identity. Matrix expansion and
the REST submitter look the row up by SHA-256 `coord_hash` over the
closed field set
`(project, kind, mode, os, os_version, distro, distro_version,
flavor, flavor_version, arch, compiler, compiler_version,
isolation, toolchain, opp_file)`, creating a new `Test` row only on
first sight. `git_ref` / `version` / `resolved_deps` are *not* part of
the hash — they live on the `TestRun` as per-attempt context, so two
runs of the same coordinate against different refs share one `Test`.
There is no submission-time dedup against existing TestRuns in phase 1:
every submission inserts a new TestRun (the legacy `--force`/
`find_existing_run()` behaviour was removed).

### `mode`

Build mode. Two canonical values:

| Aspect | Value |
|---|---|
| CLI flag | `--mode {debug\|release}` |
| REST field | `mode` |
| Test column | `mode` |
| Default | `None` (executor picks; usually `release`) |

Passed through to opp_repl as `build_mode`. `debug` enables debug
symbols and assertions; `release` is the default for fingerprint /
statistical runs.

### `git_ref`

The git branch, tag, or SHA to test. Recorded on the TestRun as
`git_ref`; the worker resolves it to a concrete commit and writes
that into `commit_sha`.

| Aspect | Value |
|---|---|
| CLI flag | `--ref` |
| REST field | `git_ref` |
| TestRun column | `git_ref` |
| Default | `None` — test the project's released sources |

When `git_ref` is given, the executor clones (or fetches into) the
project's GitHub repo in the worker cache and creates a worktree at
that ref. See
[_ensure_github_clone()](../opp_ci/executor.py) and
[_resolve_project_dir()](../opp_ci/executor.py).

Under `toolchain=nix`, a non-trivial `git_ref` triggers the `-git`
variant switch in
[resolve_git_project()](../opp_ci/executor.py): the project name
is replaced with its `-git` form (e.g. `inet-git`), and the commit is
pinned via the `OPP_ENV_GIT_REF` environment variable. Under other
toolchains, the ref is checked out conventionally.

### `version`

The opp_env package version label (e.g. `inet-4.5`, `omnetpp-6.1`).
This field is *not* settable through the single-test CLI or REST
surfaces today — it is populated only when the TestRun was produced
by matrix expansion (the matrix's `versions` axis becomes this
column).

| Aspect | Value |
|---|---|
| CLI flag | *(not exposed)* |
| REST field | *(not exposed on `/api/runs`)* |
| TestRun column | `version` |
| Default | `None` for ad-hoc runs |

For ad-hoc single-test runs, supply the version implicitly via the
`project` name (e.g. `inet-4.5` *is* the version-qualified project
identifier).

---

## Platform parameters

The platform parameters determine where the run executes and which
toolchain produces the binary.

### `os`, `distro`, and `flavor`

The target platform forms a three-level hierarchy:

* **`os`** — one of `Linux`, `Windows`, `MacOS`. The kernel family.
* **`distro`** — Linux distribution (`ubuntu`, `fedora`, ...). Only
  meaningful when `os == "Linux"`.
* **`flavor`** — distribution variant (`kubuntu`, `xubuntu`, ...).
  Carries the same package base as its parent distro.

Each level has an optional `<level>_version` partner. The version
always attaches to the *most specific* named level:

| `os` | `os_version` | `distro` | `distro_version` | `flavor` | `flavor_version` |
|---|---|---|---|---|---|
| `Linux` | **NULL** | `ubuntu` | `24.04` | — | — |
| `Linux` | **NULL** | `ubuntu` | `24.04` | `kubuntu` | inherits from distro |
| `Windows` | `11` | — | — | — | — |
| `MacOS` | `15.1` | — | — | — | — |

CLI:

| Aspect | Value |
|---|---|
| CLI flags | `--os`, `--os-version`, `--distro`, `--distro-version`, `--flavor`, `--flavor-version` |
| REST fields | `os`, `os_version`, `distro`, `distro_version`, `flavor`, `flavor_version` |
| Test columns | (same names) |
| Default | `None` at every level |

Combined shorthand on `--distro` / `--flavor` works:
`--distro 'Ubuntu 24.04'` is parsed as `--distro Ubuntu
--distro-version 24.04`. `--os` is restricted to the three OS family
names; passing a distro name there is a hard error.

Implied parents fill in automatically — `--flavor Kubuntu` resolves
`distro=ubuntu, os=Linux` via the [registry](../opp_ci/platforms.py).
Explicit contradictions (e.g. `--os Windows --distro Ubuntu`) raise an
error at submit time.

Under `isolation=podman`, the executor picks the runner image by
exact match on `(<platform-slug>, compiler, compiler_version)` —
where `platform-slug` is the most-specific named level
(`kubuntu-24.04`, `ubuntu-24.04`, `windows-11`, `macos-15`).
Under `isolation=none`, the run constrains dispatch to workers
tagged with the most-specific level: `flavor:<flavor>-<ver>`,
`distro:<distro>-<ver>`, or `os:<os>[-<ver>]`.

### `arch`

CPU architecture. Free-form string; common values are `amd64` and
`aarch64`.

| Aspect | Value |
|---|---|
| CLI flag | `--arch` |
| REST field | `arch` |
| Test column | `arch` |
| Default | `None` (no constraint) |

When omitted, the run is unconstrained on architecture — any worker
matching the other tags can pick it up. When set, only workers
tagged `arch:<value>` are eligible (under `isolation=none`); under
`isolation=podman` the value selects an arch-specific image variant.

### `compiler` and `compiler_version`

The C++ compiler.

| Aspect | Value |
|---|---|
| CLI flags | `--compiler`, `--compiler-version` |
| REST fields | `compiler`, `compiler_version` |
| Test columns | `compiler`, `compiler_version` |
| Default | `None` |

Under `isolation=podman`, both are required and form part of the
image-tag key. Under `isolation=none, toolchain=none`, they constrain
worker dispatch via the `compiler:<name>-<version>` tag.

Under `toolchain=nix`, opp_env only exposes a small allow-list:

| Compiler | Version |
|---|---|
| `gcc` | `7` |
| `clang` | unspecified |

See [_NIX_SUPPORTED_COMPILERS](../opp_ci/scheduler.py). The
validator runs at matrix-expansion time; the single-test path
delegates to opp_env, so an unsupported pair under `toolchain=nix`
will fail at install/run time rather than at submit time.

### `platform_desc` (derived)

A human-readable summary, e.g. `Ubuntu 24.04 / amd64 / clang-22`,
built by [platforms.build_platform_desc()](../opp_ci/platforms.py)
from the platform fields. Not stored as a column on the new `Test` /
`TestRun` schema — recomputed at render time so renaming a row's
platform metadata can't drift the cached label.

| Aspect | Value |
|---|---|
| CLI flag | *(none — derived)* |
| REST field | *(none — server-computed)* |
| Stored on | *(not stored — computed from `Test` columns at render time)* |

---

## Execution environment

### `isolation`

How the job's filesystem and OS are isolated from the host.

| Aspect | Value |
|---|---|
| CLI flag | `--isolation {none\|podman}` |
| REST field | `isolation` |
| Test column | `isolation` |
| Default | `none` (CLI); `None` → treated as `none` (REST) |

- `none` — direct subprocess on the worker, host packages.
- `podman` — run inside a Podman runner image keyed by
  `(os, os_version, compiler, compiler_version)`. Image building is
  driven by `opp_ci image build` / `image build-matrix`.

### `toolchain`

Where the C++ toolchain comes from.

| Aspect | Value |
|---|---|
| CLI flag | `--toolchain {none\|nix}` |
| REST field | `toolchain` |
| Test column | `toolchain` |
| Default | `none` (CLI); `None` → `none` (REST) |

- `none` — whatever compiler is installed (on the host or inside the
  container).
- `nix` — `opp_env install` then `opp_env run`. Reproducible Nix
  environment.

Isolation × toolchain gives four valid combinations; see the table in
[getting_started.md](getting_started.md#selecting-an-execution-environment).
Both axes are independent of each other and of the platform fields.

---

## Dependency parameters

### `pin` (CLI) / `resolved_deps` (column)

Force specific dependency versions for the run. The CLI accepts
repeatable `--pin dep=ver` flags; the executor resolves these via
[dependency.resolve_dependencies()](../opp_ci/dependency.py) and
stores the result as a JSON map on the TestRun.

| Aspect | Value |
|---|---|
| CLI flag | `--pin omnetpp=6.1` (repeatable) |
| REST field | *(not exposed on `/api/runs` directly)* |
| TestRun column | `resolved_deps` (JSON) |
| Default | `None` — fall back to the Version's stored map, or live-resolve via opp_env |

Example:

```bash
opp_ci run --project inet-4.5 --kind smoke \
           --pin omnetpp=6.1 --pin some-lib=1.2
```

The resolved map appears on the TestRun like:

```json
{ "omnetpp": "6.1", "some-lib": "1.2" }
```

For matrix-expanded runs, this column is populated from the matrix's
`deps` axis instead — see
[Axis: dependency versions](test_matrix_dimensions.md#axis-dependency-versions).

---

## Project-descriptor parameter

### `opp_file`

Path to the project's `.opp` file — the opp_repl project descriptor.
Used by the executor to locate the source tree, set
`github_owner`/`github_repository`, and resolve include/build paths.

| Aspect | Value |
|---|---|
| CLI flag | *(not exposed on `opp_ci run`)* |
| REST field | *(not exposed on `/api/runs`)* |
| Test column | `opp_file` |
| Default | `None` |

Set today only via the matrix (the matrix's `opp_file` field is
copied onto each expanded run). For ad-hoc runs, the executor
auto-discovers `*.opp` files in the current directory; see
[_load_workspace()](../opp_ci/executor.py) for the resolution
order.

---

## Behavior controls

These do not appear on the TestRun row — they only affect submission
behavior. (There is no submission-time duplicate check in phase 1:
every submission inserts a new `TestRun`, so the legacy `--force`
flag and the `find_existing_run()` helper are gone.)

### `skip_install`

Skip the `opp_env install` step before running the test.

| Aspect | Value |
|---|---|
| CLI flag | `--skip-install` |
| REST field | *(not applicable — coordinator does not install)* |

Useful for fast iteration: install once, then re-run tests against
the same install many times. Has no effect under
`toolchain=none` because no opp_env install happens to begin with.

---

## Lifecycle fields (system-set)

These columns are written by the coordinator and the worker as the
TestRun progresses. They are not user inputs but they appear on every
row, so they round out the picture of "everything attached to a
single test."

### `lifecycle`

State machine on `TestRun.lifecycle` — `queued` → `running` →
`finished`, with `cancelled` and `timed_out` as alternative terminals.
See [TestRunLifecycle](concepts.md#testrunlifecycle). Cancellation
applies only to queued runs; once the lifecycle is `running` the
worker can't be interrupted and the run finishes normally.

### outcome columns: `result_code` / `stdout` / `stderr` / `details`

Populated by the worker iff `lifecycle == finished`. `result_code`
is the [TestResultCode](concepts.md#testresultcode) enum (`PASS` /
`FAIL` / `ERROR` / `SKIPPED`); `stdout` / `stderr` are raw with ANSI
codes preserved; `details` is opp_repl's free-form per-test breakdown
(`to_dict()`), populated only on the direct-import executor path.
There is no separate `TestResult` table — these columns live directly
on the same `TestRun` row.

### `effective_status` (derived)

The view-side property templates use as a single status label:
returns the `result_code` value when the run is finished, otherwise
the `lifecycle` value. Not stored.

### `system_snapshot`

Optional JSON blob of best-effort host facts captured by the worker
at claim time and posted via `POST /api/workers/snapshot`. On
PostgreSQL the column is TOASTed out-of-line and lazy-loaded, so it
costs nothing on queries that don't touch it.

### `trigger`

Why the run exists. Set at submission time and read off the parent
`TestMatrixRun.trigger` via a proxy property (so single-Test runs
have `trigger == None`):

| Value | Source |
|---|---|
| `manual` | Local `opp_ci run` / `run-matrix`. |
| `web` | Web UI submission. |
| `remote` | REST submission (CLI `--remote`, `OppCiClient`). |
| `webhook` | Created by [github/webhook.py](../opp_ci/github/webhook.py). |
| `schedule` | Reserved for the periodic scheduler. |
| `rerun` | Created by the rerun helpers. |

### `worker_id`

Foreign key to [Worker](concepts.md#worker-model). `NULL` until a
worker picks up the queued job; set when the worker claims the run
via `/api/workers/poll`.

### `matrix_run_id`

Foreign key to the [TestMatrixRun](concepts.md#testmatrixrun) that
expanded this run. `NULL` for ad-hoc single-test submissions. The
parent `TestMatrixRun` carries the matrix FK (`run.matrix_id` is a
proxy property delegating through it).

### `started_at` / `finished_at` / `duration_seconds`

UTC timestamps and wall-clock duration. `started_at` is set at claim
time by `/api/workers/poll`; `finished_at` and `duration_seconds` are
set on `/api/workers/result`.

### `commit_sha`

The concrete SHA the worker resolved `git_ref` to. Set when the
worker reports results, regardless of pass/fail. Used by the
[git-notes flow](git_notes.md) and by GitHub commit-status posting.

---

## GitHub linkage (webhook-only)

These columns are populated only when the parent matrix submission
came from the [webhook receiver](concepts.md#webhook-receiver). They
live on the parent `TestMatrixRun`, not on the `TestRun` itself — for
manual / remote runs (and for any TestRun whose `matrix_run_id` is
NULL) they read back as `None`.

| Column (on `TestMatrixRun`) | TestRun proxy property | Meaning |
|---|---|---|
| `github_owner` | `run.github_owner` | GitHub repository owner. |
| `github_repo` | `run.github_repo` | GitHub repository name. |
| `github_commit_sha` | `run.github_commit_sha` | Head SHA of the triggering event. |
| `github_pr_number` | `run.github_pr_number` | PR number, when the trigger was a `pull_request` event. |
| `github_status_url` | *(no proxy — read off matrix_run)* | The `statuses_url` to post commit-status updates to. |

The status updater uses these to post commit statuses, PR comments,
and git notes after the run (or the whole matrix run) finishes. They
are the only path by which a single TestRun knows it came from a
GitHub event.

---

## Required vs. optional summary

| Field | Required for `opp_ci run` | Required for `POST /api/runs` | Required under `isolation=podman` |
|---|---|---|---|
| `project` | yes | yes | yes |
| `kind` | yes | yes | yes |
| `mode` | no | no | no |
| `git_ref` | no | no | no |
| `os` | no | no | **yes** |
| `os_version` | no | no | **yes** |
| `arch` | no | no | no |
| `compiler` | no | no | **yes** |
| `compiler_version` | no | no | **yes** |
| `isolation` | no (defaults `none`) | no | yes (to select `podman`) |
| `toolchain` | no (defaults `none`) | no | no |
| `pin` | no | n/a | no |
| `skip_install` | no | n/a | no |

---

## End-to-end example

A single test exercising every settable parameter:

```bash
opp_ci run \
    --project inet-4.5 \
    --kind fingerprint \
    --mode release \
    --ref topic/my-feature \
    --os Ubuntu --os-version 26.04 \
    --arch amd64 \
    --compiler clang --compiler-version 22 \
    --isolation podman --toolchain none \
    --pin omnetpp=6.1 \
    --skip-install
```

Produces one `TestRun` row pointing at one `Test` row:

```
Test (looked up / created via coord_hash):
    project          inet-4.5
    kind             fingerprint
    mode             release
    os               Ubuntu
    os_version       26.04
    arch             amd64
    compiler         clang
    compiler_version 22
    isolation        podman
    toolchain        none

TestRun (one row per attempt):
    test_id          → above Test row
    matrix_run_id    NULL (ad-hoc submission)
    git_ref          topic/my-feature
    commit_sha       (resolved from topic/my-feature by the worker)
    resolved_deps    {"omnetpp": "6.1"}
    lifecycle        queued → running → finished
    result_code      (on finish) PASS / FAIL / ERROR / SKIPPED
    stdout, stderr, details   (on finish) populated from opp_repl
    system_snapshot  (optional) host facts captured at claim time
    worker_id        (assigned)
```

The equivalent REST submission:

```python
from opp_ci.client import OppCiClient

ci = OppCiClient(url="https://ci.omnetpp.org/api", token="…")
ci.submit_run(
    project="inet-4.5",
    kind="fingerprint",
    mode="release",
    git_ref="topic/my-feature",
    os="Ubuntu", os_version="26.04", arch="amd64",
    compiler="clang", compiler_version="22",
    isolation="podman", toolchain="none",
)
# `--pin` is currently CLI-only (resolves dependencies locally before
# submission); pass already-resolved pins via the matrix path instead.
```

See [python_client.md](python_client.md) for the full client API.
