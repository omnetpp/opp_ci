# Web filter controls & filter completeness

## Goal

Two related changes to the web UI filter bars:

1. **Right control for the right field type.** Today most filters are
   equality `<select>` combo boxes, including for non-enumeration values
   (versions, opp-file paths, project names). A single-select equality
   dropdown can't express the queries people actually want for ordered /
   open-ended values ("all 13.x compilers", "≥ 6.0"), and the option
   lists grow unboundedly as data accumulates. Replace the combo box with
   a **typeable combo (datalist hybrid)** for those fields, keep equality
   selects for genuinely bounded categoricals, and keep text-contains for
   free-form identifiers.

2. **Complete filters on every list page.** Several pages are missing
   filter fields that exist as columns / matrix dimensions. Every list
   page (Test, TestRun, results, TestMatrix, TestMatrixRun, Project) gets
   a complete filter set, and in particular **every test-matrix dimension
   field becomes searchable** wherever that page exposes matrix data.

This plan does not change what data is stored — only the filter UI and
the query predicates behind it.

## Control taxonomy

Classify every filterable field into one of four control types. This is
the single rule the rest of the plan applies per page.

| Control | When | Widget | Backend match |
|---|---|---|---|
| **Equality select** (`f.sel`) | Bounded categorical: a small, fixed vocabulary that does not grow with data | `<select>` of distinct values, "Any" default | `col == value` |
| **Typeable combo** (`f.combo`, NEW) | Open-ended / ordered value where the option set grows and partial match is wanted: versions, project, opp_file | `<input list=…>` + `<datalist>` of distinct values | `col ILIKE %value%` (prefix `value%` for the `*_version` fields — see note) |
| **Text-contains** (`f.txt`) | High-cardinality free-form identifier, no useful suggestion list: name, git ref / commit, github owner/repo, URLs | `<input type=text>` | `col ILIKE %value%` |
| **Date range** (`f.date`) | Timestamps | two `<input type=date>` (since / until) | `created_at >= since`, `< until+1d` |

Field-to-control assignment used throughout:

- **Equality select:** `kind`, `mode`, `os`, `arch`, `isolation`,
  `toolchain`, `distro`, `flavor`, `compiler`, `status`, `verdict`,
  `actual`, `state`, `trigger`. (Bounded — `os` is Linux/Windows/MacOS;
  `distro`/`flavor`/`compiler` come from the platforms registries.)
- **Typeable combo:** `project`, `version`, `os_version`,
  `distro_version`, `flavor_version`, `compiler_version`, `opp_file`.
- **Text-contains:** `name`, `git_ref`, `ref`, `commit_sha`,
  `github_owner`, `github_repo`, `github_commit_sha`,
  `github_pr_number`, `git_url`, `dep` (resolved-deps substring).
- **Date range:** `since` / `until` over the row's `created_at`.

**Why the combo for versions, not text-contains:** the `<datalist>`
still surfaces every value present in the data (discoverability + the
"this selection returns rows" guarantee the old dropdown gave), while
allowing free typing for partial match. Best of both.

**Substring vs prefix on versions.** Default the combo to substring
`ILIKE %v%`. The `*_version` fields are the one place substring
over-matches (`6` matches `16`, `26`), so those use **prefix** `ILIKE
v%` instead — typing `13.` still gets all `13.x`. This is a per-field
flag on the combo handler, not a separate widget.

## Shared infrastructure

### `_filters.html` — new `combo` macro

Add alongside `txt` / `date` / `sel`:

```jinja2
{% macro combo(filters, name, label, options, placeholder="type or pick…") -%}
<label class="filter-field"><span>{{ label }}</span>
<input type="text" name="{{ name }}" value="{{ filters[name] }}"
       list="dl-{{ name }}" placeholder="{{ placeholder }}" autocomplete="off">
<datalist id="dl-{{ name }}">
{% for opt in options %}<option value="{{ opt }}">{% endfor %}
</datalist></label>
{%- endmacro %}
```

Note: `<datalist>` suggestion filtering is browser-side and best-effort;
the backend `ILIKE` is what actually filters, because the user may type
a value not in the list.

### `app.py` — option building + a match helper

- `_distinct_options(session, *columns)` already produces the per-column
  distinct lists that feed both `sel` and `combo`. Reuse it on the pages
  that don't call it yet (`/results`, and the extra columns added to
  `/test-runs`, `/projects`).
- Add a tiny helper to keep handlers uniform:

```python
def apply_str_filter(query, col, value, mode="eq"):
    if not value:
        return query
    if mode == "contains":
        return query.where(col.ilike(f"%{value}%"))
    if mode == "prefix":
        return query.where(col.ilike(f"{value}%"))
    return query.where(col == value)
```

Handlers call `apply_str_filter(q, Test.os_version, os_version, "prefix")`
etc. instead of open-coding each `if`.

