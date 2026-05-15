# Single Test Parameters

A *single test* in opp_ci is one [TestRun](concepts.md#testrun) row —
one queued/running/finished job, the smallest unit of work the system
schedules. Whether it was created by `opp_ci run`, the REST API,
matrix expansion, or a webhook, the row carries the same set of
fields. This guide documents every field: what it is, how it is set,
what controls it, what values are legal, and how it interacts with
the other fields.

For the matrix-level (cross-product) view of the same dimensions, see
[test_matrix_dimensions.md](test_matrix_dimensions.md). For the
data-model overview see [concepts.md](concepts.md#domain-model-database)
and [architecture.md](architecture.md#database-schema).

---

## Three ways to create a single test

The same parameter set is accepted by three surfaces; pick whichever
fits your workflow:

| Surface | Entry point | Trigger value |
|---|---|---|
| CLI (local) | `opp_ci run --project … --test …` | `manual` |
| CLI (remote) | `opp_ci --remote run --project … --test …` | `remote` |
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
| TestRun column | `project` |
| Default | none |

The project must exist in the database. For projects not in the
opp_env catalog, register one with `opp_ci add-project` (see
[cli_reference.md](cli_reference.md#projects-and-versions)).

### `test_type`

What kind of test to run. The executor uses this to dispatch into
opp_repl via [COMMAND_MAP](../opp_ci/executor.py#L105).

| Aspect | Value |
|---|---|
| CLI flag | `--test` (required, comma-separated for multiple) |
| REST field | `test_type` (required) |
| TestRun column | `test_type` |
| Default | none |

Recognized values: `smoke`, `build`, `fingerprint`, `statistical`,
`feature`, `chart`, `speed`, `sanitizer`, `release`, `opp`, `all`.
See [Axis: test types](test_matrix_dimensions.md#axis-test-types) for
what each entry point does.

Note: the CLI accepts a comma-separated list (`--test smoke,fingerprint`)
and creates one TestRun per value, sharing the install step. The REST
API accepts one value per call.

---

## Identity parameters

These together determine whether the run is considered a duplicate of
an earlier one — `find_existing_run()` in
[executor.py:68](../opp_ci/executor.py#L68) keys on the full tuple.
Use `--force` to bypass the duplicate check. The full tuple is
`(project, version, test_type, mode, git_ref, os, os_version, arch,
compiler, compiler_version, isolation, toolchain)`.

### `mode`

Build mode. Two canonical values:

| Aspect | Value |
|---|---|
| CLI flag | `--mode {debug\|release}` |
| REST field | `mode` |
| TestRun column | `mode` |
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
[_ensure_github_clone()](../opp_ci/executor.py#L346) and
[_resolve_project_dir()](../opp_ci/executor.py#L371).

Under `toolchain=nix`, a non-trivial `git_ref` triggers the `-git`
variant switch in
[resolve_git_project()](../opp_ci/executor.py#L190): the project name
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

### `os` and `os_version`

The target operating system, recorded as two columns.

| Aspect | Value |
|---|---|
| CLI flags | `--os`, `--os-version` |
| REST fields | `os`, `os_version` |
| TestRun columns | `os`, `os_version` |
| Default | `None` (let the worker decide) |

Under `isolation=docker`, both fields are required — the executor
picks the runner image by exact match on
`(os, os_version, compiler, compiler_version)`. Under
`isolation=none`, they constrain dispatch to workers tagged
`os:<name>-<version>`.

### `arch`

CPU architecture. Free-form string; common values are `amd64` and
`aarch64`.

| Aspect | Value |
|---|---|
| CLI flag | `--arch` |
| REST field | `arch` |
| TestRun column | `arch` |
| Default | `None` (no constraint) |

When omitted, the run is unconstrained on architecture — any worker
matching the other tags can pick it up. When set, only workers
tagged `arch:<value>` are eligible (under `isolation=none`); under
`isolation=docker` the value selects an arch-specific image variant.

### `compiler` and `compiler_version`

The C++ compiler.

| Aspect | Value |
|---|---|
| CLI flags | `--compiler`, `--compiler-version` |
| REST fields | `compiler`, `compiler_version` |
| TestRun columns | `compiler`, `compiler_version` |
| Default | `None` |

Under `isolation=docker`, both are required and form part of the
image-tag key. Under `isolation=none, toolchain=none`, they constrain
worker dispatch via the `compiler:<name>-<version>` tag.

Under `toolchain=nix`, opp_env only exposes a small allow-list:

| Compiler | Version |
|---|---|
| `gcc` | `7` |
| `clang` | unspecified |

See [_NIX_SUPPORTED_COMPILERS](../opp_ci/scheduler.py#L150). The
validator runs at matrix-expansion time; the single-test path
delegates to opp_env, so an unsupported pair under `toolchain=nix`
will fail at install/run time rather than at submit time.

### `platform_desc` (derived)

A human-readable summary, e.g. `Ubuntu 24.04 / amd64 / clang-22`,
built by
[_build_platform_desc()](../opp_ci/scheduler.py#L180) from the four
platform columns. Not settable directly; recomputed on every
submission.

| Aspect | Value |
|---|---|
| CLI flag | *(none — derived)* |
| REST field | *(none — server-computed)* |
| TestRun column | `platform_desc` |

---

## Execution environment

### `isolation`

How the job's filesystem and OS are isolated from the host.

| Aspect | Value |
|---|---|
| CLI flag | `--isolation {none\|docker}` |
| REST field | `isolation` |
| TestRun column | `isolation` |
| Default | `none` (CLI); `None` → treated as `none` (REST) |

- `none` — direct subprocess on the worker, host packages.
- `docker` — run inside a runner image keyed by
  `(os, os_version, compiler, compiler_version)`. Image building is
  driven by `opp_ci image build` / `image build-matrix`.

### `toolchain`

Where the C++ toolchain comes from.

| Aspect | Value |
|---|---|
| CLI flag | `--toolchain {none\|nix}` |
| REST field | `toolchain` |
| TestRun column | `toolchain` |
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
opp_ci run --project inet-4.5 --test smoke \
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
| TestRun column | `opp_file` |
| Default | `None` |

Set today only via the matrix (the matrix's `opp_file` field is
copied onto each expanded run). For ad-hoc runs, the executor
auto-discovers `*.opp` files in the current directory; see
[_load_workspace()](../opp_ci/executor.py#L154) for the resolution
order.

---

## Behavior controls

These do not appear on the TestRun row — they only affect submission
behavior.

### `force`

Bypass the duplicate-run check.

| Aspect | Value |
|---|---|
| CLI flag | `--force` |
| REST field | `force` (default `false`) |

Without `--force`, the submitter asks
[find_existing_run()](../opp_ci/executor.py#L68) whether a run with
the same `(project, version, test_type, mode, git_ref, os, os_version,
arch, compiler, compiler_version, isolation, toolchain)` already
exists. If it does, the new submission is skipped (CLI prints a
message; REST returns `{"skipped": true}` with the existing run's
ID).

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

### `status`

Lifecycle state: `queued` → `running` → terminal
(`PASS` / `FAIL` / `ERROR`). Enum in
[TestRunStatus](../opp_ci/db/models.py#L138). `ERROR` distinguishes
infrastructure failure (executor crash, install failure) from
genuine test failure.

### `trigger`

Why the run exists. Set at submission time, never changes:

| Value | Source |
|---|---|
| `manual` | Local `opp_ci run` / `run-matrix`. |
| `remote` | REST submission (CLI `--remote`, `OppCiClient`, web UI). |
| `webhook` | Created by [github/webhook.py](../opp_ci/github/webhook.py). |
| `schedule` | Reserved for the periodic scheduler. |

### `worker_id`

Foreign key to [Worker](concepts.md#worker-model). `NULL` until a
worker picks up the queued job; set when the worker posts its first
status update for the run.

### `matrix_id`

Foreign key to the [TestMatrix](concepts.md#testmatrix) the run was
expanded from. `NULL` for ad-hoc single-test submissions.

### `started_at` / `finished_at` / `duration_seconds`

UTC timestamps and wall-clock duration. Written by the worker (or the
local CLI in direct mode) as the run transitions through states.

### `commit_sha`

The concrete SHA the worker resolved `git_ref` to. Set when the
worker reports results, regardless of pass/fail. Used by the
[git-notes flow](git_notes.md) and by GitHub commit-status posting.

---

## GitHub linkage (webhook-only)

These columns are populated only when the run was created by the
[webhook receiver](concepts.md#webhook-receiver). For
manual/remote runs they remain `NULL`.

| Column | Meaning |
|---|---|
| `github_owner` | GitHub repository owner. |
| `github_repo` | GitHub repository name. |
| `github_commit_sha` | Head SHA of the triggering event. |
| `github_pr_number` | PR number, when the trigger was a `pull_request` event. |
| `github_status_url` | The `statuses_url` to post commit status updates to. |

The worker uses these to post commit statuses, PR comments, and git
notes after the run finishes. They are the only path by which a
single-test row knows it came from a GitHub event.

---

## Required vs. optional summary

| Field | Required for `opp_ci run` | Required for `POST /api/runs` | Required under `isolation=docker` |
|---|---|---|---|
| `project` | yes | yes | yes |
| `test_type` | yes | yes | yes |
| `mode` | no | no | no |
| `git_ref` | no | no | no |
| `os` | no | no | **yes** |
| `os_version` | no | no | **yes** |
| `arch` | no | no | no |
| `compiler` | no | no | **yes** |
| `compiler_version` | no | no | **yes** |
| `isolation` | no (defaults `none`) | no | yes (to select `docker`) |
| `toolchain` | no (defaults `none`) | no | no |
| `pin` | no | n/a | no |
| `force` | no | no | no |
| `skip_install` | no | n/a | no |

---

## End-to-end example

A single test exercising every settable parameter:

```bash
opp_ci run \
    --project inet-4.5 \
    --test fingerprint \
    --mode release \
    --ref topic/my-feature \
    --os Ubuntu --os-version 26.04 \
    --arch amd64 \
    --compiler clang --compiler-version 22 \
    --isolation docker --toolchain none \
    --pin omnetpp=6.1 \
    --skip-install
```

Produces one TestRun row with:

```
project          inet-4.5
test_type        fingerprint
mode             release
git_ref          topic/my-feature
os               Ubuntu
os_version       26.04
arch             amd64
compiler         clang
compiler_version 22
isolation        docker
toolchain        none
platform_desc    Ubuntu 26.04 / amd64 / clang-22
resolved_deps    {"omnetpp": "6.1"}
trigger          manual
status           running → PASS / FAIL / ERROR
worker_id        (assigned)
commit_sha       (resolved from topic/my-feature)
```

The equivalent REST submission:

```python
from opp_ci.client import OppCiClient

ci = OppCiClient(url="https://ci.omnetpp.org/api", token="…")
ci.submit_run(
    project="inet-4.5",
    test_type="fingerprint",
    mode="release",
    git_ref="topic/my-feature",
    os="Ubuntu", os_version="26.04", arch="amd64",
    compiler="clang", compiler_version="22",
)
# isolation/toolchain default to None → treated as "none" by the executor
```

See [python_client.md](python_client.md) for the full client API.
