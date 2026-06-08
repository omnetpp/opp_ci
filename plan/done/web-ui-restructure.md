# Web UI restructure — definition/execution split

## Goal

Make the **definition ↔ execution** pairs in the data model first-class in
the web UI, each with its own list, filter, and (for definitions) detail
page. Rebuild the navbar around them.

Today the UI hides the "definition" half: there is no Tests list and no
Test detail page — Tests only surface as a "run by name" table buried
inside `/runs/new`, and `/runs/new` conflates two unrelated jobs ("browse
existing Tests and run one" vs. "define a brand-new coordinate and run it
in one shot"). The model is already two clean pairs:

| | Definition | Execution |
|---|---|---|
| **Single** | `Test` (coordinate) | `TestRun` (one execution + outcome) |
| **Matrix** | `TestMatrix` (axes) | `TestMatrixRun` (execution + rollup verdict) |

This plan promotes all four to top-level, gives both **definition** types
(`Test`, `TestMatrix`) a symmetric **detail + rename** page, and splits the
two dual-mode `/...new` pages into a clean "list (with Run buttons)" +
"create (form)" pair.

## Locked decisions

- **Anonymous tests in `/tests`** — named Tests by default; an
  `?include_anonymous=1` toggle pulls in the matrix-cell tests. Anonymous
  rows render their coordinate as the label.
- **"New Test" / "New Matrix Test" submit** — primary **Save** button
  (saves the named definition), secondary **Save & run** button (saves +
  queues a run). Save & run with a blank name preserves today's one-shot
  anonymous path.
- **Admin** — kept as a top-level nav item (admins only), appended after
  Workers.
- **Routes are renamed** to match the nav labels. **No backward-compat
  aliases** for the old paths — old URLs simply 404.
- **Test Matrix gets a detail page symmetric with Test**; rename is
  available on both detail pages (both already have a rename handler).

## Navbar (final order)

Rebuild `<nav>` in [base.html](../../opp_ci/web/templates/base.html) to:

```
Dashboard · Results · Queue · Tests · Test Runs · Test Matrices ·
Test Matrix Runs · Projects · Compatibility · Rules · OSes · Compilers ·
Workers · Admin*
```

`Admin` shown only when `current_user.role == "admin"`. Drop the green
`+ New Run` / `+ Run Matrix` shortcut links — creation now happens via
`+ New` buttons on the list pages.

## Route map (old → new)

`/api/*` JSON routes in [api.py](../../opp_ci/web/api.py) are a separate
namespace and are **out of scope** — they keep `/api/runs`, `/api/matrices`,
etc. unchanged. Only the HTML `web_router` routes below change.

### Tests (single definition) — `/tests`

| New route | Replaces | Notes |
|---|---|---|
| `GET /tests` | *(none — new)* | List + filter. Named-only by default; `?include_anonymous=1`. Per-row **Run** button. Filters: project / kind / os / compiler / last-run status. |
| `GET /tests/new` | `GET /runs/new` (form half) | Coordinate form lifted out of `run_new.html`. |
| `POST /tests/new` | `POST /runs/new` | **Save** (get-or-create + `set_test_name`) and **Save & run** (also `create_test_run` + enqueue). |
| `GET /tests/{id}` | *(none — new)* | Detail: coordinate, run history, expectation editor/history (folds in `/tests/{id}/expectations`), Run button, rename form. |
| `POST /tests/{id}/run` | `POST /runs/new/named` | Queue a fresh `TestRun` for an existing Test. |
| `POST /tests/{id}/rename` | *(unchanged path)* | Already exists. |
| `POST /tests/{id}/expectations` | *(unchanged path)* | Already exists; history display moves into `/tests/{id}`. |
| `GET /tests/{id}/expectations` | *(remove)* | Folded into `/tests/{id}`; drop the standalone page (or keep as a thin redirect — **not** kept, per no-compat). |

### Test Runs (single execution) — `/test-runs`

| New route | Replaces |
|---|---|
| `GET /test-runs` | `GET /runs` |
| `GET /test-runs/{id}` | `GET /runs/{id}` |
| `POST /test-runs/{id}/rerun` | `POST /runs/{id}/rerun` |
| `POST /test-runs/{id}/cancel` | `POST /runs/{id}/cancel` |

### Test Matrices (matrix definition) — `/test-matrices`

| New route | Replaces | Notes |
|---|---|---|
| `GET /test-matrices` | `GET /matrices` | List + filter. Per-row **Run** button (new). |
| `GET /test-matrices/new` | `GET /matrices/new` | Axis form only (drop the "run by name" half). |
| `POST /test-matrices/create` | `POST /matrices/create` + `POST /matrix-runs/new/anonymous` | **Save** and **Save & run** (Save & run = `create_matrix_from_axes` + `_queue_matrix_run`). |
| `GET /test-matrices/{id}` | `GET /matrices/{id}` | Detail (already exists) — symmetric with Test detail. |
| `POST /test-matrices/{id}/run` | `POST /matrix-runs/new/named` | Queue a `TestMatrixRun` for a saved matrix. |
| `POST /test-matrices/{id}/rename` | `POST /matrices/{id}/rename` | |
| `POST /test-matrices/{id}/delete` | `POST /matrices/{id}/delete` | |

### Test Matrix Runs (matrix execution) — `/test-matrix-runs`

| New route | Replaces |
|---|---|
| `GET /test-matrix-runs` | `GET /matrix-runs` |
| `GET /test-matrix-runs/{id}` | `GET /matrix-runs/{id}` |
| `POST /test-matrix-runs/{id}/rerun` | `POST /matrix-runs/{id}/rerun` |
| `POST /test-matrix-runs/{id}/cancel` | `POST /matrix-runs/{id}/cancel` |

The old `GET /matrix-runs/new` form page goes away entirely: its "run by
name" table becomes the `/test-matrices` list (Run buttons), and its
anonymous axis form becomes `/test-matrices/new` (Save & run).

### Unchanged

`/`, `/queue`, `/results`, `/projects*`, `/commits/*`, `/compatibility*`,
`/rules*`, `/os*` (label "OSes"), `/compilers*`, `/workers`, `/admin*`.

## Symmetry achieved

```
Tests          /tests           list · new · {id} · {id}/run · {id}/rename · {id}/expectations
Test Runs      /test-runs       list · {id} · {id}/rerun · {id}/cancel
Test Matrices  /test-matrices   list · new · create · {id} · {id}/run · {id}/rename · {id}/delete
Matrix Runs    /test-matrix-runs list · {id} · {id}/rerun · {id}/cancel
```

Both definition detail pages (`/tests/{id}`, `/test-matrices/{id}`) carry:
coordinate/axes summary · run history · Run button · rename form
(· expectations, Tests only).

## Implementation steps

1. **Routes** — rename handlers in
   [app.py](../../opp_ci/web/app.py) per the map above and update every
   `RedirectResponse(url=...)` target (8 internal redirects reference the
   old paths). Order constraint preserved: literal `/test-matrix-runs/...`
   stays declared before any parametric `{id}` route.

2. **New Tests column** (the only genuinely new code):
   - `GET /tests` + `tests.html` — reuse the named-test query already in
     `run_new_form` ([app.py:380](../../opp_ci/web/app.py#L380)); add
     `include_anonymous` and the status/os/compiler filters.
   - `GET/POST /tests/new` + reuse/rename `run_new.html` as `test_new.html`
     — strip the by-name table, add the Save / Save & run buttons.
   - `GET /tests/{id}` + `test_detail.html` — mirror `matrix_detail.html`;
     pull in the expectations history from `expectations.html`.
   - `POST /tests/{id}/run` — body = old `run_new_named`.

3. **Test Matrices cleanup**:
   - Add Run buttons to `matrices.html` rows → `POST /test-matrices/{id}/run`.
   - `matrix_new.html`: drop the named-matrix table; add Save / Save & run.
   - Retire `matrix_run_new.html` and the `GET /matrix-runs/new` route.
   - `matrix_detail.html`: keep rename + Run; ensure parity with Test detail.

4. **Templates** — rewrite all hardcoded `href`/`action` paths
   (`/runs`, `/matrices`, `/matrix-runs` → new paths) across:
   `base.html`, `runs.html`, `run_new.html`, `run_detail.html`,
   `results.html`, `queue.html`, `dashboard.html`, `commit_detail.html`,
   `project_detail.html`, `matrices.html`, `matrix_new.html`,
   `matrix_detail.html`, `matrix_runs.html`, `matrix_run_new.html`,
   `matrix_run_detail.html`, `rules.html`. (`matrix_detail.html:147`
   also references a stale `/runs/new/matrix` action — drop/fix it.)
   Consider renaming the template files to match
   (`runs.html`→`test_runs.html`, etc.) for clarity.

5. **Docs** — rewrite the page map in
   [web_ui.md](../../opp_ci/doc/web_ui.md) and fix any path references in
   [rest_api.md](../../opp_ci/doc/rest_api.md) / other docs that point at
   the HTML routes.

6. **Tests** — update `tests/` that hit the web routes
   (`test_run_by_name.py`, `test_remote_cli.py`, any UI smoke tests) to the
   new paths; add coverage for `/tests` list (named-only vs.
   include-anonymous) and the Save vs. Save & run branches.

## Open / deferred

- Whether `/tests` filtering should later reuse the `/results`
  multi-dimension machinery ([rollup.py](../../opp_ci/web/rollup.py)) —
  start with simple column filters, revisit if needed.
- Backward-compat redirect aliases for old paths — explicitly **not** in
  this pass.
</content>
</invoke>
