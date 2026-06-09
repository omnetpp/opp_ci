# Consistent, grouped filters across the four list pages

Make the filter sets on **Tests**, **Test Runs**, **Test Matrices**, and **Test
Matrix Runs** consistent with each other, add the missing controls, and group
the (now numerous) fields into collapsible sections so the forms don't overwhelm.

## Background / model facts

Two families of entity, which is the right granularity for "consistency" — we
align *within* each family, and only share what makes sense *across* them.

| Entity | Kind | Timestamps | Result/verdict storage |
|---|---|---|---|
| `Test` | definition (stable per coordinate) | `created_at` only | last run's `effective_status` (computed per row, `app.py:760`) |
| `TestMatrix` | definition (stable per name) | `created_at` only | none today |
| `TestRun` | per-attempt | `created_at`, `started_at`, `finished_at` | `lifecycle` + `result_code` cols; `effective_status`/`recorded_verdict` are **computed properties** (`models.py:594`, `:622`) |
| `TestMatrixRun` | per-submission | `created_at`, `completed_at` | `verdict` + `actual_summary` **columns** (`models.py:354`,`:353`) |

Git/worker columns (relevant to the run pages):
- `TestRun`: `git_ref` + `commit_sha` (both cols, `models.py:447-449`), `worker_id` (`:444`).
- `TestMatrixRun`: `ref` + `github_commit_sha` (`models.py:333,336`), **no worker**.
- `TestRun`'s `trigger`/`github_*` are computed properties delegating to its
  `matrix_run` (`models.py:574-591`) — already filterable via the existing join.

## Design decisions (settled)

1. **Date filters live on run pages only.** Remove `since`/`until` from Test
   Matrices (it filtered `TestMatrix.created_at` = "first seen", which collides
   semantically with the run pages' "ran during"). Tests never had it. *(User
   decision.)*
2. **Two separate git fields, "Ref" + "Commit", on both run pages.** Both models
   already store both columns, so this is just a UI/backend mapping change. Today
   Test Runs collapses them into one "Git ref" box; Test Matrix Runs already has
   two. Standardize on two.
3. **Worker stays Test-Runs-only.** A matrix run spans many workers; there is no
   worker column on `TestMatrixRun`. Document as intentional, not a gap.
4. **Dependency stays on Tests + Test Runs only.** It's a test-coordinate
   attribute, not a matrix axis — not applicable to the matrix pages.
