# Remote worker log view

Make the web UI's per-worker log view (and live run-watching) work for
workers running on a **different host** than the coordinator.

Two features, to be done in this order:

1. **Worker log shipping** (the "simplified Option 1") — workers ship
   their recent log lines to the coordinator on each heartbeat; the
   existing log-view pages serve them from an in-memory buffer.
2. **Live per-run test output** (the "Option 5") — stream a running
   test's stdout/stderr to the coordinator during execution so the
   run-detail page can live-tail it.

Feature 1 is the high-leverage, cheap one and delivers most of the
"watch a remote run progress" value on its own; feature 2 is a
presentation upgrade layered on top.

> **Status:** Feature 1 implemented (`opp_ci/logbuffer.py`,
> `opp_ci/worker_logs.py`, worker heartbeat shipping, coordinator ingest +
> `worker_log_tail` source selection, `tests/test_worker_log_shipping.py`).
> Feature 2 not started. Plan stays in `pending/` until feature 2 lands.

---

## Background — why it's broken today

The log view is entirely **coordinator-local**. Both tail endpoints shell
out to `journalctl -u <unit>` on the coordinator host
([app.py:2298-2340](../../opp_ci/web/app.py), via
[journal.py:read_unit](../../opp_ci/journal.py)). That only sees journals
on the coordinator box, so a worker on another host shows
`available:false`. This was scoped out deliberately; the plan doc
[log-pages.md](../done/log-pages.md) kept the page/API contract
(`{available, reason, entries[], cursor}`) source-agnostic precisely so a
different backend can slot in.

Worker↔coordinator traffic is **outbound HTTP only** (poll / heartbeat /
result — [worker.py:130-167](../../opp_ci/worker.py)); workers run no
inbound server and may sit behind NAT. So the log path must be
**worker-push**, not coordinator-pull.

### Key finding: streamed output already flows through Python logging

The slow commands run with `stream=True`, and
[`_run_external_streaming`](../../opp_ci/executor.py) tees every child line
via `_logger.info("[%s] %s", label, line)` — i.e. through the `opp_ci`
logger, not raw stdout. The streamed call sites already cover the real
remote execution paths:

- `_run_test_via_opp_env` (host/nix) — `run_external(..., stream=True)`
- `_run_test_in_podman` — `run_external(podman_cmd, ..., stream=True)`
- podman build / bake / compile steps

Consequence: an **in-process `logging.Handler`** attached to the `opp_ci`
logger captures both the worker daemon's own records *and* the live
build/compile/test output — with no `journalctl` subprocess and no
`SupplementaryGroups=systemd-journal` needed on the worker. This is the
source we use for feature 1.

The one path NOT covered by the logger is `_run_test_direct` (in-process
opp_repl, captured to `StringIO` — [executor.py:1252](../../opp_ci/executor.py)).
Its output is not live-logged; feature 2 handles that path explicitly.

---

## Feature 1 — Worker log shipping

### Worker side

- Add a bounded in-memory ring-buffer logging handler (new
  `opp_ci/logbuffer.py`, `RingBufferHandler`) holding the last
  `OPP_CI_WORKER_LOG_RING` records (default ~2000). Each record is
  stored as `{seq, ts, level, message}` with a process-local monotonic
  `seq` (a plain incrementing int — no journald cursor needed; append
  order is authoritative).
- Install it on the `opp_ci` logger when `opp_ci worker start` boots
  (in `WorkerAgent.run`/`__init__`), at the same level the worker logs
  at. It must capture `_logger`-emitted lines from every module,
  including the executor's streamed lines.
- Track `last_shipped_seq`. On each heartbeat, take records with
  `seq > last_shipped_seq`, cap at `OPP_CI_WORKER_LOG_BATCH` (default
  ~500) most-recent, and include them in the heartbeat POST body:
  ```json
  { "logs": { "entries": [{"seq","ts","level","msg"}, ...],
              "dropped": <int> } }
  ```
  `dropped` = count skipped when more than the cap accumulated between
  beats (so the UI can show a "… N lines dropped …" marker rather than
  silently lying). Advance `last_shipped_seq` only after a 200.
- Heartbeat already posts with no body
  ([worker.py:_heartbeat](../../opp_ci/worker.py)); add the JSON body
  there. Keep it best-effort — log shipping must never break heartbeat.

Latency note: cadence = heartbeat interval (~30s), so the remote tail is
chunky, not smooth. Acceptable for v1. If smoother is wanted later, give
shipping its own short timer instead of piggybacking heartbeat (a config
knob); out of scope here.

### Coordinator side

- In-memory per-worker ring buffer (new module-level store, e.g.
  `opp_ci/worker_logs.py` with a `WorkerLogStore` keyed by `worker_id`),
  bounded to `OPP_CI_SERVE_WORKER_LOG_RING` (default ~2000) lines each,
  each line tagged with a coordinator-assigned monotonic `seq`. Purely
  in-memory — lost on `serve` restart, which is fine.
