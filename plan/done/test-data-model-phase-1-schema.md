# Plan: test data model — phase 1 (schema + cutover)

Goal: replace the legacy `TestRun` + `TestResult` pair
([opp_ci/db/models.py:179](../opp_ci/db/models.py#L179)) with the new
entity model from
[test-data-model-redesign.md](test-data-model-redesign.md), just
enough to compile and run. Operational policies, lineage queries,
filtered reruns, expectation history, and similar refinements are
deferred.

Scope is deliberately small. For the open questions that don't block
writing the schema, this plan picks a default that is the simplest
thing that works — each one is revisitable in a later phase. The
"phase 1 specifics" column below makes those picks explicit so a
reviewer can override before code is written.

Backward compatibility is not a concern: the DB is wiped as part of
this phase.

## What this phase delivers

1. SQLAlchemy models for the four entities.
2. Matrix expansion writes the new rows.
3. Worker / scheduler / REST surfaces read and write the new rows.
4. Legacy `TestRun` / `TestResult` models and their callers removed.
5. DB wiped and recreated.

## Entities in this phase

In dependency order. Full field list and rationale live in
[test-data-model-redesign.md](test-data-model-redesign.md); only the
phase-1 picks are repeated here.

| # | Entity | Phase 1 specifics |
|---|---|---|
| 1 | `Test` | The legacy `test` column (kind of test within a project — `"smoke"`, `"fingerprint"`, …) is renamed to `kind` to avoid `Test.test`. The external naming (matrix `kinds:` axis, job dict `kind`, form/CLI/API field `kind`) is renamed in lockstep. Has three mutable columns: `name`, `expected_result_code`, `expected_result_description` — all nullable, user-editable, and excluded from the `coord_hash`. Every other column is immutable after row creation. `coord_hash` covers the locked field list below. |
| 2 | `TestMatrix` | Anonymous matrices get a generated `name` and a persisted row, so `TestMatrixRun.matrix_id` is `NOT NULL`. `name` is the only mutable column; every other field is immutable. To change a matrix's content, create a new matrix. |
| 3 | `TestRun` | Lifecycle + outcome live on the same row (no separate `TestResult` table). `result_code`, `stdout`, `stderr`, `details` are nullable and populated iff `lifecycle == finished`. `system_snapshot` is a nullable JSONB column on the same row — Postgres TOAST keeps it out-of-line, compressed, and lazy-loaded, so the column-on-row layout costs nothing on queries that don't touch it. `matrix_run_id` nullable (single-`Test` runs have no parent matrix run). `worker_id` nullable, set at claim time. No lineage FK. `details` is free-form JSON. No retention policy on `system_snapshot` in phase 1 — values accumulate. |
| 4 | `TestMatrixRun` | **No own `lifecycle` column in phase 1.** Status is a roll-up computed from child `TestRun`s in app code. Promote to a column later only if the roll-up query becomes the bottleneck. |

## `Test.coord_hash` — closed field list for phase 1

Sorted-keys JSON over these fields, SHA-256 hex, unique index:

```
project, kind, mode,
os, os_version, distro, distro_version, flavor, flavor_version, arch,
compiler, compiler_version,
isolation, toolchain,
opp_file
```

Adding or removing a field later changes every hash. Treat this list
as frozen for phase 1; any change is a bump-and-rebuild.

## Behaviors locked for phase 1

These all carry over from the parent plan; repeated here so a
phase-1 reader doesn't have to round-trip.

- **`TestRun` cancel**: only queued rows transition to `cancelled`. Running rows finish normally; their outcome columns are populated on the same row.
- **`TestMatrixRun` cancel**: cascades to queued children. Running children finish normally.
- **Rerunning a `Test` or `TestMatrix`**: always creates new rows; never mutates existing ones.
- **No lineage FK** between original and rerun rows. The shared `Test` / `TestMatrix` row is the anchor.
- **Matrix rerun scope (phase 1)**: always re-expands the matrix and runs every child. Filtered "rerun only the failed ones" is phase 2.
- **Result caching / forced rerun**: not in scope. Every submitted run executes.

## Out of scope (deferred)

Each of these is an open question in
[test-data-model-redesign.md](test-data-model-redesign.md). None
blocks phase 1.

- Promoting `TestMatrixRun` status to its own column (vs. the phase-1 roll-up).
- Filtered matrix rerun ("rerun only the failed/errored children").
- Audit / history for mutable columns on `Test` (`name`, `expected_result_code`, `expected_result_description`).
- `TestRun.details` JSON schema.
- `TestRun.system_snapshot` retention policy (pruning via `UPDATE … SET system_snapshot = NULL WHERE …`).
- Per-individual-test granularity below the suite.

## Order of work

1. Define the four SQLAlchemy models.
2. Generate the schema; wipe and recreate the DB.
3. Rewrite matrix expansion to write `Test` rows (deduped via `coord_hash`) and the `TestMatrixRun` + child `TestRun` rows.
4. Rewrite the worker dequeue path to claim a `TestRun`, populate `started_at`/`worker_id`/`system_snapshot` at start, and update the same `TestRun` row with `result_code`/`stdout`/`stderr`/`details`/`finished_at` on finish.
5. Update REST surfaces (list, filter, detail views) to use the new tables.
6. Delete the legacy `TestRun` / `TestResult` models and every call site that referenced them.
