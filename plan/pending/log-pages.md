# Log pages in the web UI

Add web pages to view the **serve** (coordinator) log and the **local
worker** logs, sourced from **systemd-journald**, with live tail /
auto-refresh and ANSI colour rendering (reusing the existing
`ansi_to_html` filter).

## Motivation

Today the only way to read what `opp_ci serve` or `opp_ci worker start`
is doing is on the host: `journalctl -u opp_ci-serve` /
`journalctl -u opp_ci-worker@<name>`. Per-run `stdout`/`stderr` are
already viewable on
[`run_detail.html`](../../opp_ci/web/templates/run_detail.html), but the
*process* logs (startup, polling, heartbeats, scheduling, errors that
never reach a run record) are invisible from the UI. The operator wants:

- a **worker page** view of each local worker's process log, and
- a **separate page** for the serve log.

## Source of truth: journald, not files

Both processes run as **system units owning the same `opp_ci` user**
([systemd-service plan](../done/systemd-service.md)):

- `opp_ci-serve.service` → `opp_ci serve`
- `opp_ci-worker@<name>.service` → `opp_ci worker start` (one instance
  per registered worker; the instance name **is** the worker name)

systemd already captures each process's full stdout/stderr into the
journal, per unit, with rotation and retention — the systemd plan even
notes "`/var/log/opp_ci/` reserved; **journald is primary**." Reading the
journal is therefore strictly better than adding a `RotatingFileHandler`:

- **No capture layer.** No new config, no log dir, no handler wiring, no
  rotation logic. Zero changes to `cli.py` / `worker.py`.
- **More faithful.** The journal has the *raw* process output — uvicorn's
  own access/error loggers, stray `print()`s, and startup tracebacks that
  crash before any Python `logging` handler is attached. A file handler
  bolted onto the `logging` module would miss exactly those.
- **A native incremental-tail primitive.** `journalctl --after-cursor`
  returns only new entries since an opaque cursor — rotation-safe, no
  byte-offset bookkeeping.

The previous file-based draft is dropped. (File capture only matters for
non-systemd environments — see "Fallbacks" below.)

## Decisions

1. **Read journald via `journalctl -o json`.** Shell out, parse one JSON
   object per line, keep each entry's `__CURSOR` for the next poll. JSON
   output gives us `MESSAGE`, `PRIORITY`, `__REALTIME_TIMESTAMP`, and
   `__CURSOR` in one shot.
2. **Grant the serve process journal read access via the unit, not the
   user.** Add `SupplementaryGroups=systemd-journal` to
   `opp_ci-serve.service`. System-unit logs live in the system journal,
   readable only by root / `systemd-journal` / `adm`; the supplementary
   group scopes read access to the serve process without touching the
   `opp_ci` account's global groups. (Alternative for ad-hoc installs:
   `usermod -aG systemd-journal opp_ci`.)