5. **Unify the result/status vocabulary on the run pages** onto three controls:
   **State / Actual / Verdict** (replacing Test Runs' single "Status"). This is
   the heaviest change → **Phase 2**.

## Target filter sets

Legend: ✅ already there · ➕ add · ➖ remove · ⬜ intentionally absent

### Group: Identity & scope
| Field | Tests | Matrices | Test Runs | Matrix Runs |
|---|---|---|---|---|
| Name | ✅ | ✅ | ⬜ | ⬜ |
| Project | ✅ | ✅ | ✅ | ✅ |
| Matrix (which matrix) | ⬜ | — | ⬜ | ✅ |
| Include anonymous | ✅ | ✅ | ⬜ | ⬜ |

### Group: Environment / build (the 14–16 axis fields)
Kind, Mode, Version, OS, OS version, Distro, Distro version, Flavor, Flavor
version, Arch, Compiler, Compiler version, Isolation, Toolchain, Opp file,
Dependency.
- All four pages expose the **same axis list** (matrix pages via `_MATRIX_AXES`
  JSON filter; def/run pages via columns).
- ➕ **Version on Tests**: only add if `Test` actually carries a version
  coordinate — verify in `models.py`. If version is run-level only, leave it
  ⬜ on Tests and note so.
- Dependency: ✅ Tests, ✅ Test Runs, ⬜ matrix pages (decision 4).

### Group: Source & trigger (run pages only)
| Field | Test Runs | Matrix Runs |
|---|---|---|
| Ref | ➕ (split out of "Git ref") | ✅ |
| Commit | ➕ (split out of "Git ref") | ✅ |
| Trigger | ✅ | ✅ |
| GitHub owner / repo / PR# | ✅ | ✅ |
| Worker | ✅ | ⬜ (decision 3) |

### Group: Result
| Field | Tests | Matrices | Test Runs | Matrix Runs |
|---|---|---|---|---|
| Last status (of latest run) | ✅ | ➕ | — | — |
| State | — | — | ➕ (Phase 2, from `lifecycle`) | ✅ |
| Actual | — | — | ➕ (Phase 2, from `result_code`) | ✅ |
| Verdict | — | — | ➕ (Phase 2, from `recorded_verdict`) | ✅ |

- ➕ **Last status on Test Matrices**: mirror Tests — one select filtering on the
  most-recent `TestMatrixRun.actual_summary` (and/or `verdict`) per matrix.
- Phase 2 verdict filter on Test Runs needs a **join/subquery to `TestVerdict`**
  (`recorded_verdict` is computed, not a column). State→`lifecycle`,
  Actual→`result_code` are direct.

### Group: Time (run pages only)
Since, Until — ✅ both run pages, ➖ removed from Test Matrices.

## Filter grouping (visual only)

Group the fields into **visual sections** so the form reads as a handful of
labelled clusters instead of one long wall of inputs. **Not collapsible** — every
group is always rendered and visible; the grouping is purely layout (a heading
plus a bordered/spaced block). Implement once in `_filters.html`, reuse on all
four templates.

### New macros in `templates/_filters.html`
```jinja
{# Visual filter section: a labelled block wrapping a .filter-grid. Always
   shown — grouping is layout only, nothing collapses. #}
{% macro group_open(title) -%}
<fieldset class="filter-group">
  <legend>{{ title }}</legend>
  <div class="filter-grid">
{%- endmacro %}
{% macro group_close() -%}
  </div></fieldset>
{%- endmacro %}
```
- The existing `txt/date/sel/combo` field macros are unchanged — groups just wrap
  the `.filter-grid`.
- No `<details>`, no badge, no auto-open, no active-count helper.
- CSS for `.filter-group`/`legend` goes in `base.html`'s style block — light
  border + heading, matching the existing `.filter-*` look. The existing
  `.filter-grid` keeps doing the wrapping/columns within each section.

### Per-page group layout (example: Test Runs)
```
Identity & scope      Project
Result                State · Actual · Verdict
Environment / build   (16 axis fields)
Source & trigger      Ref · Commit · Trigger · GH×3 · Worker
Time                  Since · Until
```
All sections are visible at once; only the heading/border separates them.
Tests/Matrices: only Identity, Result, Environment sections (no Source/Time).
Matrix Runs: Identity (incl. Matrix), Result, Environment, Source, Time.

## Implementation steps

**Phase 1 — grouping + cheap consistency wins**
1. Add `group_open`/`group_close` (visual section) macros to `_filters.html`;
   add `.filter-group`/`legend` CSS to `base.html`.
2. Rewrap each of the four templates' `.filter-grid` into the sections above
   (`tests.html`, `matrices.html`, `runs.html`, `matrix_runs.html`).
3. ➖ Remove `since`/`until` from `matrices.html` and from the `/test-matrices`
   handler (`app.py:1380-1392`).
4. ➕ Split Test Runs "Git ref" → "Ref" + "Commit": template (`runs.html:28`) and
   handler (`app.py:475-478`) — map Ref→`git_ref`, Commit→`commit_sha` prefix.
5. ➕ Align the build-axis list across pages (add Version to Tests **iff** a
   `Test` version coordinate exists; otherwise document N/A).
6. ➕ "Last status" on Test Matrices: compute latest `TestMatrixRun` per matrix
   (mirror the Tests `last_status` pattern at `app.py:760`), add the select +
   options, apply as a post-query filter like Tests does.

**Phase 2 — unify the run-page result model**
7. Replace Test Runs' single "Status" with **State / Actual / Verdict**:
   - State → `TestRun.lifecycle`, Actual → `TestRun.result_code` (direct SQL).
   - Verdict → `recorded_verdict` via a join/subquery to `TestVerdict`
     (`models.py:622`); confirm an efficient query (latest promoted verdict).
   - Update `runs.html`, the handler's status logic (`app.py:482-488`), and the
     option lists (`app.py:331-334`).
   - Keep Matrix Runs' existing Verdict/Actual/State; just ensure identical
     labels/option vocabularies so the two run pages read the same.

## Verification
- Each page renders the grouped form; all sections are visible (no collapsing),
  separated by their headings/borders.
- Every filter round-trips: set it, submit, confirm the URL param persists in the
  control and the result set narrows correctly.
- Cross-check the two run pages produce the same vocabulary for State/Actual/
  Verdict; cross-check the two def pages for Last status.
- No regression on existing combos/datalists.

## Open items to confirm during implementation
- Does `Test` carry a `version` coordinate? (gates step 5's Version-on-Tests).
- Matrix "Last status": filter on `actual_summary`, `verdict`, or offer both as
  two selects? (lean: one "Last result" = `actual_summary`, plus optional "Last
  verdict").
- Efficiency of the Test-Runs verdict join in Phase 2 on large datasets.
