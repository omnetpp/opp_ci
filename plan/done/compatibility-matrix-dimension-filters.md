# Compatibility matrix — dimension filters

## Goal

Extend the project compatibility matrix page (`/compatibility/{project}`)
so the empirical pass/fail overlay can be **filtered by the execution
dimensions** that the current page collapses away: OS / distro / flavor,
compiler, build mode / kind, and toolchain / isolation / arch.

Scope decided during brainstorm (2026-06-08, revised):

- **Two-channel cell glyph** (revised 2026-06-08): after a filter is
  applied, several runs still match each cell. Encode the two interesting
  dimensions of that surviving set on the single dot:
  - **status → color** (the run `result_code`, plus the no-run
    `compatible` state),
  - **verdict → symbol** (the `TestVerdictKind`).

  See [Cell encoding](#cell-encoding-two-channels). This reverses the
  earlier "filters only, no new visual encoding" decision — the dot is
  now color **and** symbol, but it is still one dot per cell (no
  sub-dots / faceting yet).
- **Server-side reload** — reuse the existing query-param + dropdown
  pattern from the Runs/Tests pages (`_distinct_options`,
  `_filters.html` macros). No client-side JavaScript is introduced.
- **All dimensions** in scope as filters: `os`, `os_version`, `distro`,
  `distro_version`, `flavor`, `flavor_version`, `compiler`,
  `compiler_version`, `mode`, `kind`, `toolchain`, `isolation`, `arch`.

Deliberately deferred (but the data layer is built so these need **no
rework** later): faceted cells (sub-dots per dimension value) and dynamic
pivot axes. See [Future work](#future-work).

## Cell encoding (two channels)

A filtered cell still aggregates a *set* of surviving runs. Encode two
dimensions of that set independently on the one dot:

**Status → color.** Aggregate the surviving runs' `result_code`. The
rule is pure homogeneity (not the old precedence logic): if every
surviving run shares one status → that status's color; if the set is
mixed → the dedicated `mixed` color. A declared cell with **no**
surviving runs is `compatible`.

| status | color | source |
|---|---|---|
| `compatible` | blue (`--primary` `#0d6efd`) | declared, no surviving run |
| `PASS` | green (`--pass` `#198754`) | `result_code == PASS` |
| `FAIL` | yellow/amber (new var, e.g. `#f2c037`) | `result_code == FAIL` |
| `ERROR` | red (`--fail` `#dc3545`) | `result_code == ERROR` |
| `SKIPPED` | gray (`--muted` `#6c757d`) | `result_code == SKIPPED` |
| `mixed` | distinct 6th hue (e.g. purple `#6f42c1`) | runs disagree |
| _n/a_ | `--border` dot `·` | not declared compatible |

> Note: this status→color mapping **intentionally differs** from the
> existing badge / `cm-*` convention, where `FAIL` is red and `ERROR` is
> orange. Here `FAIL` is yellow and `ERROR` is red, so the matrix needs
> its **own** dot classes (e.g. `cm-fail`/`cm-error` redefined for this
> page, or a fresh `cm2-*` set) rather than reusing `cm-failed` /
> `cm-error`. Yellow is not in `:root` yet — add a `--warn` var.

**Verdict → symbol.** Aggregate the surviving runs' `recorded_verdict`
(`TestVerdictKind`), same homogeneity rule. The symbol overlays the
colored dot (or replaces the glyph — see template note).

| verdict | symbol |
|---|---|
| `EXPECTED` | checkmark `✓` |
| `UNKNOWN` (incl. runs with no recorded verdict) | `?` |
| `UNEXPECTED` | `!` |
| mixed | distinct 4th symbol (e.g. `*`) |

`compatible` (no-run) cells carry **no** verdict symbol — the dot stands
alone. The two channels are independent: a cell can be e.g. green-`✓`
(all PASS, all EXPECTED), red-`!` (all FAIL, all UNEXPECTED), or
purple-`*` (statuses and verdicts both mixed).

The exact `mixed` color and symbol are defined once in the legend, so
they are trivial to change there.

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
      "verdict": run.recorded_verdict,   # TestVerdictKind value or None
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
  filter the per-run list to runs matching every active `filters[dim]`,
  then aggregate **two channels** over the survivors. If none survive but
  the cell was declared → `compatible` (status only, no verdict). Make a
  cell a small dict carrying both channels plus the runs for the tooltip:
  ```python
  {"status": str, "verdict": str | None, "runs": [...]}
  ```
  (Update the template's if/elif chain to read `cell.status` /
  `cell.verdict`.)

- Replace the precedence-based `_aggregate_status` with **two
  homogeneity aggregators** (per the [Cell encoding](#cell-encoding-two-channels)
  rule — *not* the old precedence logic where any FAIL won):
  ```python
  def _aggregate_status(runs):
      codes = {r["result_code"] for r in runs}
      if not codes:            return "compatible"
      if len(codes) == 1:      return next(iter(codes)).value   # PASS/FAIL/ERROR/SKIPPED
      return "mixed"

  def _aggregate_verdict(runs):
      # None (no recorded verdict) folds into UNKNOWN
      kinds = {(r["verdict"] or TestVerdictKind.UNKNOWN) for r in runs}
      if not kinds:            return None        # compatible cell — no symbol
      if len(kinds) == 1:      return next(iter(kinds)).value   # EXPECTED/UNKNOWN/UNEXPECTED
      return "mixed"
  ```
  Both return the literal string `"mixed"` when the survivors disagree —
  the template maps that to the mixed color / mixed symbol.

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
- **Two-channel cell rendering.** Replace the bare-string if/elif chain
  with a dot whose **color comes from `cell.status`** and whose
  **symbol overlay comes from `cell.verdict`**:
  - Map `cell.status` → a dot class (`cm2-compatible/pass/fail/error/
    skipped/mixed`, plus `cm2-none` for the `·`).
  - Map `cell.verdict` → the overlaid glyph: `EXPECTED`→`✓`,
    `UNKNOWN`→`?`, `UNEXPECTED`→`!`, `mixed`→the mixed symbol; `None`
    (compatible / no runs) → no glyph, dot only.
  - Simplest markup: a colored disc (`●`) with the verdict glyph drawn
    on top (absolutely-positioned `<span>` or a small inline-grid), so
    color and symbol read as one mark. A plain text fallback
    (`✓`/`?`/`!` colored by status) is acceptable if overlaying is fiddly.
- Enrich the dot `title=` to show the per-run breakdown when
  `cell.runs` has entries (e.g. `"PASS/EXPECTED — ubuntu/gcc-14
  (release); FAIL/UNEXPECTED — fedora/clang-18 (release)"`), surfacing
  both channels per run on hover.
- **Legend at the bottom of the page** (per request), in two parts:
  one row of status colors (compatible / PASS / FAIL / ERROR / SKIPPED /
  mixed / n-a) and one row of verdict symbols (`✓` expected / `?`
  unknown / `!` unexpected / mixed). Add the one-line note that filters
  narrow the *tested* overlay and that `compatible` means "declared, no
  matching test run." Replace the existing single-row legend.

CSS in `base.html`: add a `--warn` (yellow) and a `--mixed` (purple)
var to `:root`, and a `cm2-*` dot class set implementing the
[status→color table](#cell-encoding-two-channels) — these can't reuse
`cm-failed`/`cm-error` because this page's FAIL/ERROR colors are
swapped relative to the rest of the app. Add a small rule for the
verdict-glyph overlay. `.compat-matrix` table styling is untouched.

### 4. Tests — `tests/test_compatibility.py` (new)

There is currently no compatibility test. Add one:

- Seed a `Project` with `dependency_names`, a couple of `Version` rows
  with `resolved_dependencies`, and several finished `TestRun`s across
  different `os`/`compiler`/`mode` for the same `(version, dep_version)`
  cell with differing `result_code`s.
- Assert **status** channel: no filter → cell `status == "mixed"` when
  result_codes disagree (homogeneity, *not* precedence); `os=<one>`
  filter → cell collapses to that run's status; filter with no matching
  run on a declared cell → `compatible`.
- Assert **verdict** channel: all-EXPECTED survivors → `verdict ==
  "EXPECTED"`; mixed EXPECTED+UNEXPECTED → `verdict == "mixed"`; a run
  with no recorded verdict folds into `UNKNOWN`; `compatible` cell →
  `verdict is None`.
- Assert `options` lists only the project's dimension values.

## Files touched

| File | Change |
|---|---|
| [opp_ci/compatibility.py](../../opp_ci/compatibility.py) | un-collapse overlay, per-run `verdict`, two homogeneity aggregators, `filters` + scoped `options`, dict return |
| [opp_ci/web/app.py](../../opp_ci/web/app.py) | dimension query params on `compatibility_page`, pass options/filters |
| [opp_ci/web/templates/compatibility.html](../../opp_ci/web/templates/compatibility.html) | filter form, two-channel cell (color=status, symbol=verdict), richer tooltip, bottom legend |
| [opp_ci/web/templates/base.html](../../opp_ci/web/templates/base.html) | `--warn`/`--mixed` vars, `cm2-*` dot classes, verdict-glyph overlay rule |
| `tests/test_compatibility.py` | new — status + verdict aggregation + filtering semantics |

## Future work

The un-collapsed per-cell run list (`cell.runs`) is the substrate for
the encoding ideas deferred from the brainstorm — none require touching
the data layer again:

- **Faceted cells**: split a cell into sub-dots by one chosen dimension,
  each colored by its own status (most readable multi-dim view).
- **More channels**: the cell already uses color (status) + symbol
  (verdict); a third channel (shape or size keyed to a dimension) could
  layer on, though readability drops fast past two.
- **Dynamic pivot**: let the user choose row/column axes from the full
  dimension set (would need a small client-side or query-param pivot
  selector).
