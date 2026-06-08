# Plan: run tests and matrices by name or anonymously (web UI parity)

Goal: give the web UI two **structurally identical** pages — one for
running a single **test**, one for running a **matrix** — where each
page lets the user either

- **run by name** — pick a previously-named definition and queue it, or
- **run anonymously** — fill in an inline form and queue a one-shot,
  optionally naming it so it becomes reusable later.

This mirrors the structure the **CLI already has** for matrices
([`run-matrix`](../../opp_ci/cli.py#L1083): `--matrix NAME` vs. inline
axis flags) and closes three gaps:

1. The web UI cannot run a matrix anonymously at all — only named
   matrices can be queued, via the "Run from Matrix" picker bolted onto
   the single-run page ([app.py:656](../../opp_ci/web/app.py#L656),
   [run_new.html:161](../../opp_ci/web/templates/run_new.html#L161)).
2. The web UI cannot run a single test *by name* — every single run is
   built from the inline coordinate form, even though `Test.name`
   already exists in the schema.
3. A matrix is forced to carry a name (`TestMatrix.name` is
   `NOT NULL` — [models.py:82](../../opp_ci/db/models.py#L82)), so the
   "anonymous matrix" concept is faked with a synthesised
   `adhoc:project:timestamp` string
   ([`_generate_anonymous_matrix_name`](../../opp_ci/cli.py#L1073)).
4. The TestMatrixRun **index and detail pages exist but don't mirror
   their TestRun counterparts**: the matrix-run index
   ([matrix_runs.html](../../opp_ci/web/templates/matrix_runs.html)) has
   no "+ New" header action and no per-row **Actions** column, and there
   are **no matrix-run rerun/cancel endpoints** at all — whereas TestRun
   has [`run_rerun`](../../opp_ci/web/app.py#L571) /
   [`run_cancel`](../../opp_ci/web/app.py#L596) and an Actions column
   ([runs.html:54](../../opp_ci/web/templates/runs.html#L54)).

The user's framing: *"A Test can have a name just like a TestMatrix can.
The user should be able to name a Test when it is created on the first
run, or later edit its name to find it and re-run it. Tests should be
deduped, so only one test with the exact same coordinates exists. It's
the same for TestMatrix… it may or may not have a name; it can be run
anonymously and also looked up by name and run by name."*

## Background: what already exists

### Tests are already deduped and already nameable

[`Test`](../../opp_ci/db/models.py#L235) has a nullable, mutable
[`name`](../../opp_ci/db/models.py#L243) column that is **excluded from
`coord_hash`** so renaming never affects dedup. Dedup is enforced by the
unique [`coord_hash`](../../opp_ci/db/models.py#L262) over the closed
[`TEST_COORD_FIELDS`](../../opp_ci/db/models.py#L188) set, and
[`get_or_create_test`](../../opp_ci/persistence.py#L48) already returns
the existing row when the coordinates match. So the data layer is
*already* exactly what the user asked for — only one Test per coordinate,
with an editable name. **No test-side dedup work is needed.** What's
missing is UI/CLI/API surface to set the name, edit it, and run by it.

### Matrices carry a mandatory name; anonymity is faked

[`TestMatrix.name`](../../opp_ci/db/models.py#L82) is
`unique=True, nullable=False`. The CLI's anonymous path therefore
*invents* a name (`adhoc:inet:20260608T…Z`) just to satisfy the NOT NULL
constraint ([cli.py:1196](../../opp_ci/cli.py#L1198)). The web has no
anonymous path at all. To make "a matrix may or may not have a name"
real, `name` must become nullable; an anonymous matrix then stores
`NULL` instead of a synthetic string.

Note on uniqueness: a SQL `UNIQUE` constraint treats `NULL`s as
distinct (Postgres and SQLite both allow many `NULL`s under a unique
column), so simply dropping `NOT NULL` keeps "named matrices are unique"
while permitting any number of anonymous (`NULL`-named) matrices. The
same partial-uniqueness needs to be **added** for `Test.name`, which is
currently not unique at all.

### The CLI is the reference for the page structure

[`run-matrix`](../../opp_ci/cli.py#L1083) accepts three mutually
exclusive input modes — `--matrix NAME`, `--spec-file`, or inline axis
flags — and for the latter two builds a `TestMatrix` row on the fly,
creates a [`TestMatrixRun`](../../opp_ci/db/models.py#L271), expands via
[`expand_matrix`](../../opp_ci/cli.py#L1208), and enqueues a job per
cell. The new web matrix-run page is the GUI of that same flow. The
single-test [`run`](../../opp_ci/cli.py#L114) command has **no** `--name`
and no run-by-name mode; that asymmetry gets fixed here too so the CLI
and web stay aligned.

## Design decisions

| Question | Decision |
|---|---|
| Two separate pages or one page with tabs? | **Two pages**, parallel structure: `/runs/new` (run a test) and `/matrix-runs/new` (run a matrix). The user asked for a *separate* page; keeping them separate also keeps each form short. |
| Where does "Run from Matrix" live after this? | **Moved entirely** to `/matrix-runs/new` (named-matrix section). Removed from `run_new.html` and `run_new_matrix` is relocated/renamed. (User: "Move to matrix page".) |
| What backs "run a test by name"? | The existing `Test.name`. No new entity, no 1×1-matrix modelling. A named test is a `Test` row whose `name IS NOT NULL`. |
| What backs "run a matrix by name"? | The existing `TestMatrix` with `name IS NOT NULL`. Anonymous = a `TestMatrix` row with `name IS NULL`. |
| How is "by name" presented? | **A filterable table, not a combobox.** Each row shows the definition's coordinates (test) / config summary (matrix) plus a per-row **Run** button, with a name/project filter above. The user sees *what* a definition is about before running it. Models the existing [`/runs`](../../opp_ci/web/templates/runs.html) and [`/matrices`](../../opp_ci/web/templates/matrices.html) list pages. The Run action posts the row's **id** (the row was located by the filter); `get_*_by_name` lookups stay for the CLI/REST by-name path. |
| Make `TestMatrix.name` nullable? | **Yes.** Drop `NOT NULL`; keep the unique constraint (NULLs stay distinct). Stop synthesising `adhoc:…` names — store `NULL`. |
| Make `Test.name` unique? | **Yes, partial-unique** (`WHERE name IS NOT NULL`). Required so "look up by name" is unambiguous. Coordinates stay deduped independently by `coord_hash`. |
| Should an anonymous run be able to *save* a name? | **Yes.** Both anonymous forms have an optional "Name (optional)" field. Filling it promotes the one-shot into a reusable named definition (sets `Test.name` / `TestMatrix.name`); leaving it blank keeps it anonymous. This is the single mechanism behind "name it on first run". |
| Where is a name *edited later*? | A small rename form on the run/test surface (`run_detail.html`, expectations page) and on `matrix_detail.html`. New `POST /tests/{id}/rename` and `POST /matrices/{id}/rename`. |
| Anonymous matrix display label when `name IS NULL`? | Show `(anonymous #<id>)` everywhere `matrix.name` is rendered (matrices list, matrix-run rows, detail headers). Add a `TestMatrix.display_name` property so templates don't each reinvent the fallback. |
| Keep `/matrices/new` (save-without-running)? | **Yes, unchanged in scope.** Authoring a saved matrix definition without running it stays its own page; the new run page cross-links to it. Merging the two is out of scope. |
| CLI/REST parity in this plan? | **Yes**, as explicit later steps. Web is the core; CLI `run --name` / run-by-name and the REST anonymous-matrix body follow so the three surfaces don't drift (same rationale as [[project_test_module_import_cycle]]-style cross-layer consistency). |

## Naming / API-change table

| Surface | Before | After |
|---|---|---|
| `TestMatrix.name` column | `unique=True, nullable=False` | `unique=True, nullable=True` |
| `Test.name` column | nullable, **not** unique | nullable, **partial-unique** (`WHERE name IS NOT NULL`) |
| Anonymous matrix name (CLI) | synthesised `adhoc:proj:ts` | `NULL` (drop `_generate_anonymous_matrix_name`) |
| Web `/runs/new` page | Single Run + Run-from-Matrix on one page | Run-named-test + Run-anonymous-test; matrix form removed |
| Web `/matrix-runs/new` | did not exist | Run-named-matrix + Run-anonymous-matrix |
| Web `POST /runs/new/matrix` | handler on the run page | relocated to the matrix-run page (`POST /matrix-runs/new/named`) |
| Web `POST /tests/{id}/rename` | did not exist | set/clear `Test.name` |
| Web `POST /matrices/{id}/rename` | did not exist | set/clear `TestMatrix.name` |
| CLI `run` | no `--name`, no run-by-name | `--name NAME` (set on first run); `--test NAME` (run an existing named test) |
| REST `SubmitMatrixRequest` | `{matrix_name: str}` (required) | `matrix_name` *or* inline `{project, axes…, name?}` |
| REST `SubmitRunRequest` | coordinate fields | + optional `name`; + optional `test_name` to run by name |

## Files to touch

### Schema + migration

- [opp_ci/db/models.py](../../opp_ci/db/models.py)
  - [`TestMatrix.name`](../../opp_ci/db/models.py#L82): `nullable=True`.
  - Add a `display_name` property to `TestMatrix` returning
    `self.name or f"(anonymous #{self.id})"`.
  - `Test.name`: keep the column; add a partial unique index. Either a
    `__table_args__` `Index("uq_test_name", "name", unique=True,
    sqlite_where=…, postgresql_where=…)` or a named `UniqueConstraint`
    via a migration. Mirror it for `TestMatrix.name` if the existing
    plain `unique=True` needs to become an explicit partial index for
    parity (Postgres already allows multiple NULLs under `UNIQUE`, so
    this is optional for matrices and required for tests).
- New Alembic revision under the versions dir referenced by
  [alembic.ini](../../alembic.ini):
  - `ALTER TABLE test_matrices ALTER COLUMN name DROP NOT NULL`.
  - Backfill: leave existing `adhoc:%` names as-is (don't rewrite to
    NULL — they're historical), OR optionally `UPDATE … SET name = NULL
    WHERE name LIKE 'adhoc:%'`. **Decision: leave them**; only *new*
    anonymous matrices store NULL. Note this in the migration docstring.
  - Add the partial unique index on `tests.name`. Pre-check for
    existing duplicate non-null test names and fail the migration with a
    clear message if any exist (there should be none today — nothing
    sets `Test.name` yet).

### Persistence helpers (new shared code)

- [opp_ci/persistence.py](../../opp_ci/persistence.py)
  - `get_test_by_name(session, name)` → `Test | None`.
  - `get_matrix_by_name(session, name)` → `TestMatrix | None`.
  - `set_test_name(session, test, name)` / `set_matrix_name(session,
    matrix, name)` — set-or-clear with a `ValueError` on collision so
    every caller (web, CLI, REST) reports the same "name already taken"
    error. Empty/blank → `NULL`.
  - `create_matrix_from_axes(session, *, project, name, config,
    opp_file=None)` — the shared "build a `TestMatrix` row from a config
    dict" step used by `matrix_create`, the new anonymous-run handler,
    and the CLI. Replaces the inline `TestMatrix(...)` construction in
    [matrix_create](../../opp_ci/web/app.py#L1194) and
    [run_matrix](../../opp_ci/cli.py#L1198).

### Web — Run Tests page

- [opp_ci/web/app.py](../../opp_ci/web/app.py)
  - [`run_new_form`](../../opp_ci/web/app.py#L513): also pass the list of
    **named** tests (`select(Test).where(Test.name.isnot(None))`) for the
    by-name **table**, and accept optional `name`/`project` query-param
    filters on that list, mirroring
    [`matrices_list`](../../opp_ci/web/app.py#L996). Drop the `matrices`
    context (no longer rendered here).
  - [`run_new_submit`](../../opp_ci/web/app.py#L583): add an optional
    `name: str = Form(default="")`. After `get_or_create_test`, if `name`
    is set call `set_test_name` (catch `ValueError` → redirect back with
    `message_type=error`).
  - New `POST /runs/new/named`: queue a `TestRun` for the Test
    identified by the submitted `test_id` (the row the user picked from
    the filtered table), via `create_test_run` (carry optional
    `git_ref`/`version`/pins). 404-style flash if the row is gone.
  - **Remove** [`run_new_matrix`](../../opp_ci/web/app.py#L656) from this
    module (logic moves to the matrix-run page below).
- [opp_ci/web/templates/run_new.html](../../opp_ci/web/templates/run_new.html)
  - Add a top "Run a named test" card: a **filterable table** of named
    tests — a name/project filter form above (like
    [matrices.html](../../opp_ci/web/templates/matrices.html#L11)),
    columns for name, project, kind, and the key coordinate fields so
    the user can tell rows apart, and a per-row **Run** button that
    posts `test_id` to `POST /runs/new/named`. Model the markup on
    [runs.html](../../opp_ci/web/templates/runs.html) /
    [matrices.html](../../opp_ci/web/templates/matrices.html).
  - Add a "Name (optional)" field to the existing Single Run form.
  - **Delete** the "Run from Matrix" card
    ([run_new.html:161-179](../../opp_ci/web/templates/run_new.html#L161)).

### Web — Run Matrices page (new)

- [opp_ci/web/app.py](../../opp_ci/web/app.py)
  - New `GET /matrix-runs/new` (`matrix_run_new_form`): same context
    builder as [`matrix_new_form`](../../opp_ci/web/app.py#L1034)
    (projects, OS/compiler/distro/flavor suggestions, versions-by-project)
    **plus** the list of named matrices (with optional `name`/`project`
    query-param filters) for the by-name **table**. Factor the shared
    context into a `_matrix_form_context(session)` helper so
    `matrix_new_form` and this handler can't drift.
  - New `POST /matrix-runs/new/named`: the relocated body of the old
    `run_new_matrix` — look up the `TestMatrix` by the submitted
    `matrix_id` (the picked table row), `create_matrix_run`,
    `expand_matrix`, `enqueue_job` per cell, redirect to
    `/matrix-runs/{id}` (instead of `/queue`).
  - New `POST /matrix-runs/new/anonymous`: same axis `Form(...)` fields
    as [`matrix_create`](../../opp_ci/web/app.py#L1129); build the config
    via the shared `_build_matrix_config_from_form` helper (extract from
    `matrix_create`), create the `TestMatrix` (name = the optional Name
    field or `NULL`) via `create_matrix_from_axes`, then `create_matrix_run`
    + expand + enqueue. Redirect to `/matrix-runs/{id}`.
- New template
  `opp_ci/web/templates/matrix_run_new.html`, structured to **mirror**
  `run_new.html`:
  - Card 1 "Run a named matrix": a **filterable table** of named
    matrices — a name/project filter above and columns for name,
    project, tests/kinds, modes, and refs (as in
    [matrices.html](../../opp_ci/web/templates/matrices.html#L18)), with
    a per-row **Run** button posting `matrix_id` to
    `POST /matrix-runs/new/named`.
  - Card 2 "Run an anonymous matrix": the axis inputs cloned from
    [matrix_new.html](../../opp_ci/web/templates/matrix_new.html) plus a
    "Name (optional)" field → `POST /matrix-runs/new/anonymous`.
  - Reuse the same project→version datalist `<script>` block.

### Web — TestMatrixRun index & detail parity

The matrix-run **index** ([matrix_runs.html](../../opp_ci/web/templates/matrix_runs.html))
and **detail** ([matrix_run_detail.html](../../opp_ci/web/templates/matrix_run_detail.html))
pages exist but are not structured like the TestRun pages. Bring them to
full parity with [runs.html](../../opp_ci/web/templates/runs.html) /
[run_detail.html](../../opp_ci/web/templates/run_detail.html):

- [opp_ci/web/app.py](../../opp_ci/web/app.py)
  - New `POST /matrix-runs/{matrix_run_id}/rerun`: load the
    `TestMatrixRun`, `create_matrix_run` against the same `matrix_id`
    (`trigger="rerun"`), re-`expand_matrix` + `enqueue_job` per cell —
    the same body as the named-matrix run handler, parameterised by the
    existing matrix. Redirect to the new `/matrix-runs/{id}`. Mirrors
    [`run_rerun`](../../opp_ci/web/app.py#L571).
  - New `POST /matrix-runs/{matrix_run_id}/cancel`: set every child
    `TestRun` still `queued` to `cancelled` with `finished_at=now`
    (`WHERE matrix_run_id=… AND lifecycle=queued`); leave running
    children to finish, matching the locked decision behind
    [`run_cancel`](../../opp_ci/web/app.py#L596). Refresh the matrix-run
    rollup counters afterward (reuse the rollup helper in
    [web/rollup.py](../../opp_ci/web/rollup.py)). Redirect to
    `/matrix-runs/{id}`.
  - **Route ordering:** register the literal `/matrix-runs/new` (and its
    `/named`,`/anonymous` POSTs) *before* the parameterised
    `/matrix-runs/{matrix_run_id}` detail route, and the
    `/{id}/rerun|cancel` POSTs alongside — otherwise FastAPI captures
    `"new"` as a `matrix_run_id`.
- [matrix_runs.html](../../opp_ci/web/templates/matrix_runs.html)
  - Header: add a `+ Run Matrix` button (→ `/matrix-runs/new`) styled
    like runs.html's `+ New Run`, using the same flex header layout
    ([runs.html:4-7](../../opp_ci/web/templates/runs.html#L4)).
  - Add an **Actions** column with **Re-run** and (when any child is
    queued) **Cancel** buttons, structurally identical to
    [runs.html:54-66](../../opp_ci/web/templates/runs.html#L54).
  - Render `m.display_name` (already covered above) so anonymous
    matrices read `(anonymous #id)`.
- [matrix_run_detail.html](../../opp_ci/web/templates/matrix_run_detail.html)
  - Add the same Re-run / Cancel actions to the detail header, mirroring
    [run_detail.html](../../opp_ci/web/templates/run_detail.html).

### Web — rename / edit name later

- [opp_ci/web/app.py](../../opp_ci/web/app.py)
  - `POST /tests/{test_id}/rename` → `set_test_name` (catch collision).
  - `POST /matrices/{matrix_id}/rename` → `set_matrix_name`.
- Templates:
  - [run_detail.html](../../opp_ci/web/templates/run_detail.html) and/or
    [expectations.html](../../opp_ci/web/templates/expectations.html):
    inline rename form for the underlying Test.
  - [matrix_detail.html](../../opp_ci/web/templates/matrix_detail.html):
    inline rename form.
  - [matrices.html](../../opp_ci/web/templates/matrices.html#L36),
    [matrix_runs.html](../../opp_ci/web/templates/matrix_runs.html),
    [matrix_run_detail.html](../../opp_ci/web/templates/matrix_run_detail.html):
    render `matrix.display_name` instead of bare `matrix.name` so
    anonymous matrices show `(anonymous #id)`.

### Web — navigation

- [base.html](../../opp_ci/web/templates/base.html#L104): the nav has
  `+ New Run`, `Matrices`, `Matrix runs`. Add a `+ Run Matrix` link to
  `/matrix-runs/new` next to `Matrix runs`, matching the `+ New Run`
  styling so the two run-actions look like siblings.

### CLI parity

- [opp_ci/cli.py](../../opp_ci/cli.py)
  - [`run`](../../opp_ci/cli.py#L114): add `--name` (set `Test.name`
    after `get_or_create_test`) and `--test NAME` (run an existing named
    test, skipping the coordinate flags). `--test` is mutually exclusive
    with `--project/--kind`.
  - [`run-matrix`](../../opp_ci/cli.py#L1083): stop calling
    `_generate_anonymous_matrix_name`; pass `name=None` for the anonymous
    path (add an optional `--name` to opt into saving). Delete the helper
    once unused.
  - Route the inline `TestMatrix(...)` construction through
    `create_matrix_from_axes`.

### REST parity

- [opp_ci/web/api.py](../../opp_ci/web/api.py)
  - [`SubmitRunRequest`](../../opp_ci/web/api.py#L54): add optional
    `name` and `test_name`. In [`submit_run`](../../opp_ci/web/api.py#L100),
    if `test_name` is set, run by name; else build the coordinate and set
    `name` if provided.
  - [`SubmitMatrixRequest`](../../opp_ci/web/api.py#L72): make
    `matrix_name` optional and add inline axis fields + optional `name`.
    In [`submit_matrix_run`](../../opp_ci/web/api.py#L155): named path
    unchanged; anonymous path builds via `create_matrix_from_axes`. (This
    also picks up the `"matrix": …` → `"matrix_name": …` response-key fix
    already flagged in
    [align-status-and-matrix-naming.md](./align-status-and-matrix-naming.md)
    — coordinate the two plans so the response shape is touched once.)

### Documentation

- [doc/web_ui.md](../../doc/web_ui.md) — document the two parallel run
  pages and the rename affordances.
- [doc/concepts.md](../../doc/concepts.md) — "named vs anonymous"
  definition for both Test and TestMatrix; note tests are deduped by
  coordinate, name is a separate editable label.
- [doc/cli_reference.md](../../doc/cli_reference.md) — `run --name` /
  `run --test`; `run-matrix` anonymous now NULL-named.
- [doc/rest_api.md](../../doc/rest_api.md) — the two extended request
  bodies.
- [doc/data_model.md](../../doc/data_model.md) — `TestMatrix.name`
  nullability, `Test.name` partial-uniqueness.

## Migration sequence

Each step is one commit. The helper/schema steps land before their
callers.

1. **Schema + migration.** Make `TestMatrix.name` nullable, add the
   partial unique index on `tests.name`, add `TestMatrix.display_name`.
   Alembic up/down + the duplicate-name pre-check. No behaviour change
   yet.
2. **Persistence helpers.** Add `get_test_by_name`, `get_matrix_by_name`,
   `set_test_name`, `set_matrix_name`, `create_matrix_from_axes`,
   `_build_matrix_config_from_form` (or its non-web equivalent). Unit
   tests for name collision (`ValueError`) and blank→NULL.
3. **Run Tests page.** Add named-test dropdown + `POST /runs/new/named`,
   add the optional Name field to the Single Run form, remove the
   Run-from-Matrix card and `run_new_matrix`. (Temporarily, named-matrix
   running is unavailable in the web between this step and step 4 — land
   3 and 4 together or in immediate succession.)
4. **Run Matrices page.** New `GET /matrix-runs/new`, the named and
   anonymous POST handlers, `matrix_run_new.html`, nav link. Restores and
   extends the matrix-running capability removed in step 3. Mind the
   literal-before-parameterised route ordering vs.
   `/matrix-runs/{matrix_run_id}`.
5. **TestMatrixRun index & detail parity.** Add
   `POST /matrix-runs/{id}/rerun` and `/cancel`; add the `+ Run Matrix`
   header button and the Re-run/Cancel Actions column to
   `matrix_runs.html`; add the same actions to `matrix_run_detail.html`.
6. **Rename affordances.** `POST /tests/{id}/rename`,
   `POST /matrices/{id}/rename`, template forms, `display_name` rendering
   across matrix templates.
7. **CLI parity.** `run --name` / `run --test`; `run-matrix` NULL-named
   anonymous; drop `_generate_anonymous_matrix_name`; route through
   `create_matrix_from_axes`.
8. **REST parity.** Extend `SubmitRunRequest` / `SubmitMatrixRequest` and
   their handlers. Coordinate the matrix response-key rename with
   [align-status-and-matrix-naming.md](./align-status-and-matrix-naming.md).
9. **Docs sweep.** `web_ui.md`, `concepts.md`, `cli_reference.md`,
   `rest_api.md`, `data_model.md`.

Steps 1, 2, 9 are non-breaking. Steps 3+4 are a paired UI change. Steps
7, 8 change CLI/REST surfaces (see Risks).

## Verification

- `alembic upgrade head` then `downgrade` round-trips cleanly on both
  SQLite and Postgres; the duplicate-name pre-check is exercised by a
  test that seeds two same-named tests and asserts the migration aborts.
- `pytest` passes; new helper tests cover name collision and blank→NULL
  for both Test and TestMatrix.
- Manual web — Run Tests page:
  - Anonymous run with a Name → a `Test` is created/found, `name` set,
    one `TestRun` queued; re-submitting the same coordinates with no name
    reuses the same `Test` (dedup) and does not clear the name.
  - Anonymous run with a name already taken by a *different* coordinate →
    friendly error, nothing queued.
  - Named-test table: filter by name/project, read each row's
    coordinates, click **Run** on a row → new `TestRun` on that `Test`.
  - The Run-from-Matrix card is gone.
- Manual web — Run Matrices page:
  - Named matrix (filtered from the table, config visible per row) →
    **Run** → `TestMatrixRun` + N queued jobs → redirect to
    `/matrix-runs/{id}`.
  - Anonymous matrix with axes, no name → `TestMatrix` row with
    `name IS NULL`, runs; it renders as `(anonymous #id)` in
    `/matrices` and `/matrix-runs`.
  - Anonymous matrix with a Name → reusable named matrix appears in the
    named table afterwards.
- Manual web — TestMatrixRun pages mirror the TestRun pages:
  - `/matrix-runs` has a `+ Run Matrix` header button and an Actions
    column; **Re-run** spawns a fresh `TestMatrixRun` from the same
    matrix and redirects to it; **Cancel** flips only the still-`queued`
    child runs to `cancelled` (running children keep going) and the
    rollup counters update.
  - `/matrix-runs/{id}` shows the same Re-run / Cancel actions.
  - `/matrix-runs/new` resolves to the run form (not the detail route)
    despite sharing the `/matrix-runs/…` prefix.
- Manual web — rename: rename a Test from `run_detail`, rename a matrix
  from `matrix_detail`; collision shows an error; blank clears the name.
- CLI: `opp_ci run --project inet --kind smoke --name my-smoke` then
  `opp_ci run --test my-smoke` reuses the same `Test`. `opp_ci run-matrix
  --project inet --kinds smoke` creates a `NULL`-named matrix (no
  `adhoc:` row). `grep -n _generate_anonymous_matrix_name opp_ci/`
  returns nothing.
- REST: `POST /api/runs/matrix` with an inline body (no `matrix_name`)
  queues an anonymous matrix; with `matrix_name` still works.
- The two run pages are visually parallel (same layout: a filterable
  "by name" **table** above an "anonymous" form with an optional Name
  field).

## Risks & notes

- **Steps 3 and 4 must ship together.** Step 3 removes web matrix-running
  before step 4 re-adds it. Land them back-to-back (or squash) so no
  deploy sits in the gap.
- **`TestMatrix.name` nullability touches every `matrix.name` render.**
  Any template or serializer that assumes a non-null name will print an
  empty cell for anonymous matrices unless switched to `display_name`.
  Grep `matrix.name` / `m.name` / `matrix_rel.name`
  ([api.py:737](../../opp_ci/web/api.py#L737),
  [matrices.html:36](../../opp_ci/web/templates/matrices.html#L36)) and
  audit each.
- **`Test.name` partial-unique index** behaves differently across
  engines for the `WHERE name IS NOT NULL` clause (`sqlite_where` vs.
  `postgresql_where`). Test the migration on both; this codebase runs
  SQLite in dev and Postgres in prod.
- **Existing `adhoc:%` matrix names stay.** They remain valid named
  matrices and will still show their synthetic name. Only matrices
  created *after* this change are NULL-named. If that inconsistency is
  undesirable, the optional backfill `UPDATE … SET name = NULL WHERE name
  LIKE 'adhoc:%'` can be enabled — but it would orphan any AutoTestRule
  or docs that reference those names, so it's off by default.
- **REST `SubmitMatrixRequest` becomes a union shape.** Clients sending
  the old `{matrix_name}` body are unaffected; the field just stops being
  required. Document the inline-axes alternative. Coordinate the
  `"matrix"`→`"matrix_name"` response-key rename with the existing
  [align-status-and-matrix-naming.md](./align-status-and-matrix-naming.md)
  plan so the wire format is broken exactly once.
- **`run --test` vs. `run --project/--kind` mutual exclusion.** Decide
  the precedence and error message up front; reuse the same
  "pick exactly one of…" pattern already in
  [run-matrix](../../opp_ci/cli.py#L1147).
- **`/matrix-runs/new` vs. `/matrix-runs/{matrix_run_id}` route order.**
  FastAPI matches in declaration order; the literal `new` (and
  `{id}/rerun`, `{id}/cancel`) routes must be declared before the
  `{matrix_run_id}` detail route or `"new"` is parsed as an id and 404s.
- **Matrix Cancel must refresh the rollup.** Flipping queued child runs
  to `cancelled` outside the normal finalize path will desync the
  `TestMatrixRun` counters unless the rollup is recomputed (reuse
  [web/rollup.py](../../opp_ci/web/rollup.py)); otherwise `total_count`
  keeps counting cells that will never run.
- **Out of scope:** merging `/matrices/new` (save-without-run) into the
  run page; per-run naming of `TestRun` / `TestMatrixRun` rows (only the
  *definitions* — Test and TestMatrix — are nameable here); any change to
  `coord_hash` or the dedup semantics.
