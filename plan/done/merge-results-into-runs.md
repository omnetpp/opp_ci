# Merge the Results page into Test Runs (Flat / Rollup toggle)

Retire the separate `/results` page. The `/test-runs` page becomes the single
"Runs" page with a **View** toggle: **Flat** (the existing run list) or
**Rollup** (the former Results summary). The Detailed run-list that Results
carried is dropped — it duplicated Test Runs.

## Why
Results' Detailed view is a flat `TestRun` list linking to `/test-runs/<id>` —
functionally Test Runs, with now-identical filters. Results' real value is the
**rollup** (Summary). One page, one filter set, two ways to view the result set.

## Handler (`runs_list`, `/test-runs`)
Fold `results_page`'s rollup logic into `runs_list`:
- New params: `view` (default flat), `grouping` (any), `show_obsolete` (False),
  `run_ids` (drill-down). Normalize legacy values: `summary`→rollup,
  `detailed`→flat.
- `run_ids` → `where(TestRun.id.in_(...))` (drill-down from a rollup row).
- Obsolete-hiding (the `~exists(newer finished run at same test+commit)` clause
  from results_page) applies in **rollup view unless `show_obsolete`**; flat
  view keeps showing everything (current Test Runs behavior).
- `selectinload(TestRun.verdicts)` so the rollup's `recorded_verdict` is cheap.
- `limit` defaults per view: 50 flat, 200 rollup.
- When rollup: `summaries = rollup_runs(runs, grouping)`,
  `extra_dims = visible_extra_dims(summaries)`; else both None/[].
- Context already supplies workers + verdict/actual/state options; add
  `summaries`, `extra_dims`, `view`, `grouping`, `show_obsolete`.

## Template (`runs.html`)
- Add a **Display** group to the filter form: View (Flat/Rollup), Grouping,
  show obsolete (ported from results.html).
- Branch after the form: `{% if view == "rollup" %}` render the summary table
  (port results.html lines ~76-140, incl. the deps_label/primary_dims work);
  `{% else %}` the existing flat table.
- Rollup row drill-down link: `/results?...&view=detailed` →
  `/test-runs?run_ids=...` (flat view of exactly those runs).

## Retire `/results`
- Replace `results_page` with a redirect: `/results?…` → `/test-runs?…`,
  defaulting `view=summary` when absent (bare /results was the rollup). Keeps
  old bookmarks, the compatibility drill-down, and admin link working.
- Delete `results.html` after porting its summary block into runs.html.
- Move the `from opp_ci.web.rollup import …` into `runs_list`.

## Call sites
- `base.html` nav: remove the **Results** link (keep "Test Runs").
- `compatibility.html:32`: repoint to `/test-runs?run_ids=…` (flat shows all,
  incl. obsolete — drop the `view=detailed&show_obsolete=1`).
- `admin.html:192` "Results Search": repoint to `/test-runs?view=rollup`.

## Tests
- Repoint the `/results*` cases in test_filter_controls to `/test-runs`
  (flat) / `/test-runs?view=rollup`.
- Add a redirect test: `GET /results?view=summary` → 307 → `/test-runs`.
- Keep the deps-column assertion against the rollup view.

## Verify
- Flat view unchanged; Rollup view matches old Results summary (Deps column
  shows omnetpp=…). Drill-down from a rollup row lands on the flat list of those
  runs. `/results` and all old links still resolve.
