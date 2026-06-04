# Plan: rename `test_type` / `test_types` → `test` / `tests`

Goal: collapse the noisy `test_type` / `test_types` naming throughout
opp_ci into the shorter `test` / `tests`. The user-facing CLI flag is
already `--test` / `--tests`; only the *destinations* downstream of
that flag (DB column, JSON keys, Python attributes / params, REST
fields, HTML form fields, template variables, doc anchors) still
carry the longer name. This rename closes that gap so every surface
of the system says the same thing.

This is a pure rename — no behavior changes, no schema additions, no
new tests. The migration is the only piece that has to think about
backward compatibility (existing `test_matrices.config` rows in the
DB carry a `"test_types"` JSON key that needs rewriting in place,
alongside the column rename).

## Renaming table

| Surface | Before | After |
|---|---|---|
| DB column | `test_runs.test_type` | `test_runs.test` |
| Matrix `config` JSON key | `"test_types": [...]` | `"tests": [...]` |
| SQLAlchemy attribute | `TestRun.test_type` | `TestRun.test` |
| REST request / response field | `test_type` | `test` |
| REST list-query param | `?test_type=smoke` | `?test=smoke` |
| HTML form field (`name=`) | `test_type` / `test_types` | `test` / `tests` |
| HTML datalist id | `test-type-options` | `test-options` |
| Jinja template variable | `filter_test_type` | `filter_test` |
| Python function param / kwarg | `test_type=`, `test_types=` | `test=`, `tests=` |
| Python local variables | `test_type`, `test_types` | `test`, `tests` |
| Click destination | `"test_type"`, `"test_types"` | `"test"`, `"tests"` |
| Internal CLI flag | `--test-type` (in `internal_run_direct`) | `--test` |
| Doc anchor | `#axis-test-types`, `#test-type` | `#axis-test`, `#test` |
| Doc heading | `## Axis: test types` | `## Axis: test` |
| Job dict key (scheduler / executor / webhook) | `"test_type"` | `"test"` |
| log/error strings ("Unknown test type: …") | unchanged — these refer to the *concept* "test type", not the field name |

The two user-facing CLI flags `--test` and `--tests` keep their
current spelling — only their internal Click destinations change.
No CLI invocation breaks.

## Files to touch

Counts come from `grep -rn 'test_type\|test_types\|test-type\|test-types'`
in the working tree (193 hits). Grouped here so the rename can be
done in coherent batches.

### Database & migration (do first)

- [opp_ci/db/models.py](../opp_ci/db/models.py) — `TestRun.test_type`
  column + `__repr__`.
- New migration
  `opp_ci/db/migrations/versions/<rev>_rename_test_type_to_test.py`,
  `down_revision = "c5d8a4f12b30"` (the current head). Must:
  1. `op.alter_column("test_runs", "test_type", new_column_name="test")`.
  2. For every row in `test_matrices`, rewrite the `config` JSON in
     place: rename the key `test_types` → `tests`. Reuse the
     iterate-and-update shape from
     [9f1c4d2a8e10_rename_docker_to_podman.py](../opp_ci/db/migrations/versions/9f1c4d2a8e10_rename_docker_to_podman.py)
     (`bind.execute(sa.select(...))` → mutate dict → `bind.execute(sa.update(...))`).
  3. `downgrade()` does the inverse: column rename back, and rewrite
     `tests` → `test_types` in matrix configs.

### Python source

- [opp_ci/cli.py](../opp_ci/cli.py) — every `test_type` / `test_types`
  occurrence, Click destinations, loop variables, f-strings, the
  `internal_run_direct` `--test-type` flag.
- [opp_ci/executor.py](../opp_ci/executor.py) — `find_existing_run`
  kwarg, `run_test` / `_run_test_*` params, `COMMAND_MAP` lookups,
  the `--test-type` subprocess arg at the
  podman-fallback call site (becomes `--test`).
- [opp_ci/scheduler.py](../opp_ci/scheduler.py) — default-config
  `test_types` key, axis-tuple unpacking, job-dict `"test_type"` keys.
- [opp_ci/worker.py](../opp_ci/worker.py) — job-dict reads, `run_test`
  kwarg.
- [opp_ci/notes.py](../opp_ci/notes.py) — `run.test_type` reference.
- [opp_ci/client.py](../opp_ci/client.py) — `submit_run` / `list_runs`
  param names + payload keys + docstring example.