### Matrix-dimension filtering (config JSON)

Matrix axes live in `TestMatrix.config` as JSON lists, not columns. They
must still be filtered **in SQL**, not with a Python post-filter: the
matrix-runs page applies `LIMIT`, and SQL evaluates `WHERE` before
`LIMIT`, so a Python filter running after the query would truncate rows
out of the window before the axis filter ever saw them (a matching run
older than the limit would silently vanish). `matrix_axis_sql_filter`
adds a correlated `EXISTS` over the JSON array per active axis:

```python
def matrix_axis_sql_filter(query, axis_filters, dialect):
    for param, ckey, _label, control in _MATRIX_AXES:
        val = axis_filters.get(param)
        if not val:
            continue
        bind = f"axis_{param}"
        if dialect == "postgresql":
            op  = "ILIKE" if control == "combo" else "="
            pat = f"%{val}%" if control == "combo" else val
            clause = text(f"EXISTS (SELECT 1 FROM json_array_elements_text("
                          f"test_matrices.config -> '{ckey}') AS _ax(v) "
                          f"WHERE _ax.v {op} :{bind})")
        else:  # sqlite — LIKE is case-insensitive for ASCII
            op  = "LIKE" if control == "combo" else "="
            pat = f"%{val}%" if control == "combo" else val
            clause = text(f"EXISTS (SELECT 1 FROM json_each("
                          f"test_matrices.config, '$.{ckey}') WHERE value {op} :{bind})")
        query = query.where(clause.bindparams(**{bind: pat}))
    return query
```

JSON list expansion differs per dialect (SQLite `json_each` vs Postgres
`json_array_elements_text`), hence the branch. `ckey` is from the fixed
`_MATRIX_AXES` table (never user input) so interpolating it into the JSON
path is safe; the compared value is always a bound parameter. Combo
(substring) semantics = "any axis value contains the typed text"; selects
keep exact membership. `matrix_axis_options` (distinct values per axis,
for the dropdowns/datalists) stays a plain Python gather over all
matrices.

### OS & Compilers registry pages

`/os` (name, version, arch) and `/compilers` (name, version) already
filtered every column, but as bare text-contains inputs. Realigned to the
taxonomy via the shared macros: `name`/`arch` → equality select, version
→ combo (prefix). Pure control swap; same columns.

## Per-page target filters

Legend: ✓ keep · **CHG** control change · **NEW** add.

### Test — `/tests` (`tests.html`, `tests_list`)

| Field | Now | Target | |
|---|---|---|---|
| name | txt | text-contains | ✓ |
| project | sel | combo | CHG |
| kind, mode, os, arch, isolation, toolchain, distro, flavor, compiler | sel | equality select | ✓ |
| os_version, distro_version, flavor_version, compiler_version | sel | combo (prefix) | CHG |
| opp_file | — | combo | **NEW** |
| dep (resolved_deps substring) | — | text-contains | **NEW** |
| status | sel | equality select | ✓ |

`dep` matches a substring of the serialized `resolved_deps` (e.g.
`omnetpp 6.4` / `6.4.0`); SQLite/Postgres JSON-as-text `ILIKE` is enough
for a first cut.

### TestRun — `/test-runs` (`runs.html`, `runs_list`)

Expose the full Test coordinate (joined) plus run-specific context.

| Field | Now | Target | |
|---|---|---|---|
| project | sel | combo | CHG |
| kind, mode, os, distro, compiler | sel | equality select | ✓ |
| os_version, distro_version, flavor, flavor_version, arch, compiler_version, isolation, toolchain | — | sel/combo per taxonomy | **NEW** |
| opp_file | — | combo | **NEW** |
| version | sel | combo (prefix) | CHG |
| git_ref | txt | text-contains | ✓ |
| worker | sel | select | ✓ |
| status | sel | equality select | ✓ |
| trigger | — | equality select | **NEW** |
| github_owner, github_repo | — | text-contains | **NEW** |
| github_pr_number | — | text-contains | **NEW** |
| dep | — | text-contains | **NEW** |
| since / until | date | date range | ✓ |

`trigger` / `github_*` / `pr_number` live on the parent `TestMatrixRun`;
filtering them needs a join from `TestRun → TestMatrixRun` (left join, so
standalone runs with no matrix run survive when the filter is unset).

### Results — `/results` (`results.html`, `results_page`)

This page already accepts every dimension as a query param but renders
them as **plain text inputs with exact-equality** backends — the worst
combination (must type the exact string, no suggestions). Realign it to
the taxonomy:

- Build `options` via `_distinct_options` (page currently passes none).
- Categoricals → equality select; `*_version` + `project` + `opp_file` →
  combo; `status` → select (keep).
- Add `dep` text-contains (**NEW**).

