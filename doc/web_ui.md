# Web UI

Server-rendered FastAPI + Jinja2 application in `opp_ci/web/`. Started
with `opp_ci coordinator start` (default `127.0.0.1:8080`).

> **Authentication:** the HTML routes listed below carry no auth
> dependency — anyone who can reach the bind address can browse and
> trigger actions exposed by the form pages. Bind to `127.0.0.1` for
> single-user installs, or put a reverse proxy in front (nginx,
> Cloudflare Access, basic-auth, …) before exposing the UI publicly.
> Only the `/api/*` JSON endpoints use the bearer-token roles
> documented in [rest_api.md](rest_api.md#authentication).

## Page map

The UI is organised around the two **definition ↔ execution** pairs in
the data model: a `Test` (coordinate) runs into a `TestRun`, and a
`TestMatrix` (axes) runs into a `TestMatrixRun`. Both halves of each pair
get their own list page; the two *definition* types also get a detail
page with a rename form. The navbar order is:

> Dashboard · Results · Queue · Tests · Test Runs · Test Matrices ·
> Test Matrix Runs · Projects · Compatibility · Rules · OSes · Compilers ·
> Workers · Admin (admins only)

| Path | Purpose |
|---|---|
| `/` | Dashboard — project health badges, recent activity, summary stats |
| `/results` | Multi-dimensional results search — Detailed and Summary modes, CSV export |
| `/queue` | Currently queued / running jobs |
| `/tests` | Test catalog — **named tests only by default**; `?include_anonymous=1` pulls in the per-cell tests minted by matrix runs. Filters by name / project / kind / os / compiler / last-run status. Each row has a **Run** button (submitter+). |
| `/tests/new` | Coordinate form for a new Test. **Save** stores the definition (named or anonymous) and lands on its detail page; **Save & run** also queues a `TestRun`. Git ref / version / OMNeT++ version apply to the run, not the coordinate. |
| `/tests/{test_id}` | Test detail — coordinate, run history, expectation editor + history (forward-only), rename form, and a Run button (submitter+). |
| `/test-runs` | Test runs list — filterable/sortable, cancel and re-run actions |
| `/test-runs/{run_id}` | Run detail — metadata, the outcome columns (`result_code`, `stdout`, `stderr`, `details`) off the same `TestRun` row, colored stdout (ANSI→HTML), re-run / cancel buttons, and a rename form for the underlying `Test` (submitter+). |
| `/test-matrices` | Matrix CRUD — list, create, delete, plus a per-row **Run** button (submitter+). Anonymous (unnamed) matrices render as `(anonymous #id)`. |
| `/test-matrices/new` | Axis form for a new matrix. **Save** stores the definition; **Save & run** also queues a `TestMatrixRun`. |
| `/test-matrices/{matrix_id}` | Matrix detail and expansion preview, plus a rename form and a "Run this matrix" button (submitter+). |
| `/test-matrix-runs` | Index of recent `TestMatrixRun` rows with their stored rollup verdict. Filters by project / verdict (EXPECTED / UNEXPECTED / UNKNOWN) / since. Per-row Re-run / Cancel actions (submitter+). |
| `/test-matrix-runs/{matrix_run_id}` | Rollup header + per-cell `TestVerdict` table for one matrix run, with Re-run / Cancel actions. UNEXPECTED rows are highlighted; UNKNOWN rows carry an inline expectation editor (submitter+) that posts a new `ExpectedTestResult` for that cell's `Test`. `?unexpected_only=1` filters to diverged + undeclared cells. |
| `/projects` | Project catalog list with last tested version and status |
| `/projects/{name}` | Per-project summary, version history, run buttons. Carries a "Latest release run" card showing the most recent tag-triggered `TestMatrixRun` with its verdict (EXPECTED ⇒ release-ready), counters, and links to the matrix / matrix-run detail. |
| `/compatibility` | Project compatibility index |
| `/compatibility/{project}` | Compatibility matrix vs. dependency versions |
| `/rules` | AutoTestRule CRUD for GitHub triggers |
| `/rules/{id}` | Rule detail |
| `/os` | Known OS / OS-version combinations seen in runs |
| `/compilers` | Known compiler / compiler-version combinations seen in runs |
| `/workers` | Registered workers with heartbeat / tags / current jobs |
| `/admin` | Workers, API tokens, project registration, system health (admins only) |
| `/commits/{project}/{sha}` | Per-commit summary for a project (linked from git notes) |

> **Routes were renamed** from the earlier `/runs`, `/matrices`,
> `/matrix-runs` scheme to the `/test-*` names above; there are **no
> backward-compatibility redirects**, so update any bookmarks. The
> `/api/*` JSON routes are unaffected.

## Results page filter and display modes

`/results` is a **multi-dimensional filter** — every stored dimension
can be independently constrained:

| Filter dimension | Examples |
|---|---|
| Project | inet, omnetpp, simu5g, … |
| Project version | 4.5, 4.6, master, git |
| Dependency versions | omnetpp: 6.1, 6.0; inet: 4.5 |
| OS / OS version | Ubuntu 24.04, Fedora 41, macOS 15 |
| Compiler / compiler version | gcc-14, clang-18 |
| Build mode | release, debug |
| Kind | smoke, fingerprint, statistical, … |
| Result status | PASS, FAIL, ERROR, SKIPPED |

Dimensions left unset act as wildcards.

Two display modes:

1. **Detailed** — one row per result. Every dimension and metadata
   (duration, timestamp, stdout link) is shown.
2. **Summary** — rows collapsed across the unfiltered dimensions:
   - Uniform-status groups collapse to one line.
   - Mixed-status groups show a breakdown ("18 PASS, 2 FAIL") with a
     drill-down link.
   - Grouping is hierarchical: project+version → test → remaining
     dimensions.

Example summary view for "INET 4.6" with no other filters fixed:

```
inet 4.6 / smoke        — PASS (all 12 combinations)
inet 4.6 / fingerprint  — 46 PASS, 2 FAIL  [expand]
inet 4.6 / statistical  — PASS (all 8 combinations)
```

Rollup logic is in `opp_ci/web/rollup.py`.

## Matrix runs and the release-readiness view

The `/test-matrix-runs` and `/test-matrix-runs/{id}` pages answer the
"is this release ready to publish?" question by reading the **stored**
rollup columns on each `TestMatrixRun` — no fan-out across cells at
render time. The verdict is the three-state grade
([`TestVerdictKind`](data_model.md#testverdictkind)):

- **EXPECTED** — every cell met its declared expectation; release-ready.
- **UNEXPECTED** — at least one cell diverged (wrong outcome, or
  unexpected ERROR).
- **UNKNOWN** — no mismatches, but at least one cell ran without a
  declared expectation. Declare an expectation on the cell (via the
  inline editor) and re-run to flip it.

Cells whose `TestVerdict.cache_hit` is true reuse a prior `TestRun`
via the [content-addressable cache](data_model.md#cache-fingerprint)
— the run column on the detail page links to the original observation.

Expectations edited inline apply *forward only*: the historical
verdict pins the specific `ExpectedTestResult` row that was in force
at recording time (via `TestVerdict.expectation_id`), so old rollups
stay reconstructible. The detail page surfaces this with a
"future runs only" hint next to the editor.

## Admin actions

The `/admin` page provides:

- **Workers** — table with status / tags / heartbeat / current jobs; register form.
- **API tokens** — table with revoke buttons; create form.
- **Projects** — register a project not in the opp_env catalog.
- **System health** — DB connectivity, queue depth, worker counts.

The same operations are available via `opp_ci worker / token` CLI
groups and the REST API.

## ANSI handling

opp_repl stdout contains ANSI escape codes. They are stored **raw** in
`TestRun.stdout` and `TestRun.stderr` (on the same row as the
lifecycle and outcome — there is no separate `TestResult` table after
the phase-1 schema cutover), then converted to colored HTML at render
time via a Jinja filter. Don't strip ANSI codes before storage —
downstream tools and the comparison view depend on them.
