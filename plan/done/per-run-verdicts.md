# Per-run verdicts (nullable `TestVerdict.matrix_run_id`)

## Goal

Give **every finished `TestRun` its own verdict** — `EXPECTED /
UNEXPECTED / UNKNOWN` — not just runs that were promoted as cells of a
`TestMatrixRun`. Surface it on the run detail page and in CLI
`list-runs`.

Semantics: **snapshot**. The verdict is computed once, at finalize time,
against the Test's expectation *then in force*, and frozen
(`expectation_id` + `recorded_at` pinned). Later edits to the
expectation do not retro-change a recorded verdict. (The *compatibility
matrix* will want live/current-expectation semantics instead — that is a
separate, later piece of work and deliberately out of scope here.)

## Design

Generalize [`TestVerdict`](../../opp_ci/db/models.py#L362) from "one row
per **cell of a `TestMatrixRun`**" to "one recorded verdict of a
`TestRun`, *optionally* within a matrix." Matrix cells keep
`matrix_run_id` set; standalone runs get a row with `matrix_run_id =
NULL`.

### Why this is a small change

- The verdict is already a pure function of `(run.result_code,
  Test expectation)` — [`compute_verdict_kind`](../../opp_ci/persistence.py#L245)
  needs no matrix.
- [`finalize_verdict_for_run`](../../opp_ci/persistence.py#L385) is
  **the single chokepoint every run-completion path already calls**
  (matrix *and* standalone — verified: standalone runs are created via
  `create_test_run` at cli.py:877, api.py:181, app.py:684/745/769, and
  each calls `finalize_verdict_for_run`). Its promotion loop is not
  matrix-specific, and its rollup loop already guards `if mid is not
  None` ([persistence.py:424](../../opp_ci/persistence.py#L424)).
- [`TestRun.recorded_verdict`](../../opp_ci/db/models.py#L567) iterates
  `self.verdicts` regardless of `matrix_run_id`, so it starts returning
  the standalone verdict for free.

### Creation hook: in `finalize`, not at creation

Do **not** add cell-creation to the five `create_test_run` sites.
Instead, in `finalize_verdict_for_run`, after the existing
finished/result-code guard:

1. Check whether the run has **any** `TestVerdict` row.
2. If none, create one bare pending cell via `create_test_verdict(...,
   matrix_run_id=None, verdict=None, recorded_at=None)`.
3. Fall through to the existing pending-promotion loop, which sets
   `expectation_id` / `verdict` / `recorded_at`.

This auto-dedups: matrix runs already have cells (created in
`enqueue_job`), so no bare row is added for them. It is idempotent: a
second `finalize` finds the now-promoted cell (verdict no longer NULL),
creates nothing, promotes nothing. Runs with `result_code is None`
(cancelled / timed_out) return early as today → no verdict, which is
correct (no actual outcome to judge).

## Implementation

### 1. Schema — `opp_ci/db/models.py`

- `TestVerdict.matrix_run_id`: `nullable=False` → `nullable=True`
  ([models.py:375](../../opp_ci/db/models.py#L375)). FK unchanged.
- Update the class docstring (and the `matrix_run` relationship comment)
  to the generalized meaning.

### 2. Migration — none

No Alembic migration. The existing DB is recreated from scratch
(`create_all`), so it picks up the nullable column directly. (If a
migration is ever needed for a live DB, relax the NOT NULL via
`op.batch_alter_table("test_verdicts")` for SQLite+Postgres parity.)

### 3. Creation hook — `opp_ci/persistence.py`

In [`finalize_verdict_for_run`](../../opp_ci/persistence.py#L385), after
the `lifecycle/result_code` guard and before selecting `pending`, insert
the "create bare cell if none exists" step described above. Reuse
[`create_test_verdict`](../../opp_ci/persistence.py#L267) (already
accepts everything; just pass `matrix_run_id=None`).

### 4. Display — run detail (web)

`opp_ci/web/templates/run_detail.html`: render `run.recorded_verdict`
as a badge (e.g. `EXPECTED` neutral/green, `UNEXPECTED` red, `UNKNOWN`
muted; hide when `None`). The route already loads the run; no query
change needed since `recorded_verdict` walks the loaded relationship.
Consider also showing the frozen expectation it was judged against.

### 5. Display — CLI `list-runs`

[cli.py:1218](../../opp_ci/cli.py#L1218): add a `Verdict` column to the
header and the row format string, sourced from `run.recorded_verdict or
"-"`. (Same treatment optionally at the other `list` formatter,
cli.py:1430.)

### 6. Tests — `tests/test_per_run_verdict.py` (new)

- Standalone run, no expectation → finalize creates one
  `TestVerdict(matrix_run_id=NULL, verdict=UNKNOWN)`;
  `run.recorded_verdict == "UNKNOWN"`.
- Standalone run with a matching expectation → `EXPECTED`; mismatching →
  `UNEXPECTED`; `expectation_id` pinned (snapshot: later expectation
  edit does **not** change the recorded verdict).
- Idempotency: calling `finalize_verdict_for_run` twice yields exactly
  one verdict row.
- Matrix run still gets exactly its matrix cell — **no** extra
  NULL-matrix row.
- Cancelled / timed_out run (result_code None) → no verdict row.

## Files touched

| File | Change |
|---|---|
| [opp_ci/db/models.py](../../opp_ci/db/models.py) | `matrix_run_id` nullable; docstring |
| [opp_ci/persistence.py](../../opp_ci/persistence.py) | `finalize_verdict_for_run`: create bare cell if none |
| `opp_ci/web/templates/run_detail.html` | verdict badge |
| [opp_ci/cli.py](../../opp_ci/cli.py) | `Verdict` column in `list-runs` |
| `tests/test_per_run_verdict.py` | new — coverage above |

## Out of scope (next)

Compatibility-matrix coloring by verdict — and specifically whether the
matrix reads these frozen verdicts or recomputes **live** against the
current expectation. Tracked in
[compatibility-matrix-dimension-filters.md](compatibility-matrix-dimension-filters.md)
follow-up.
