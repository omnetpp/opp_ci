# Dashboard summary overview

Extend the opp_ci dashboard from "4 outcome tiles + recent runs" into a
real overview hub: lifecycle-state summary, worker summary, entity
inventory, with a time-window selector — and move the recent-runs table
off the dashboard onto the Queue page.

## Motivation

Today [`dashboard.html`](../../opp_ci/web/templates/dashboard.html) shows
only four tiles (Total / Passed / Failed / Errors) plus a 20-row recent
runs table. It ignores:

- lifecycle states other than the finished outcomes (queued, running,
  cancelled, timed_out, and SKIPPED outcomes),
- worker fleet health (connected/total, busy, capacity),
- the inventory the system manages (tests, matrices, matrix runs,
  projects, rules, OSes, compilers),
- any sense of *recency* — counts are all-time, so a long-lived install
  shows numbers that never move.

The recent-runs table also overlaps conceptually with the Queue page,
which already shows running + queued but stops short of "what just
finished."

## Decisions (resolved with user)

1. **Time selector: yes — day / week / month / all.** Scopes the
   lifecycle/outcome counts and any recency-based numbers. Inventory
   counts (projects, OSes, compilers, rules, tests, matrices) stay
   absolute — they are not time-bound. Worker summary is always current.
2. **Move "Recent Runs" to the Queue page**, renaming the page concept to
   **Activity**: Running / Queued / Recently finished. Dashboard becomes
   pure summary tiles.
3. **Layout: grouped stat sections** — distinct rows of tiles under
   headings (Lifecycle, Outcomes, Workers, Inventory). Every number links
   to its filtered list page.

## Data model recap (no schema changes needed)

From [`models.py`](../../opp_ci/db/models.py):

- `TestRunLifecycle`: `queued`, `running`, `finished`, `cancelled`,
  `timed_out`.
- `TestResultCode` (only when `lifecycle == finished`): `PASS`, `FAIL`,
  `ERROR`, `SKIPPED`.
- `TestRun.effective_status` already collapses these into one label and
  drives the existing `badge-*` CSS — reuse it for consistency.
- Time fields: `created_at`, `started_at`, `finished_at`. Use
  `created_at` for the window filter (a run created in the window counts,
  regardless of when/if it finished).
- `Worker`: `status` (online/busy/offline), `concurrency`,
  `current_job_count`, `last_heartbeat`; connected = heartbeat within
  `WORKER_HEARTBEAT_TIMEOUT`.
- Inventory tables: `Test`, `TestMatrix`, `TestMatrixRun`, `Project`,
  `Rule`, plus OS/compiler catalog tables and `Worker`.

## Plan

### 1. Time-window helper

Add a small shared helper (in `app.py`, near `_distinct_options`) that
turns a `window` query param into a UTC cutoff:

```python
def _window_cutoff(window):  # "day" | "week" | "month" | "all" (default)
    now = datetime.datetime.utcnow()
    return {
        "day":   now - datetime.timedelta(days=1),
        "week":  now - datetime.timedelta(days=7),
        "month": now - datetime.timedelta(days=30),
    }.get(window)  # None => no lower bound (all-time)
```

Validate/normalise unknown values to `"all"`. Window is applied as
`TestRun.created_at >= cutoff` when cutoff is not None.

### 2. Dashboard route ([`app.py:230`](../../opp_ci/web/app.py))

Replace the four hard-coded counts with grouped aggregates computed in a
few queries (prefer `group_by` over one-query-per-bucket):

- **Lifecycle counts** (window-scoped): one
  `select(TestRun.lifecycle, func.count()).group_by(TestRun.lifecycle)`
  → dict keyed by lifecycle value. Surfaces queued / running / finished /
  cancelled / timed_out.
- **Outcome counts** (window-scoped, finished only): one
  `group_by(TestRun.result_code)` filtered to
  `lifecycle == finished` → PASS / FAIL / ERROR / SKIPPED.
- **Worker summary** (always current): reuse the connected/total/busy
  logic from `workers_list` ([`app.py:2074`](../../opp_ci/web/app.py)).
  Factor the heartbeat/connected computation into a small
  `worker_summary(session)` helper so both routes share it:
  `{registered, connected, busy, idle_capacity, total_running_jobs}`.