- [opp_ci/opp_env_adapter.py](../opp_ci/opp_env_adapter.py) — default
  matrix config `"test_types"`.
- [opp_ci/github/status.py](../opp_ci/github/status.py) — markdown
  table cell.
- [opp_ci/github/webhook.py](../opp_ci/github/webhook.py) — job dict
  construction + `submit_run` kwargs.
- [opp_ci/web/api.py](../opp_ci/web/api.py) — Pydantic request/response
  models, query-param names, ORM attribute reads, response-dict keys.
- [opp_ci/web/app.py](../opp_ci/web/app.py) — query params, ORM filter
  clauses, template-context keys (`filter_test_type`), the form-POST
  handler for matrices (`test_types` form field).
- [opp_ci/web/rollup.py](../opp_ci/web/rollup.py) — primary-dimensions
  list + module docstring.

### Jinja templates

- [opp_ci/web/templates/results.html](../opp_ci/web/templates/results.html)
  — filter input `name="test_type"`, `filter_test_type`, primary-dim
  list literal, row cell.
- [opp_ci/web/templates/runs.html](../opp_ci/web/templates/runs.html) —
  filter input, row cell.
- [opp_ci/web/templates/queue.html](../opp_ci/web/templates/queue.html) —
  row cells (2).
- [opp_ci/web/templates/compare.html](../opp_ci/web/templates/compare.html) —
  filter input + `filter_test_type`.
- [opp_ci/web/templates/dashboard.html](../opp_ci/web/templates/dashboard.html) —
  row cell.
- [opp_ci/web/templates/admin.html](../opp_ci/web/templates/admin.html) —
  row cell.
- [opp_ci/web/templates/project_detail.html](../opp_ci/web/templates/project_detail.html) —
  row cell.
- [opp_ci/web/templates/commit_detail.html](../opp_ci/web/templates/commit_detail.html) —
  row cell.
- [opp_ci/web/templates/matrices.html](../opp_ci/web/templates/matrices.html) —
  `m.config.get("test_types", …)`.
- [opp_ci/web/templates/matrix_detail.html](../opp_ci/web/templates/matrix_detail.html) —
  `matrix.config.get("test_types", …)`, job cell, run cell.
- [opp_ci/web/templates/matrix_new.html](../opp_ci/web/templates/matrix_new.html) —
  `<label for="test_types">`, `<input name="test_types" id="test_types" list="test-type-options">`,
  `<datalist id="test-type-options">`.
- [opp_ci/web/templates/run_new.html](../opp_ci/web/templates/run_new.html) —
  `<label for="test_type">`, `<select name="test_type" id="test_type">`.
- [opp_ci/web/templates/run_detail.html](../opp_ci/web/templates/run_detail.html) —
  `run.test_type`.

The visible labels (`<th>Test</th>`, `<label>Test</label>`,
`placeholder="Test"`) are already singular and don't change.

### Documentation

- [doc/concepts.md](../doc/concepts.md) — `test_type` / `test_types`
  references, anchor `#test-type`, link targets `#axis-test-types`.
- [doc/data_model.md](../doc/data_model.md) — TestRun column table.
- [doc/test_matrix_dimensions.md](../doc/test_matrix_dimensions.md) —
  heading "Axis: test types" → "Axis: test"; the JSON-key row in the
  aspect table; every example snippet that uses `test_types: [...]`;
  intra-doc anchor links.
- [doc/single_test_parameters.md](../doc/single_test_parameters.md) —
  parameter name in tables, code samples, the "TestRun column" cell
  ("`test_type`" → "`test`").
- [doc/rest_api.md](../doc/rest_api.md) — endpoint description, curl
  body example.
- [doc/python_client.md](../doc/python_client.md) — code samples.
- [doc/cli_reference.md](../doc/cli_reference.md) — anchor reference.
- [doc/getting_started.md](../doc/getting_started.md) — anchor
  reference, the inline `sqlite3 … SELECT … test_type … FROM test_runs`
  example becomes `SELECT … test … FROM test_runs`.
- [doc/git_notes.md](../doc/git_notes.md) — the `<test_type>` placeholder
  in the format template.
- [plan/pending/project-test-automation.md](./project-test-automation.md) —
  references to `test_type` in tuples, JSON snippets, and the
  `--test-types` CLI flag in older draft examples. This plan is
  pending; update it so future work doesn't get rebased onto the old
  spelling.