3. **A Logs hub at `/logs`** (option B) lists every source — *Serve* plus
   each registered worker — each linking to its own viewer page
   (`/logs/serve`, `/logs/worker/{id}`). One discoverable nav entry,
   symmetric treatment of serve and workers, scales as workers are added.
   The [`/workers`](../../opp_ci/web/app.py#L2172) list rows also link
   straight to the matching worker viewer.
4. **Live update by polling, not streaming.** No SSE/websocket infra
   exists; polling the cursor endpoint every ~2 s is proportionate and
   matches existing conventions.
5. **Session-cookie auth, submitter+.** The viewers are browser pages
   polling with the session cookie, so the tail endpoints live on the
   **web router** (cookie auth via `require_user("submitter")`), *not* the
   `/api` bearer router. Process logs can leak tokens/paths, so `readonly`
   users are excluded — but operators at `submitter` and above (not just
   `admin`) can read them.

## Plan

### 1. Journal reader helper

New module `opp_ci/journal.py`, pure-ish and unit-testable by mocking the
subprocess:

```python
def read_unit(unit, *, cursor=None, lines=1000, timeout=10):
    """Return (entries, last_cursor, available).
    - cursor None  → last `lines` entries (initial load).
    - cursor given → entries strictly after it (incremental poll).
    entries: list of {"ts": datetime, "priority": int, "message": str, "cursor": str}.
    available False when journalctl is missing / errors / access denied
    (e.g. dev checkout, no systemd) — caller renders an "unavailable" notice."""
```

Command shape:

```
journalctl --no-pager -o json -u <unit> \
    [ --after-cursor <cursor> | -n <lines> ]
```

Parsing notes:
- `MESSAGE` may be a string or, for non-UTF-8 lines, an array of byte
  values — handle both (decode array → bytes → utf-8 with `errors="replace"`).
- `__REALTIME_TIMESTAMP` is microseconds since epoch → `datetime`.
- `PRIORITY` (0–7 syslog levels) → optional level colouring in the viewer.
- Empty stdout (no new entries) is the normal incremental case → return
  `[]` and the unchanged cursor.

### 2. Unit-name mapping

- Serve: constant `opp_ci-serve.service`.
- Worker: a `Worker` row's `name` → `opp_ci-worker@<name>.service`,
  interpolated **verbatim** — the deployment already uses the worker name
  directly as the systemd instance (`opp_ci-worker@local`,
  `workers/local.env`), so no `systemd-escape` round-trip is needed or
  correct. Centralise as `worker_unit_name(name)` so page and tail agree.
- **Guard the charset**: a name with `/`, whitespace, or control chars
  could never be a valid instance in this deployment anyway. Reject such
  names (→ `available:false`, reason) rather than feed a bogus unit to
  `journalctl`. (Args are passed as a list, not a shell string, so this is
  belt-and-suspenders, not an injection fix.)

Make the unit names configurable — `OPP_CI_SERVE_UNIT` (default
`opp_ci-serve.service`) and `OPP_CI_WORKER_UNIT_TEMPLATE` (default
`opp_ci-worker@{instance}.service`) — so a non-standard install can point
at differently-named units.

### 3. Tail endpoints (web router, cookie auth — in [`app.py`](../../opp_ci/web/app.py))

```
GET /logs/serve/tail?cursor=<str>            → require_user("submitter")
GET /logs/worker/{worker_id}/tail?cursor=<str>  → require_user("submitter")
```

These return `JSONResponse` (not the bearer `/api` router — the browser
polls with the session cookie). Each returns:

```json
{ "available": true, "entries": [ {"ts": "...", "priority": 6, "html": "..."} ],
  "cursor": "<opaque>", "reason": null }
```

- The worker endpoint resolves `worker_id` → `Worker.name` →
  `worker_unit_name(...)`; 404 if the worker row is unknown.
- `available:false` + `reason` when journalctl is unavailable or access
  is denied, so the UI explains *why* rather than showing an empty box.
- Render each `MESSAGE` to safe HTML **server-side**: escape first, then
  run through the existing `_ansi_to_html`
  ([`app.py:63`](../../opp_ci/web/app.py#L63)) so colour codes show and no
  raw markup is injected. The browser just appends `html`.

### 4. Web pages

All on `web_router`, gated with `require_user("submitter")`:

- `GET /logs` → **hub** (`logs.html`): a table/list of sources — *Serve*
  (→ `/logs/serve`) and each `Worker` (→ `/logs/worker/{id}`, showing
  connected/offline status alongside).
- `GET /logs/serve` and `GET /logs/worker/{id}` → **viewer**
  (one shared `log_view.html`, parameterised with a title and the tail
  URL). 404 if the worker id is unknown.

The viewer template carries the log pane + a small JS poller that

1. on load, GETs the tail URL with no cursor (last N lines),
2. every ~2 s GETs with the stored cursor, appends returned `html`
   entries, updates the cursor, and auto-scrolls when following,
3. shows the `reason` banner when `available:false`.

Controls: a "follow tail" toggle and pause/resume. Optional: tint each
line by `priority` using the existing `--warn`/`--error` colours from
`base.html`.

### 5. Navigation

Add a **Logs** entry in
[`base.html`](../../opp_ci/web/templates/base.html) → `/logs` (render
conditionally on `current_user.role in ["submitter", "admin"]`, before the
admin-only `Admin` link). Each row in `workers.html` also links to
`/logs/worker/{id}`, so worker logs are reachable from both the hub and
the Workers list.

### 6. Deployment

One unit change: add `SupplementaryGroups=systemd-journal` to
`opp_ci-serve.service` (decision 2). Document it and the
`OPP_CI_SERVE_UNIT` / `OPP_CI_WORKER_UNIT_TEMPLATE` overrides alongside
the other `OPP_CI_SERVE_*` vars. No worker-side change.

## Fallbacks (out of scope, noted)

- **Non-systemd / dev checkout** (`opp_ci serve` from a shell): there is
  no journal, so `read_unit` returns `available:false` and the page shows
  "log viewing requires systemd". Acceptable — production is systemd. A
  file-handler backend behind the same `read_unit` interface could fill
  this gap later if wanted.
- **Remote workers**: a worker on another host logs to *that* host's
  journal, which the serve box can't read. The per-worker page would show
  `available:false`. Cross-host log viewing would need the worker to
  *ship* lines to the coordinator (a `logging.Handler` POSTing to a new
  `/api/workers/log`, buffered server-side) — a separate feature; the
  page/API shape here is source-agnostic so it can slot in.

## Testing

- `read_unit`: initial load (`-n`), incremental (`--after-cursor`),
  empty-output case, `MESSAGE`-as-byte-array decoding, timestamp parse,
  `available:false` when `journalctl` is missing / returns non-zero — all
  by mocking `subprocess`.
- `worker_unit_name`: verbatim mapping for normal names; charset guard
  rejects `/`, spaces, control chars.
- Endpoints: auth enforced (submitter+ ok, readonly → 403), unknown worker →
  404, cursor round-trips, `available/reason` shape.
- HTML safety: a `MESSAGE` containing `<script>` and ANSI codes is
  escaped *and* colourised (no raw markup in output).
- One integration smoke test on a host with the units running: serve and
  worker pages return live entries.

## Open questions

- **Worker not yet started**: `opp_ci-worker@<name>.service` may have no
  journal entries (registered but never run). Show "no log entries"
  rather than an error — distinguish from access-denied.
- **Cursor staleness**: if the journal rotates past a held cursor,
  `--after-cursor` may return nothing or error. Detect and fall back to a
  fresh `-n` load. Worth confirming `journalctl`'s exact behaviour here.
- **Download / full log**: add a "download" link (stream a larger
  `journalctl -o cat -n N`) alongside the tail view?
- ~~**Per-unit base names**~~: confirmed on the coordinator —
  `opp_ci-serve.service` and `opp_ci-worker@local.service` (the single
  registered worker is named `local`), so the hardcoded defaults are
  correct.
