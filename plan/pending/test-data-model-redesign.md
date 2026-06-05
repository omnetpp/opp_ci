# Plan: redesign the test data model

Goal: replace the legacy `TestRun` + `TestResult` pair with a model
that cleanly separates *what is being tested* from *each attempt to
test it*. The legacy schema conflated both on a single `TestRun` row
and used `TestResult` as a 1:N child of questionable purpose. The new
model reuses the `TestRun` name for the per-attempt entity (with
materially different semantics from the legacy row) and folds the
outcome columns onto `TestRun` directly — no separate `TestResult`
table, they were always 1:0..1. The expectation, also 1:0..1 with
`Test`, similarly lives as nullable columns on `Test` rather than in a
separate table.

Backward compatibility is **not** a concern: the database is wiped as
part of this change. Migrations are not in scope.

## Phase 1 status — shipped

The cutover to this model is implemented and merged. Schema lives in
[`opp_ci/db/models.py`](../opp_ci/db/models.py) (`Test` at L229,
`TestMatrixRun` at L266, `TestRun` at L293); persistence helpers in
[`opp_ci/persistence.py`](../opp_ci/persistence.py). The phase-1
cutover plan, with the exact phase-1 picks for the non-blocking
defaults, is in [done](../done/test-data-model-phase-1-schema.md).