- `worker_heartbeat` ([api.py:494](../../opp_ci/web/api.py)) reads the
  optional `logs` field and appends to the store under
  `worker_info["worker_id"]`. Backward compatible: old workers send no
  body, store stays empty, page falls back (below).

### Serving — reuse the existing contract

- `worker_log_tail` ([app.py:2326](../../opp_ci/web/app.py)) chooses its
  source:
  1. If the store has lines for this worker → serve from the ring buffer,
     using the coordinator `seq` as the opaque `cursor` (entries with
     `seq > cursor`). Render through `_render_log_entries` /
     `_ansi_to_html` exactly as today.
  2. Else fall back to the current local `journalctl` path (keeps the
     co-located `local` worker working, and gives nothing-yet workers a
     sensible `available:false`/empty).
- `_render_log_entries` currently expects journald-shaped dicts
  (`ts` as datetime, `priority`). Add a small adapter so ring-buffer rows
  (string ts, Python log level) render identically. UI template unchanged.

### Config (config.py)

- `OPP_CI_WORKER_LOG_RING` (worker ring size, default 2000)
- `OPP_CI_WORKER_LOG_BATCH` (per-heartbeat cap, default 500)
- `OPP_CI_SERVE_WORKER_LOG_RING` (coordinator per-worker ring, default 2000)

### Tests

- `RingBufferHandler`: capture, ordering, bound/eviction, seq monotonic,
  `since(seq)` slice, dropped-count when over cap.
- Heartbeat round-trip: worker batches new-since-last, advances only on
  200, re-sends on failure; coordinator appends; old-worker (no body)
  path unaffected.
- `worker_log_tail` source selection: buffer present → buffer; absent →
  journalctl fallback; cursor round-trips; `available/reason` shape held.
- ANSI/HTML safety preserved through the adapter (`<script>` + colour).

---

## Feature 2 — Live per-run test output

Goal: run-detail page live-tails the running test's stdout/stderr,
attributed to the run (not the worker), and covers the in-process
`_run_test_direct` path that feature 1's logger source misses.

### Executor

- Thread an optional `on_output(chunk)` callback into `run_test` →
  the three `_run_test_*` helpers.
  - For the streamed external paths, tee from the same per-line pump that
    already feeds `_logger` (`_run_external_streaming`'s `emit`).
  - For `_run_test_direct`, tee the `StringIO` writes.
- Callback is best-effort and must not affect the returned
  `result.stdout`/`stderr` (final report path unchanged).

### Worker

- In `_execute`, pass an `on_output` that appends chunks to a per-run
  buffer and ships them incrementally — batched on the heartbeat timer
  (or its own short timer), keyed by `run_id`, to a new endpoint
  `POST /api/runs/{run_id}/output-append` (worker-token auth) carrying
  `{seq, text}`. Final authoritative stdout/stderr still goes via
  `_report_result` at completion (single source of truth on finish).

### Coordinator

- In-memory live-output store keyed by `run_id` (bounded ring, evicted
  when the run reaches a terminal lifecycle or on a TTL). Append on
  `output-append`.
- New `GET /runs/{run_id}/output/tail` returning the same
  `{available, entries[], cursor}` shape.
- run_detail.html ([run_detail.html:112](../../opp_ci/web/templates/run_detail.html)):
  while `lifecycle == running`, show a live pane polling the tail
  endpoint (reuse the `log_view.html` polling JS / partial). Once
  finished, show the stored `TestRun.stdout`/`stderr` as today.

### Tests

- `on_output` fires for all three paths; doesn't alter final
  stdout/stderr; safe when callback is None.
- `output-append` append/evict-on-terminal; tail cursor round-trip;
  run_detail switches live→static at completion.

---

## Sequencing & scope

1. Feature 1 first — smaller, self-contained, immediate remote
   run-watching. Ship and use it before deciding feature 2's final shape.
2. Feature 2 second — only genuinely adds value for the in-process
   `_run_test_direct` path and for run-attributed (vs worker-attributed)
   output; re-confirm it's wanted after feature 1 lands.

Out of scope: persistence across `serve`/worker restarts (both stores are
in-memory by design), sub-heartbeat streaming latency, and any
coordinator→worker pull (SSH/HTTP) — the push model is deliberate.

## Open questions

- **Buffer on restart**: coordinator restart drops buffered worker logs;
  next heartbeat only re-ships `> last_shipped_seq`, so the pane is briefly
  empty until the worker's seq advances. Acceptable, or have the worker
  reset `last_shipped_seq` to "whole ring" when it sees a fresh coordinator
  (e.g. heartbeat response carries a serve-start nonce)? Lean: accept it
  for v1.
- **Local worker uniformity**: keep journalctl for the co-located `local`
  worker (richer, persistent) and only buffer-serve remotes, or move
  everything to the shipped-log path for consistency? Lean: keep the
  fallback so `local` stays on journalctl.
- **Level mapping**: Python log levels vs the template's syslog-style
  `priority` colouring — pick a small mapping in the adapter.