- **Inventory counts** (absolute): `func.count()` per entity table —
  Tests, Test Matrices, Test Matrix Runs, Projects, Rules, OSes,
  Compilers. Cheap; can be a single helper returning a dict.

Pass `window` (the normalised value) into the template so the selector
can mark the active option and links can preserve it.

### 3. Dashboard template ([`dashboard.html`](../../opp_ci/web/templates/dashboard.html))

- Add a **window selector** at top (segmented links `Day | Week | Month |
  All` that just set `?window=`, active one highlighted — no JS needed).
- **Section: Lifecycle** — tiles for Queued, Running, Finished,
  Cancelled, Timed out. Reuse the `badge-*` colors via the tile value
  color. Each tile links to `/test-runs?lifecycle=<state>` (confirm the
  runs filter accepts a lifecycle param; add it if missing — see §5).
- **Section: Outcomes** — Passed / Failed / Errors / Skipped, linking to
  `/test-runs?result=<code>`.
- **Section: Workers** — Connected / Total, Busy, Free capacity. Links to
  `/workers`.
- **Section: Inventory** — Tests, Matrices, Matrix Runs, Projects, Rules,
  OSes, Compilers; each links to its list page.
- Drop the Recent Runs table (moves to Queue, §4).
- Add a `.stat-section` wrapper + heading style to `base.html` (a labeled
  row of `.stat` tiles). Keep the existing `.stats`/`.stat` classes.

Every tile value should be a link so the dashboard doubles as navigation.
Carry the active `window` into the lifecycle/outcome tile links so
drilling down stays scoped (only if the runs list supports a date filter;
otherwise omit window from those links to avoid implying a filter that
isn't applied).

### 4. Queue → Activity page ([`queue.html`](../../opp_ci/web/templates/queue.html), [`app.py:261`](../../opp_ci/web/app.py))

- Keep Running and Queued sections.
- Add a **Recently finished** section: last N (e.g. 20) runs with
  `lifecycle in (finished, cancelled, timed_out)` ordered by
  `finished_at desc`, using the same columns as the old dashboard table
  (ID, Project, Kind, Status badge, Duration, Finished-at).
- Add a small header summary line: `running / queued / recently
  finished` counts (mirrors the workers page header style).
- Update the nav label from "Queue" to "Activity" in
  [`base.html`](../../opp_ci/web/templates/base.html) (route can stay
  `/queue`; optionally add `/activity` later). Page `<h2>` already says
  "Active Tests" — rename to "Activity".

### 5. Runs-list filter support (dependency check)

The dashboard tiles link into `/test-runs` with a lifecycle/outcome
filter. Verify `runs_list` ([`app.py:404`](../../opp_ci/web/app.py))
accepts `lifecycle=` and `result=` query params. If a lifecycle filter
isn't already present, add one (string filter on `TestRun.lifecycle`).
This keeps the tiles as real drill-downs rather than dead numbers.

### 6. Shared helpers / cleanup

- Extract `worker_summary(session)` and reuse in both `dashboard` and
  `workers_list`.
- Keep all aggregation server-side (group_by), not per-bucket loops, so
  the dashboard stays one round-trip-ish even as data grows.

## Out of scope (note for later)

- Trend charts / sparklines over time (the window selector gives point-in-
  time scoping; graphs are a separate effort).
- Auto-refresh / live updates on the dashboard or Activity page.
- Per-project or per-OS breakdown tables on the dashboard (link out to the
  existing list pages instead).

## Test / verification

- Unit-ish: assert the window helper maps day/week/month/all correctly and
  unknown → all.
- Route: hit `/` with each `window` value; assert lifecycle/outcome counts
  shrink as the window narrows on seeded data, while inventory counts stay
  constant.
- Queue/Activity: assert a finished run appears in "Recently finished" and
  not in Running/Queued.
- Manual: load the dashboard with the mm1k/local DB, click each tile, and
  confirm it lands on the correctly filtered list page.

## Suggested commit breakdown

1. `web: add time-window helper + lifecycle/outcome group-by aggregates`
2. `web: regroup dashboard into Lifecycle/Outcomes/Workers/Inventory tiles`
3. `web: add window selector to dashboard`
4. `web: move recent runs to Queue page as "Recently finished"; rename to Activity`
5. `web: add lifecycle filter to runs list` (if not already present)