This parent plan is the long-form design doc. Below: locked decisions
(what's already committed in code), then the still-open questions for
future phases.

## The new entities

Defined in dependency order — each row depends only on rows above it.

| # | Entity | Cardinality | Role |
|---|---|---|---|
| 1 | `Test` | one per distinct coordinate in the cube | Deduped definition of "this suite at this platform/mode/compiler/…", plus a small set of editable per-test metadata: `name`, `expected_result_code`, `expected_result_description`. The dedup target. |
| 2 | `TestMatrix` | one per saved or anonymous matrix | Generator. Expands into many `Test`s. |
| 3 | `TestRun` | one per attempt | Lifecycle + outcome row. Exists from the moment of submission. Carries the per-attempt context (commit, deps, worker, timing) and, once finished, the outcome (`result_code`, stdout, stderr, details). Targets one `Test`. |
| 4 | `TestMatrixRun` | one per matrix invocation | First-class grouping of the `TestRun`s produced by submitting a matrix. Enables rerun / cancel / progress queries even when the matrix is anonymous. |

## Locked design decisions

| Question | Decision |
|---|---|
| Name for the cube-point | `Test`. May refine to a parent `TestSuite` + per-coordinate `Test` (or split further into per-individual-test rows) later if/when individual tests within a suite become first-class. |
| Lifecycle vs. outcome | Separate columns on the same row. `TestRun.lifecycle ∈ {queued, running, finished, cancelled, timed_out}`; `TestRun.result_code ∈ {PASS, FAIL, ERROR, SKIPPED}` and is non-null iff `lifecycle == finished`. No single enum that mixes the two. |
| `TestResult` table | Merged into `TestRun`. The relationship was always 1:0..1, and the "presence of a row" signal is replaceable by `lifecycle == finished` (equivalently, `result_code IS NOT NULL`). Outcome columns (`result_code`, `stdout`, `stderr`, `details`) live on `TestRun` as nullable. |
| Expectation lives on `Test` | The expectation is a 1:0..1 property of a `Test`, so it lives as two nullable columns on `Test` rather than in a separate table: `expected_result_code` and `expected_result_description`. Same merge rationale as `TestResult`→`TestRun`. |
| Expectation shape | `expected_result_code` (PASS/FAIL/ERROR/SKIPPED, nullable — `NULL` means "no expectation set, defaults to PASS at the grading layer") plus a free-text `expected_result_description` for the human reason behind it (e.g. "FAIL on macOS-arm64 because clang 17 miscompiles the fingerprint computation, tracked in #432"). No expected stdout/fingerprint/tolerances yet. No structured provenance or history. Can grow later. |
| `TestMatrixRun` is first-class | Even when the user submits an anonymous (unsaved) matrix, a `TestMatrixRun` row is created. Lets us answer "rerun the failed ones from matrix run 42" without requiring the matrix to have been saved. |
| Dedup | `Test` has a canonical hash column over the coordinate fields. Sorted-keys JSON → SHA-256 hex. Unique index. |
| Backfill on new dimensions | When a new dimension is added, each existing `Test` is assigned a defined default value before the hash is recomputed. The hash space does not silently fracture. |
| Cancelling a `TestRun` | Only meaningful for queued `TestRun`s: state transitions to `cancelled`, outcome columns remain `NULL`. A running `TestRun` cannot be interrupted — the worker is allowed to finish, the outcome is recorded on the same row, and `lifecycle` ends in `finished`. "Cancel a running `TestRun`" is effectively a no-op. |
| Cancelling a `TestMatrixRun` | Cascades to its children: every queued child `TestRun` is cancelled. Already-running children finish normally per the rule above. No worker-abort mechanism is needed. |
| Rerunning a `Test` or `TestMatrix` | Always creates new rows; existing rows are never mutated. Running a `Test` creates a fresh `TestRun`. Re-running it later creates another fresh `TestRun`. Running a `TestMatrix` creates a fresh `TestMatrixRun` with its child `TestRun`s. Re-running the matrix creates another `TestMatrixRun`. This keeps history append-only and makes "what happened on attempt N?" a direct row lookup. |
| Rerun lineage | No FK from the new `TestRun`/`TestMatrixRun` to the original. The `Test` (resp. `TestMatrix`) row is the shared anchor — "all attempts of this `Test`" is just `SELECT FROM TestRun WHERE test_id = X`. No graph traversal needed. |
| Cache / "should we actually re-execute?" | Whether a rerun request *re-executes* on the worker, or returns a cached prior outcome for the same `(Test, commit_sha, …)`, is a separate execution-time concern (a "forced" rerun would bypass any such cache). Not in scope for this schema redesign. |
| Single-`Test` runs without a matrix | A `TestRun` may exist with `matrix_run_id = NULL`. Running a single `Test` (whether a first-time invocation or a one-off rerun) creates a `TestRun` parented to no `TestMatrixRun`. (Resolves open question 6.) |
| Mutable columns on `Test` and `TestMatrix` | `TestMatrix` has exactly one mutable column: `name` (optional, nullable, user-editable label). `Test` has three: `name`, `expected_result_code`, `expected_result_description`. Every other column on either row is fixed at creation time — to change a coordinate or matrix dimension you create a new row. All three `Test` mutables are excluded from `Test.coord_hash` so editing them never affects dedup. |
| Name for the suite-kind column | The legacy `Test.test` column (kind of test — `"smoke"`, `"fingerprint"`, `"statistical"`, …) is renamed to `kind`. Applied consistently across **every** surface — column, matrix axis (`kinds:`), job dict key, form fields, CLI flags (`--kind` / `--kinds`), API fields, URL query params, local variables, template labels. (`Test.test` is awkward in Python and once the column is renamed there's no reason to keep `test` as the external word.) |
| `coord_hash` field list | Closed list: `project, kind, mode, os, os_version, distro, distro_version, flavor, flavor_version, arch, compiler, compiler_version, isolation, toolchain, opp_file`. Sorted-keys canonical JSON → SHA-256 hex. Adding/removing a field re-keys every existing `Test`; treat as bump-and-rebuild. |
| Anonymous matrices: persistence | Anonymous matrices still get a persisted `TestMatrix` row with an auto-generated name, so `TestMatrixRun.matrix_id` is `NOT NULL` and the join is uniform. (Resolves former open question 3.) |
| Worker assignment timing | `TestRun.worker_id` is nullable while `lifecycle=queued` and set at claim time inside the dequeue transaction. No pre-assignment. (Resolves former open question 6.) |
| Matrix rerun scope (phase 1) | "Rerun a `TestMatrixRun`" always re-expands the matrix and runs all children — no filtered "rerun only the failed ones" yet. A filtered variant is a future feature decision (see open questions). |
| `TestMatrixRun` lifecycle column (phase 1) | No own column; status is rolled up from child `TestRun.lifecycle` in app code. Promote to a column later only if the roll-up query becomes the bottleneck. |

## Field placement: coordinate vs. run context

| Field | Lives on | Why |
|---|---|---|
| `name` (nullable, editable) | `Test`, `TestMatrix` | Optional user-editable label. Excluded from `Test.coord_hash`. |
| `expected_result_code`, `expected_result_description` (both nullable, editable) | `Test` | Human-controlled expectation. `NULL` `expected_result_code` means "no expectation set". Excluded from `Test.coord_hash`. |
| `project`, `kind`, `mode` | `Test` | Identity of what's being tested. (`kind` is the renamed legacy `test` column.) |
| `os`, `os_version`, `distro`, `distro_version`, `flavor`, `flavor_version`, `arch` | `Test` | Platform coordinates. See [distro-and-flavor-dimensions.md](distro-and-flavor-dimensions.md). |
| `compiler`, `compiler_version` | `Test` | Toolchain coordinate. |
| `isolation`, `toolchain` | `Test` | Run-environment coordinate. |
| `opp_file` | `Test` | Locates the test definition; part of identity. |
| `coord_hash` | `Test` | Canonical hash for dedup. |
| `commit_sha`, `git_ref`, `version` | `TestRun` | Identifies the code under test. Different commits → many `TestRun`s of the same `Test`. Enables "did this `Test` regress?". |
| `resolved_deps` | `TestRun` | Snapshotted per attempt; may differ between attempts of the same `Test`. |
| `worker_id`, `started_at`, `finished_at`, `duration_seconds`, `lifecycle` | `TestRun` | Per-attempt facts. |
| `result_code`, `stdout`, `stderr`, `details` | `TestRun` | Outcome columns. All nullable. Populated iff `lifecycle == finished`. Postgres TOAST stores large `stdout`/`stderr` out-of-line and lazy-loads them, so size doesn't hurt lifecycle queries. |
| `system_snapshot` (JSONB, nullable) | `TestRun` | Rolling capture of system facts at run time: rolling-release versions, OS build/kernel, libc, CPU model, RAM, disk, env vars relevant to the run, package versions of resolved deps, container/podman image digest, omnetpp/inet build hash, etc. See [System snapshot](#system-snapshot). Lives on `TestRun` directly; Postgres TOAST handles out-of-line storage, compression, and lazy access. Best-effort, never blocks the run. |
| `trigger`, `github_owner`, `github_repo`, `github_commit_sha`, `github_pr_number`, `github_status_url` | `TestMatrixRun` | A submission's origin is a property of the matrix run, not of each child `TestRun`. |
| `matrix_id` | `TestMatrixRun` | The matrix that produced this row. Nullable for anonymous matrices. |
| `platform_desc` | dropped | Denormalized display string. Derive on render. |

## Relationships

```
TestMatrix ──(expands into)──▶ Test (many, via dedup)
     │
     └──(invoked as)──▶ TestMatrixRun ──(spawns)──▶ TestRun
                            │                        │
                            │                        └──(targets)──▶ Test
                            │
                            └── matrix_id nullable (anonymous matrix)

```

## System snapshot

Every `TestRun` captures a JSON blob of system facts at run time, in
the `system_snapshot` JSONB column. The point is to make months-later
"why did this fail back then?" debugging possible without re-deriving
the world from a commit SHA. On rolling-release distros (Arch, Nix
unstable, Tumbleweed) the SHA tells us nothing about what was actually
installed; the snapshot does.

The column lives on `TestRun` directly rather than in a side table.
Postgres TOAST automatically stores large values out-of-line,
compresses them (`pglz` by default, `lz4` on PG14+), and lazy-loads
them only when the column is explicitly selected — so a column-on-row
layout gets the size and laziness benefits a side table would have
offered, without the join. Pruning is done by `UPDATE … SET
system_snapshot = NULL WHERE …`, leaving the lifecycle row and its
outcome intact.

Captured best-effort, never blocks the run. If a probe fails, the
corresponding field inside the JSON is just absent. If snapshot
capture fails entirely, the column is left `NULL`.

What goes in (initial list, will refine):

- **Time**: wall-clock `started_at` is on the row; snapshot also
  records timezone, monotonic clock, and the worker's NTP offset if
  available.
- **OS / kernel**: `uname -a`, `/etc/os-release`, kernel build date,
  kernel cmdline.
- **Rolling-release identifiers**: pacman db timestamp, nix channel
  generation, apt last-update, dnf transaction id — whichever applies.
- **Libc / linker**: `ldd --version`, dynamic linker path.
- **Toolchain**: full `--version` output for the active compiler,
  linker, make, cmake, python.
- **CPU / memory / disk**: `/proc/cpuinfo` summary (model, flags,
  microcode), `/proc/meminfo` totals, `df` on the work dir, mount
  options (noatime, tmpfs, …).
- **Container/isolation**: podman image name + digest, container
  runtime version, cgroup version, security flags.
- **Project artifacts**: opp_env lockfile hash, omnetpp build
  configuration (`Makefile.inc` snippet or build mode), inet feature
  set, any `*.mode` file content.
- **Resolved deps**: package versions actually present (not just the
  spec) — overlaps with `TestRun.resolved_deps` but records installed
  versions, not requested ones.
- **Environment**: filtered env vars (`PATH`, `LD_LIBRARY_PATH`,
  `OMNETPP_*`, `CC`, `CXX`, `OMP_NUM_THREADS`, …). Never the full
  environment — secrets risk.
- **Worker**: hostname, opp_ci worker version, uptime.

Stored as one JSON column for now. If a particular field becomes a
common query target, promote it to its own column later.

## Open questions

Schema-blocking questions are all resolved (see locked decisions above
and [phase-1 plan in done](../done/test-data-model-phase-1-schema.md)).
What remains is future-phase work — operational policies, feature
extensions, and granularity refinements.

1. **Promote `TestMatrixRun.lifecycle` to a real column?** Phase 1 rolls
   up child `TestRun.lifecycle` in app code. Worth promoting once we see
   real query patterns where the roll-up is the bottleneck (dashboards
   over many matrix runs, "list active matrix runs" filtered queries).
   If promoted: who keeps it in sync — a trigger on `TestRun`, a
   write-time recompute in the persistence helpers, or a periodic
   reconciler?

2. **Filtered matrix rerun.** Phase 1 only supports "rerun every child"
   when a `TestMatrixRun` is re-submitted. Add "rerun only the
   failed/errored children" as a UI/CLI option? If so, does the new
   `TestMatrixRun` re-expand the (possibly evolved) `TestMatrix` and
   pick a subset, or copy the surviving subset of `Test`s from the old
   `TestMatrixRun` directly? Both produce different semantics when the
   matrix has been edited between attempts.

3. **Audit / history for mutable columns on `Test`.** `Test` has three
   mutables (`name`, `expected_result_code`,
   `expected_result_description`). A silent overwrite of
   `expected_result_code` changes how every past `TestRun` grades, and a
   silent rename loses context. Do we want a single audit mechanism
   covering all three (audit table, trigger, or `updated_at`/`updated_by`
   columns), or nothing at all? Phase 1 has nothing.

4. **`TestRun.details` JSON schema.** Phase 1 stores it as a free-form
   blob. If a particular field becomes a common query target (e.g.
   per-subtest breakdown for a comparison view, fingerprint mismatch
   data for triage), promote it to its own column or codify a JSON
   schema. Risk of becoming a junk drawer otherwise.

5. **`system_snapshot` retention policy.** Pruning is `UPDATE … SET
   system_snapshot = NULL WHERE …` — operationally cheap, semantically
   safe (lifecycle row + outcome stay). The policy is open: drop after
   N months, drop above a size threshold, drop only for
   `result_code=PASS` runs, never drop? Likely a deployment-config
   question rather than a code one.

6. **Cancel / abort for running `TestRun`s.** Phase 1 lets running runs
   finish — cancel only transitions queued rows. If we ever want a real
   abort (worker-side signal, polling flag), it lands here. Out of
   scope until there's a concrete need.

7. **Suite-internal granularity.** Deferred: a `Test` represents a
   *full suite* at coordinates, and the suite's internal per-test
   results collapse into one aggregate outcome on `TestRun`. When we
   eventually want per-individual-test results, the path is to add a
   per-individual-test entity below `Test` and split the outcome
   accordingly (re-introducing a child outcome table is one option).
   The names should not block this future split.