No new dimensions needed — purely a control-type + match-semantics fix to
make `/results` consistent with `/tests`.

### TestMatrix — `/test-matrices` (`matrices.html`, `matrices_list`)

Axes already cover every matrix dimension as `f.sel` membership filters.
Changes:

| Field | Now | Target | |
|---|---|---|---|
| name | txt | text-contains | ✓ |
| project | sel | combo | CHG |
| version, os_version, distro_version, flavor_version, compiler_version | sel | combo | CHG |
| kind, mode, os, arch, isolation, toolchain, distro, flavor, compiler | sel | equality select | ✓ |
| opp_file | — | combo | **NEW** |
| since / until (created_at) | — | date range | **NEW** |

### TestMatrixRun — `/test-matrix-runs` (`matrix_runs.html`, `matrix_runs_list`)

The explicit "all matrix dimension fields searchable" requirement lands
hardest here: a matrix run currently can't be filtered by the dimensions
of its underlying matrix.

| Field | Now | Target | |
|---|---|---|---|
| project | sel | combo | CHG |
| matrix | sel | select | ✓ |
| trigger, verdict, actual, state | sel | equality select | ✓ |
| ref | txt | text-contains | ✓ |
| github_owner, github_repo | — | text-contains | **NEW** |
| github_commit_sha | — | text-contains | **NEW** |
| github_pr_number | — | text-contains | **NEW** |
| **all matrix axes** (kind, mode, version, os, os_version, distro, distro_version, flavor, flavor_version, compiler, compiler_version, arch, isolation, toolchain) | — | sel/combo per taxonomy, filtered via joined `TestMatrix.config` | **NEW** |
| since / until | date | date range | ✓ |

The query already joins `TestMatrix`; reuse `filter_by_matrix_axes` with
`get_matrix = lambda row: row[1]` (the `TestMatrix` element of each
`(TestMatrixRun, TestMatrix)` row). Build axis `options` from all
matrices' configs exactly as `matrices_list` does — factor that into a
shared `matrix_axis_options(matrices)` helper.

### Project — `/projects` (`projects.html`, `projects_list`)

| Field | Now | Target | |
|---|---|---|---|
| name | txt | text-contains | ✓ |
| opp_env_name | — | combo | **NEW** |
| github_owner | — | combo | **NEW** |
| github_repo | — | text-contains | **NEW** |
| git_url | — | text-contains | **NEW** |
| dep (dependency_names substring) | — | text-contains | **NEW** |
| status (last finished run status) | — | equality select | **NEW** |

`status` reuses the already-computed `last_status[p.name]` map —
post-filter in Python like `/tests` does, no extra query.

## Implementation order

1. `_filters.html`: add the `combo` macro.
2. `app.py`: add `apply_str_filter`, `filter_by_matrix_axes`,
   `matrix_axis_options` helpers.
3. `/tests`: switch controls, add `opp_file` + `dep`. (Smallest delta;
   validates the macro + helpers end-to-end.)
4. `/results`: build options, swap text→select/combo, keep equality for
   categoricals, add `dep`.
5. `/test-runs`: add missing dimension params + the join-backed
   `trigger`/`github_*`, switch controls.
6. `/test-matrices`: combos for versions/project, add `opp_file` +
   date range.
7. `/test-matrix-runs`: add `github_*` and the full matrix-axis filter
   block via the shared helpers.
8. `/projects`: add the remaining columns + `status`.

Each step is one route handler + its template; ship and eyeball
independently.

## Testing

- Unit: `apply_str_filter` (eq / contains / prefix, empty value is a
  no-op); `filter_by_matrix_axes` membership; `matrix_axis_options`
  dedup/sort.
- Route tests per page: each new param narrows results and an unknown
  value yields zero rows without error (the forgiving-URL behaviour the
  date/status filters already have).
- Combo prefix vs substring: assert `os_version=6` matches `6.x` but not
  `16.x`; `compiler=13.` (combo) matches `13.2.0` and `13.3.0`.
- Existing filter route tests stay green (equality selects unchanged).

## Out of scope / open questions

- **Co-dependent facets.** Option lists are still computed over the whole
  table, so impossible combinations (os=Windows + distro=ubuntu) remain
  selectable and return zero rows. True faceted narrowing is a separate,
  larger change — not addressed here.
- **`dep` filter depth.** First cut is a substring match over the
  JSON-serialized `resolved_deps` / `dependency_names`. A structured
  `name=version` predicate (JSON path) can come later if substring proves
  too blunt.
- **Per-column `DISTINCT` cost.** Each page already runs one `DISTINCT`
  per select/combo column; adding columns adds queries. Fine at current
  table sizes; revisit with a cached options endpoint if it shows up in
  profiles.
- **Numeric range filters** (duration, reclaim_count) are intentionally
  excluded — no current demand.