### Out of scope

- `build/lib/opp_ci/*` — regenerated by the package build, do not edit
  by hand.
- `plan/done/*` — historical, leave intact.
- The string "test type" in prose (e.g. `"Unknown test type: …"` error
  messages, "test types" in tutorial prose) — these refer to the
  concept, not the field name, and reading them as
  "the test value `smoke`" is awkward. Keep prose as-is; only change
  identifiers, JSON keys, anchors, and code-block samples.

## Migration sequence

A clean order that keeps the working tree runnable at every step:

1. **Models + migration.** Rename the column on `TestRun`, write the
   Alembic revision, run `alembic upgrade head` against a copy of a
   real DB (or the local dev DB) to confirm the column rename and the
   matrix-config rewrite both land.
2. **Python source.** Rename every Python identifier in one sweep —
   `cli.py`, `executor.py`, `scheduler.py`, `worker.py`, `notes.py`,
   `client.py`, `opp_env_adapter.py`, `github/*.py`, `web/api.py`,
   `web/app.py`, `web/rollup.py`. After this step the CLI and REST
   API both speak the new names.
3. **Templates.** Form fields, template-context keys, `<datalist>` ids.
   The template-context keys (`filter_test_type` → `filter_test`)
   must change in lockstep with the corresponding `web/app.py` and
   `web/api.py` edits — don't split those across steps.
4. **Docs.** Anchors, JSON keys in examples, code samples, the
   `## Axis: test types` heading. Re-grep after to confirm zero stale
   anchor links remain (`grep -rn '#axis-test-types\|#test-type' doc/`).
5. **`project-test-automation.md`.** Sweep the pending plan so it's
   already using the new names when work on it resumes.

Each step is a separate commit. Step 1 must be deployed before steps
2–3 take effect (column needs to exist before code reads
`TestRun.test`).

## Verification

- `grep -rn 'test_type\|test_types\|test-type\|test-types' opp_ci/ doc/ alembic.ini` returns
  only:
  - intentional prose hits in error/log strings ("Unknown test type"),
  - tutorial / concept prose using "test types" as a noun phrase,
  - the new Alembic migration file (which references the old names
    in its docstring and inside the rename calls).
  Anything else is a missed rename.
- `pytest` (or whatever test target the repo uses) passes — every
  code path that touches a TestRun is in the rename, so import errors
  / attribute errors surface immediately.
- `alembic upgrade head` then `alembic downgrade -1` on a snapshot DB
  round-trips cleanly: column name flips back, matrix configs flip
  back.
- Manual sweep of the web UI: load `/runs`, `/results`, `/queue`,
  `/compare`, `/matrices`, `/matrices/<id>`, `/matrices/new`,
  `/runs/new`, `/runs/<id>`. Confirm the filter inputs still filter,
  the matrix-creation form still posts and creates a matrix, the
  Test column on every list page still renders, the matrix-detail
  "Tests" row still shows the configured tests.
- `curl -H 'Authorization: Bearer …' /api/runs?test=smoke` returns
  the expected filtered list; `?test_type=smoke` is ignored (this is
  the intended break — REST callers must update).
- `opp_ci run --test smoke --project inet` still submits a run; CLI
  output displays the test name in the slash-separated descriptor.

## Risks & notes

- **REST/CLI clients break.** External scripts hitting `/api/runs`
  with `?test_type=…`, or sending `{"test_type": …}` in POST bodies,
  will silently drop the filter / fail validation after this change.
  No back-compat shim is planned — call this out in release notes.
- **Matrix-config keys in user-authored JSON.** If users keep matrix
  JSON files outside the DB (e.g. checked-in fixtures, shell scripts
  that POST a matrix config), they must update `test_types` → `tests`.
  The DB migration covers stored rows only.
- **Generic name `test`.** `run.test == "smoke"` reads fine; the param
  `test=` is unambiguous in the call sites we have. The only place
  worth a second look is `for test in tests.split(",")` loops — the
  loop var shadows the parameter, which is the same pattern the
  current code already uses (`for test_type in test_types.split(",")`),
  just shorter.
- **Doc anchors.** External pages or bookmarks linking to
  `#axis-test-types` will 404 inside the page. Acceptable for an
  internal-tool doc set; note in release notes.
