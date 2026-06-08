# Compatibility matrix — dimension filters

## Goal

Extend the project compatibility matrix page (`/compatibility/{project}`)
so the empirical pass/fail overlay can be **filtered by the execution
dimensions** that the current page collapses away: OS / distro / flavor,
compiler, build mode / kind, and toolchain / isolation / arch.

Scope decided during brainstorm (2026-06-08):

- **Filters only** — no new visual encoding (shapes/sizes/multi-channel
  glyphs). The cell stays a single status dot, exactly as today.
- **Server-side reload** — reuse the existing query-param + dropdown
  pattern from the Runs/Tests pages (`_distinct_options`,
  `_filters.html` macros). No client-side JavaScript is introduced.
- **All dimensions** in scope as filters: `os`, `os_version`, `distro`,
  `distro_version`, `flavor`, `flavor_version`, `compiler`,
  `compiler_version`, `mode`, `kind`, `toolchain`, `isolation`, `arch`.

Deliberately deferred (but the data layer is built so these need **no
rework** later): faceted cells (sub-dots per dimension value),
multi-channel glyph encoding, and dynamic pivot axes. See
[Future work](#future-work).

## The core problem this fixes

[compatibility.py](../../opp_ci/compatibility.py) keys the empirical
overlay only on `(version_label, dep_name, dep_version)`
([`_collect_test_overlays`](../../opp_ci/compatibility.py#L144)) and then
collapses every matching run into one status via
[`_aggregate_status`](../../opp_ci/compatibility.py#L213). All of
`os / distro / flavor / compiler / mode / kind / toolchain / isolation /
arch` (which live on the `Test` row, [models.py:264](../../opp_ci/db/models.py#L264))
are thrown away. A `mixed` cell that is really "PASS on Ubuntu+gcc, FAIL
on Fedora+clang" loses exactly the information we now want to filter by.

The foundational change is therefore: **stop collapsing in the data
layer** — capture each contributing run *with its dimensions*, filter
that list, then aggregate what survives.

## Semantics to nail down

- **Filters subset the empirical overlay only.** Declared compatibility
  (`Version.resolved_dependencies`) carries no OS/compiler, so a filter
  can never remove a `compatible` cell. It can only revert a tested cell
  back toward `compatible`: if `os=Windows` is selected and a cell is
  declared-compatible + PASS-on-Linux with *no* Windows run, the cell
  shows `compatible` ("declared, not tested on Windows") — not PASS.
  This is correct and worth a code comment + a line in the legend area.
- **Filter option lists are scoped to the project**, not global. Offer
  only dimension values that actually occur among the runs contributing
  to *this* project's matrices, so the dropdowns never list a compiler
  that was never used here.
- **Empty filter = today's behavior**, byte-for-byte. No filter params →
  identical aggregation and rendering as the current page.

## Implementation

### 1. Data layer — `opp_ci/compatibility.py`

Un-collapse the overlay and thread filters through.

- `_collect_test_overlays(session, project_name, versions)`: change the
  dict **value** from `[result_code, ...]` to a list of per-run dicts:
  ```python
  {
      "result_code": run.result_code,
      "run_id": run.id,
      "finished_at": run.finished_at,
      "os": run.test.os, "os_version": run.test.os_version,
      "distro": run.test.distro, "distro_version": run.test.distro_version,
      "flavor": run.test.flavor, "flavor_version": run.test.flavor_version,
      "compiler": run.test.compiler, "compiler_version": run.test.compiler_version,
      "mode": run.test.mode, "kind": run.test.kind,
      "toolchain": run.test.toolchain, "isolation": run.test.isolation,
      "arch": run.test.arch,
  }
  ```
  (Join already loads `Test`; access via the relationship or select the
  columns.) Keep both overlay-population branches (`run.resolved_deps`
  vs the version's declared single-pin deps).

- New `_DIMENSIONS` tuple listing the 13 dimension keys above (single
  source of truth used by filtering, option-collection, and the route).

- `_build_declared_matrix(versions, dep_name, test_overlays, filters)`:
  before calling `_aggregate_status`, filter the per-run list to runs
  matching every active `filters[dim]`. Aggregate over the surviving
  `result_code`s. If none survive but the cell was declared → `compatible`.
  Optionally attach the surviving run dicts to the cell for tooltips
  (see step 3): make a cell a small dict `{"status": str, "runs": [...]}`
  instead of a bare string. (Update the template's if/elif chain to read
  `cell.status`.)

- `get_compatibility_matrix(session, project_name, filters=None)`: add
  the optional `filters` dict. Return a dict instead of a bare list so
  the route gets the scoped option lists too:
  ```python
  {"matrices": [...], "options": {dim: [sorted distinct values], ...}}
  ```
  Compute `options` from the dimension values seen across all collected
  overlay runs for the project. Update the docstring's documented return
  shape. **This is the only signature change** — single caller, below.

### 2. Web route — `opp_ci/web/app.py` (`compatibility_page`, ~L1072)

Mirror [`runs_list`](../../opp_ci/web/app.py#L249):

- Add a `Query(default=None)` param per dimension (`os` keeps
  `alias="os"` like the Runs route).
- Build `filters = {dim: <param> for dim in _DIMENSIONS if <param>}`.
- Call `result = get_compatibility_matrix(session, project_name, filters)`.
- Pass to the template: `matrices=result["matrices"]`,
  `options=result["options"]`, and a `filters` dict of
  `{dim: value or ""}` (so the dropdowns render the current selection),
  matching the `_filters.html` macro contract.

### 3. Template — `opp_ci/web/templates/compatibility.html`

- Add a filter `<form method="get">` above the matrices, using the
  shared macros: `{% import "_filters.html" as f %}` then one
  `{{ f.sel(filters, "<dim>", "<Label>", options.<dim>) }}` per
  dimension, plus a `Filter` submit button and a `Clear` link to the
  bare project URL (copy the `filter-actions` block from `runs.html`).
  Group the platform-hierarchy selects together for readability.
- Update the cell rendering to read `cell.status` (was the bare `cell`
  string) in the if/elif chain.
- Enrich the dot `title=` to show the per-run breakdown when
  `cell.runs` has entries (e.g. `"PASS — ubuntu/gcc-14 (release); FAIL —
  fedora/clang-18 (release)"`). This surfaces the dimension data on
  hover even without visual encoding — a cheap down-payment on the
  deferred faceting work.
- Add a one-line note near the legend explaining that filters narrow the
  *tested* overlay and that `compatible` means "declared, no matching
  test run."

No CSS change strictly required (filter styles already exist in
`base.html` / the `_filters.html` macros). The matrix `.compat-matrix`
styling is untouched.

### 4. Tests — `tests/test_compatibility.py` (new)

There is currently no compatibility test. Add one:

- Seed a `Project` with `dependency_names`, a couple of `Version` rows
  with `resolved_dependencies`, and several finished `TestRun`s across
  different `os`/`compiler`/`mode` for the same `(version, dep_version)`
  cell with differing `result_code`s.
- Assert: no filter → cell aggregates to `mixed`; `os=<one>` filter →
  cell collapses to that run's status; filter with no matching run on a
  declared cell → `compatible`; `options` lists only the project's
  dimension values.

## Files touched

| File | Change |
|---|---|
| [opp_ci/compatibility.py](../../opp_ci/compatibility.py) | un-collapse overlay, add `filters` + scoped `options`, dict return |
| [opp_ci/web/app.py](../../opp_ci/web/app.py) | dimension query params on `compatibility_page`, pass options/filters |
| [opp_ci/web/templates/compatibility.html](../../opp_ci/web/templates/compatibility.html) | filter form, `cell.status`, richer tooltip, legend note |
| `tests/test_compatibility.py` | new — filtering + aggregation semantics |

## Future work

The un-collapsed per-cell run list (`cell.runs`) is the substrate for
the encoding ideas deferred from the brainstorm — none require touching
the data layer again:

- **Faceted cells**: split a cell into sub-dots by one chosen dimension,
  each colored by its own status (most readable multi-dim view).
- **Multi-channel glyph**: color=status, shape=dim, size=dim.
- **Dynamic pivot**: let the user choose row/column axes from the full
  dimension set (would need a small client-side or query-param pivot
  selector).
