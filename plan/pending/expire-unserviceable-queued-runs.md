# Expire unserviceable queued runs

## Problem

A queued `TestRun` whose required tags no worker advertises hangs in the
queue forever. `worker_poll` only ever hands a run to a worker whose tags
are a superset of the run's required set (`_worker_can_run`); a misrouted
submission (wrong tags, or a worker type never deployed) is simply never
claimed. The heartbeat reaper (`mark_stale_workers_offline`) only recovers
`running` runs whose worker went dark — it never looks at `queued` runs, so
there is no feedback and no terminal state for a stuck queue entry.

## Decision (locked)

- **One tier only: unserviceable expiry.** Expire a queued run only when
  *no enabled worker's tags satisfy it* and it has been queued longer than
  `QUEUE_UNSERVICEABLE_TIMEOUT`. Satisfiable-but-starved runs (right tags,
  fleet busy/offline) are left to wait or be manually cancelled — no blind
  max-age tier.
- **Satisfiability counts enabled workers of any status.** A busy, offline,
  or rebooting worker still "covers" the run; status flaps with the
  heartbeat, so only true misroutes (no enabled worker matches at all) are
  reaped. Avoids draining the queue during a fleet outage.
- **Terminal state: `timed_out` + ERROR**, mirroring `retire_poison_run`,
  with a message naming the unsatisfiable tag set. Calls
  `finalize_verdict_for_run` so the matrix cell resolves and the parent
  `TestMatrixRun` can complete (same wedge bug the poison-pill path fixes).
- **Default timeout: 300s** (longer than a worker's register/first-heartbeat
  window, short enough that a bad submission fails visibly in minutes).
  `0` disables the sweep.

## Changes

- **persistence.py**
  - Move `_required_tags_for_test` (new name for the required-set part of
    `_worker_can_run`) and `_platform_required_tag` down from web/api.py.
  - Add `expire_unserviceable_queued_runs(session, now, workers,
    timeout_seconds)` → count expired. Skips when `timeout_seconds <= 0`.
    For each `queued` run older than the threshold whose required tags are
    not a subset of any enabled worker's tags: terminal-fail it like
    `retire_poison_run` (timed_out/ERROR + finalize). Caller owns the txn.
- **web/api.py** — `_worker_can_run` / `_platform_required_tag` call the
  moved helpers. Pure refactor; existing poll/tag tests stay green.
- **config.py** — `QUEUE_UNSERVICEABLE_TIMEOUT`
  (`OPP_CI_QUEUE_UNSERVICEABLE_TIMEOUT`, default 300, `0` disables).
- **web/app.py** — in `_reap_stale_workers`, after
  `mark_stale_workers_offline`, load enabled workers and run the queued
  sweep in the same session/commit. Reuses `_reaper_loop` + startup
  reconciliation — no new task or interval. Order matters: read worker
  rows by `enabled` (not `status`) so a worker the reaper just flipped
  `offline` still counts toward satisfiability.
- **tests/test_queue_expiry.py** — unserviceable run expired;
  serviceable-but-no-online-worker run left alone; matrix rollup completes
  after expiry; `timeout=0` disables.
