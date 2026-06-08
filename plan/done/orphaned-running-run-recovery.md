# Recover orphaned `running` TestRuns / wedged TestMatrixRuns

## Problem

When an opp_ci worker stops or crashes mid-job, its `TestRun`s stay in
`lifecycle=running` indefinitely, and the parent `TestMatrixRun` stays
perpetually incomplete (`completed_at` NULL, verdict unfinalized, GitHub
check stuck pending).

The only existing recovery path — re-queueing orphans in
[auth.py:107-122](../../opp_ci/auth.py#L107-L122) — is gated on
`worker.status == "offline"`. But the `status` column is written to
`"offline"` **only at worker registration**
([api.py:365](../../opp_ci/web/api.py#L365),
[app.py:2136](../../opp_ci/web/app.py#L2136)); every other path sets
`"online"`/`"busy"`, and **nothing flips a live worker back to
`offline`**. `WORKER_HEARTBEAT_TIMEOUT` is used only for a derived UI
badge ([app.py:1850-1861](../../opp_ci/web/app.py#L1850-L1861)), never
persisted. So:

- Worker crashes and never returns → runs stuck `running` forever.
- Worker crashes and **restarts** → status was `online`/`busy`, so the
  re-queue block never fires; runs still stuck forever, and the
  heartbeat job-count reconciliation
  ([api.py:407-419](../../opp_ci/web/api.py#L407-L419)) keeps the ghost
  runs counted, pinning the worker `busy` so it stops polling.
- Coordinator restarts → `serve` does only `create_all`, no
  reconciliation ([cli.py:941+](../../opp_ci/cli.py#L941)).

Net: the re-queue path is effectively dead code in normal operation.

## Goal

Orphaned `running` runs are automatically detected and re-queued within
a bounded time of the owning worker going dark, with no manual SQL.
Three independent triggers, all sharing one reclaim primitive:

1. **Heartbeat-timeout reaper** (primary) — periodically flip workers
   whose `last_heartbeat` is older than `WORKER_HEARTBEAT_TIMEOUT` to
   `offline` and reclaim their `running` runs.
2. **Startup reconciliation** — on coordinator boot, reclaim runs owned
   by any already-stale/offline worker.
3. **On-reconnect** (existing) — keep, but make it correct by routing
   through the shared primitive so it fires for genuinely-stale workers.

## Design

### 1. Shared reclaim primitive — `persistence.py`

Extract the re-queue body from `auth.py` into a reusable function so all
three triggers behave identically:

```python
def reclaim_orphaned_runs(session, worker_id, now, max_reclaims):
    """Reclaim every TestRun left `running` on worker_id. Each is either
    re-queued for another attempt or, once it has burned through
    max_reclaims attempts, retired to a terminal `timed_out` state (see
    "Poison-pill handling"). Returns (requeued, retired). Does NOT commit
    — caller owns the transaction."""
    orphans = session.execute(
        select(TestRun).where(
            TestRun.worker_id == worker_id,
            TestRun.lifecycle == TestRunLifecycle.running,
        )
    ).scalars().all()
    requeued = retired = 0
    for run in orphans:
        run.reclaim_count = (run.reclaim_count or 0) + 1
        if run.reclaim_count > max_reclaims:
            retire_poison_run(session, run, now)   # terminal, see below
            retired += 1
        else:
            run.lifecycle = TestRunLifecycle.queued
            run.worker_id = None
            run.started_at = None
            # running->queued keeps the matrix cell non-finished, so the
            # parent rollup's completed_at stays NULL either way.
            requeued += 1
    return requeued, retired
```

```python
def mark_stale_workers_offline(session, now, timeout_seconds):
    """Flip online/busy workers whose last_heartbeat is older than the
    timeout to `offline`, reclaim their running runs, zero job count.
    Returns list of (worker_name, reclaimed_count). Does NOT commit."""
    threshold = now - timedelta(seconds=timeout_seconds)
    stale = session.execute(
        select(Worker).where(
            Worker.status.in_(("online", "busy")),
            or_(Worker.last_heartbeat.is_(None),
                Worker.last_heartbeat < threshold),
        )
    ).scalars().all()
    results = []
    for w in stale:
        requeued, retired = reclaim_orphaned_runs(
            session, w.id, now, max_reclaims)
        w.status = "offline"
        w.current_job_count = 0
        results.append((w.name, requeued, retired))
    return results
```

`mark_stale_workers_offline` gains a `max_reclaims` parameter, threaded
through from config to `reclaim_orphaned_runs`.

Note: a freshly-registered worker has `last_heartbeat=NULL` and
`status="offline"`, so it is *not* matched by the `online/busy` filter —
the reaper won't churn pre-connect workers.

### 2. Background reaper task — `web/app.py`

The app runs under a single `uvicorn.run(app)` process, so an asyncio
background task is sufficient (no cross-process coordination needed).

Add a FastAPI `lifespan` handler to `app = FastAPI(...)`
([app.py:102](../../opp_ci/web/app.py#L102)) that:

- **On startup**: open a session, call `mark_stale_workers_offline(...)`
  once (covers coordinator-restart reconciliation), commit, log counts.
- Spawn an `asyncio.create_task` loop that every
  `WORKER_HEARTBEAT_TIMEOUT / 2` seconds (configurable, see below) runs
  `mark_stale_workers_offline`, commits, and logs any reclaims. Wrap the
  body in try/except so a transient DB error doesn't kill the loop.
- **On shutdown**: cancel the task and await it.

Reaper cadence: derive from a new
`OPP_CI_WORKER_REAP_INTERVAL` (default `WORKER_HEARTBEAT_TIMEOUT // 2`,
min 15s). Worst-case detection latency = timeout + interval.

Run the session work via `run_in_executor` (SQLAlchemy sync session)
or a short-lived `SessionLocal()` inside `asyncio.to_thread` to avoid
blocking the event loop.

### 3. Startup reconciliation — already covered

The lifespan startup call in (2) handles this. No change to
`cli.py serve` needed beyond it being the entry that imports the app.
(If `serve` and the ASGI app can be launched separately, put the
reconciliation in the lifespan handler — not `serve` — so it always
runs.)

### 4. Simplify the on-reconnect path — `auth.py`

Replace the inline re-queue block
([auth.py:107-122](../../opp_ci/auth.py#L107-L122)) with a call to
`reclaim_orphaned_runs(session, worker.id)` when
`worker.status == "offline"`, then set `online` / zero the count. This
keeps the existing behaviour for a worker that the reaper just flipped
offline and which then reconnects — it reclaims (idempotent: the reaper
already reclaimed, so this is usually a no-op) and comes back online
cleanly. Behaviour preserved, logic de-duplicated.

## Config (`config.py`)

- Reuse `WORKER_HEARTBEAT_TIMEOUT` (120s default) as the staleness bound.
- Add `OPP_CI_WORKER_REAP_INTERVAL` (default `max(15, TIMEOUT // 2)`).

## Edge cases / decisions

- **In-flight result race**: a worker reported stale by the reaper may
  still be alive and POST a result for a run that was just re-queued.
  `worker_report_result` must tolerate a run that is no longer `running`
  on this worker — guard it to ignore/log a result whose
  `run.worker_id != reporter` or `lifecycle != running` (check current
  behaviour at [api.py:574+](../../opp_ci/web/api.py#L574) and harden if
  needed). This is the one correctness-sensitive spot.
- **Re-queued run re-runs from scratch**: acceptable — tests are
  expected to be idempotent; partial output from the crashed attempt is
  discarded (we clear `started_at`; outcome columns were never written).
- **Retry/poison-pill**: in scope — see the dedicated section below.
- **TestMatrixRun**: no direct change. Once children are re-queued and
  eventually finish, `recompute_matrix_run_rollup`
  ([persistence.py:307](../../opp_ci/persistence.py#L307)) sets
  `completed_at`/verdict normally. Confirm the rollup is invoked after
  the re-run finishes (it is, via `finalize_verdict_for_run`).

## Testing

- Unit: `mark_stale_workers_offline` flips only online/busy + stale,
  reclaims their runs, leaves fresh and offline-registered workers
  alone; `reclaim_orphaned_runs` clears worker_id/started_at and returns
  count.
- Integration: register worker → poll job (run `running`) → simulate
  staleness (backdate `last_heartbeat`) → run reaper → assert run
  `queued`, worker `offline`, `current_job_count==0` → another worker
  polls and gets it.
- Integration: result POST for an already-reclaimed run is ignored
  cleanly (no crash, no double-finalize).
- Startup: seed a `running` run on a stale worker, boot app, assert it's
  reclaimed.

## Files touched

- `opp_ci/persistence.py` — new `reclaim_orphaned_runs`,
  `mark_stale_workers_offline`.
- `opp_ci/web/app.py` — `lifespan` startup reconcile + reaper task.
- `opp_ci/auth.py` — route re-queue through shared primitive.
- `opp_ci/web/api.py` — harden `worker_report_result` against
  reclaimed/foreign runs.
- `opp_ci/config.py` — `OPP_CI_WORKER_REAP_INTERVAL`.
- tests — as above.
