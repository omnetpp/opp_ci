# Web UI

Server-rendered FastAPI + Jinja2 application in `opp_ci/web/`. Started
with `opp_ci serve` (default `127.0.0.1:8000`).

## Page map

| Path | Purpose |
|---|---|
| `/` | Dashboard — project health badges, recent activity, summary stats |
| `/projects` | Project catalog list with last tested version and status |
| `/projects/{name}` | Per-project summary, version history, run buttons |
| `/runs` | Test runs list — filterable/sortable, cancel and re-run actions |
| `/runs/new` | Submit form — single run or "Run from Matrix" |
| `/runs/{run_id}` | Run detail — metadata, per-test results table, colored stdout (ANSI→HTML), re-run / cancel buttons |
| `/results` | Multi-dimensional results search — Detailed and Summary modes, CSV export |
| `/compare` | Side-by-side diff of two runs or two branches |
| `/queue` | Currently queued / running jobs |
| `/matrices` | Matrix CRUD — create form, list, delete |
| `/matrices/{id}` | Matrix detail and expansion preview |
| `/rules` | AutoTestRule CRUD for GitHub triggers |
| `/rules/{id}` | Rule detail |
| `/compatibility` | Project compatibility index |
| `/compatibility/{project}` | Compatibility matrix vs. dependency versions |
| `/os` | Known OS / OS-version combinations seen in runs |
| `/compilers` | Known compiler / compiler-version combinations seen in runs |
| `/admin` | Workers, API tokens, project registration, system health |
| `/commit/{sha}` | Per-commit summary (used by git notes) |

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
| Test type | smoke, fingerprint, statistical, … |
| Result status | PASS, FAIL, ERROR, SKIP |

Dimensions left unset act as wildcards.

Two display modes:

1. **Detailed** — one row per result. Every dimension and metadata
   (duration, timestamp, stdout link) is shown.
2. **Summary** — rows collapsed across the unfiltered dimensions:
   - Uniform-status groups collapse to one line.
   - Mixed-status groups show a breakdown ("18 PASS, 2 FAIL") with a
     drill-down link.
   - Grouping is hierarchical: project+version → test type → remaining
     dimensions.

Example summary view for "INET 4.6" with no other filters fixed:

```
inet 4.6 / smoke        — PASS (all 12 combinations)
inet 4.6 / fingerprint  — 46 PASS, 2 FAIL  [expand]
inet 4.6 / statistical  — PASS (all 8 combinations)
```

Rollup logic is in `opp_ci/web/rollup.py`.

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
`TestResult.stdout` and `TestResult.stderr`, then converted to colored
HTML at render time via a Jinja filter. Don't strip ANSI codes before
storage — downstream tools and the comparison view depend on them.
