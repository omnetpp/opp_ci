# Worker run-filters: opt out of Tests by isolation / toolchain (and any coordinate axis)

## Problem

A worker that advertises the `podman` / `nix` capability tags today both **can**
and **will** run those jobs — capability and willingness are the same gate. The
only matcher is

```
required_tags_for_test(test) ⊆ worker.tags          # persistence.py:859, api.py:675
```

There is no way for an operator to say *"this host is capable of Podman runs but
I don't want it spending its cycles on them"* or *"this host should only ever run
containerized jobs, never bare-metal."* Two things make negative tags insufficient:

1. The **default axis values** `isolation=none` and `toolchain=none` carry **no
   required tag at all** (`required_tags_for_test`, persistence.py:870-885), so
   there is nothing to exclude — you cannot express "don't run bare-metal."
2. Capability tags are a *union* membership test; subtracting one would also make
   the worker fail genuine capability checks elsewhere.

We want a **willingness filter** that is orthogonal to the capability tag set: a
worker can be capable of an axis value yet decline it.

## Goal

Give each worker an optional, DB-stored, admin/CLI-editable **run-filter** that
restricts which Tests it will be assigned, expressed **per coordinate axis** as
either an allow-list or a deny-list. Surface `isolation` and `toolchain` as
first-class CLI flags; keep the underlying mechanism general over any coordinate
axis (compiler, os, arch, mode, kind, …).

## Decisions (locked)

1. **Semantics: both, per axis.** Each axis may carry *either* an `allow` set
   *or* a `deny` set — whichever is present wins. Setting both for the same axis
   is a validation error. An axis with no entry is unconstrained (runs everything
   the worker is capable of). This keeps the user's "opt-out" framing (deny) while
   allowing the safer "only these" framing (allow) where an operator prefers it.
2. **Storage: DB per-worker, like `tags`.** A new `Worker.run_filters` JSON
   column. Set at `worker register`, edited via `worker update` and the admin
   `PATCH /workers/{id}` endpoint / web worker page. Central visibility lets the
   coordinator's unserviceable sweep reason about willingness even for workers
   that are momentarily offline.
3. **Scope: general mechanism, isolation+toolchain surfaced.** `run_filters` is
   keyed by coordinate-field name and works for any axis in `TEST_COORD_FIELDS`.
   The CLI exposes `--accept-isolation/--deny-isolation` and
   `--accept-toolchain/--deny-toolchain` as ergonomic shortcuts, plus a general
   `--run-filter AXIS=allow:v1,v2 | AXIS=deny:v1` escape hatch for the rest.
4. **Willingness is a second, independent gate — never weakens capability.** A
   worker may serve a test iff it **can** (tags ⊇ required) **and is willing**
   (passes its run-filters). Both must hold.
5. **Fleet resolution stays capability-based (v1).** `fleet.py` pins loose axes
   from *advertised tags*, not willingness. Run-filters do not steer resolution.
   (See Risks — documented, with a future-knob note. Note isolation/toolchain are
   not fleet-resolved anyway; they default to `["none"]` in the scheduler.)

## Data model

`run_filters` shape — a dict keyed by coordinate-axis name:

```json
{
  "isolation": {"allow": ["none"]},
  "toolchain": {"deny": ["nix"]}
}
```

- Empty/absent dict (`{}`) → no filtering → current behavior (backward compatible).
- Each value object has **exactly one** of `allow` / `deny`, a non-empty list of
  strings.
- Axis key must be a member of `TEST_COORD_FIELDS` (db/models.py:232).

### Axis value normalization

The value compared against the filter is the test's **effective** axis value:

- `isolation` / `toolchain`: `getattr(test, axis) or "none"` (mirrors the existing
  default-to-`"none"` convention in `required_tags_for_test`, persistence.py:870-871).
- Other axes: the raw string value; a `None` coordinate is treated as the literal
  `"none"` for comparison so it behaves predictably (never silently matches a
  concrete allow-list; never trips a concrete deny-list).

This normalization lives in one helper so the poll matcher, the unserviceable
sweep, and any future web preview all agree.

## Implementation

### Phase 1 — schema + core predicate (pure, well-tested)

- **`db/models.py`**: add `run_filters = Column(JSON, default=dict)` to `Worker`
  (after `tags`). Update `__repr__` only if useful. Fresh DBs get it via
  `create_all`.
- **Migration**: the column is additive and the codebase provisions fresh DBs
  via `Base.metadata.create_all`. **Decision (shipped): recreate the database**,
  consistent with the project's existing convention (no migration mechanism is
  in use; the alembic scaffold has zero versions). No ALTER guard or alembic
  revision was added.
- **`persistence.py`**:
  - `validate_run_filters(run_filters) -> dict` — normalize/validate: axis ∈
    `TEST_COORD_FIELDS`; exactly one of `allow`/`deny`; non-empty string list;
    for `isolation`/`toolchain` reject values outside the scheduler's known sets
    (`{"none","podman"}`, `{"none","nix"}`) to catch typos like `podmann`. Raise
    `ValueError` on violation. Returns the canonicalized dict (sorted, deduped).
  - `_coord_value(test, axis)` — the normalization helper above.
  - `worker_accepts_test(run_filters, test) -> bool` — for each axis in
    `run_filters`: read effective value; `allow` → must be in set; `deny` → must
    not be in set. Empty filters → `True`.
  - `worker_can_serve(worker, test) -> bool` — `required_tags_for_test(test)
    .issubset(worker.tags or []) and worker_accepts_test(worker.run_filters or {}, test)`.
    Single source of truth for "can + willing."
- **Unit tests**: predicate truth table for allow/deny × isolation/toolchain ×
  default-`none`; both-set rejection; unknown-value rejection; unknown-axis
  rejection; empty = accept-all; general axis (e.g. `compiler` deny).

### Phase 2 — wire into dispatch and the unserviceable sweep

- **`web/api.py` poll** (api.py:626-633): replace `_worker_can_run(worker_tags,
  candidate.test)` with `worker_can_serve(worker, candidate.test)` (pass the
  `worker`, not just its tags). Keep `_worker_can_run` as a thin wrapper or
  inline-delegate to `persistence.worker_can_serve` so the rule stays in one place.
- **`persistence.py` unserviceable sweep** (`expire_unserviceable_queued_runs`,
  persistence.py:911-941): a run is serviceable iff some **enabled** worker
  satisfies `worker_can_serve`. Change the per-run check from "tags subset" to
  the combined predicate over the enabled-worker list. **Distinguish the cause in
  the retire message** (`retire_unserviceable_run`, persistence.py:888-908):
  - no enabled worker has the required *tags* → existing message.
  - capable workers exist but **all decline** via run-filters → message:
    `"no enabled+willing worker: all capable workers opt out of
    isolation=<…>/toolchain=<…>"`, and record `details.declined_by_filter = True`
    alongside `required_tags`. This prevents opted-out jobs from sitting in the
    queue with a misleading "missing tags" explanation.
- **Tests**: a queued run that only an opted-out worker could serve is expired
  after the timeout with the filter-specific message; a run a willing worker can
  serve is left queued.

### Phase 3 — CLI surface

- **`worker register`** (cli.py:2832): add options
  `--accept-isolation`, `--deny-isolation`, `--accept-toolchain`,
  `--deny-toolchain` (each comma-separated), and a repeatable general
  `--run-filter AXIS=allow:v1,v2 | AXIS=deny:v1`. Parse → build `run_filters` →
  `validate_run_filters` → store on the new `Worker`. Echo the resolved filters.
  Reject mixing accept+deny for the same axis at the CLI layer with a friendly
  `UsageError` (defense in depth over the persistence validator).
- **`worker update`** (cli.py:2903): same flags, plus
  `--clear-run-filters` (drop all) and `--clear-filter AXIS` (drop one axis).
  Merge semantics mirror `_resolve_tags` (cli.py:504): start from current
  `run_filters`, apply per-axis set/clear, validate, persist via `update_worker`.
- **`worker list`** (cli.py:2876): add a compact `Filters` column, e.g.
  `iso:allow[none] tc:deny[nix]`, `-` when empty.
- A small `_parse_run_filters(...)` helper shared by register/update, with unit
  tests for the `AXIS=allow:…` grammar and the shortcut flags.

### Phase 4 — admin API + web

- **`web/api.py`**: extend `WorkerUpdateRequest` with an optional `run_filters`
  field; thread it through `patch_worker` (api.py:446) → `update_worker`
  (persistence.py:975, add `run_filters=` param that calls
  `validate_run_filters`). Add `run_filters` to `_worker_to_dict` (api.py ~430)
  and to the worker `register_worker` request body (api.py:407) so remote
  register (cli `_worker_register_remote`) can pass it.
- **`/workers/me`** (api.py:497): include `run_filters` so the worker can log,
  on startup, what it is configured to decline (informational only — matching is
  coordinator-side). Add a worker-startup log line (worker.py, near the
  `/me` fetch ~128-142).
- **Web worker page** (admin worker management UI — see
  `plan/done/admin-worker-management.md`): render current run-filters and provide
  an editor (per-axis allow/deny chips for isolation & toolchain, raw JSON/textarea
  for advanced axes), POSTing the PATCH. Read-only display is the minimum; editor
  is the stretch within this phase.

### Phase 5 — docs + service env

- Document the willingness-vs-capability distinction and the flags in the worker
  README / `opp_ci worker --help` epilog.
- Note in the service-install path (`service.py`) that run-filters are DB state,
  not env — changing them does **not** require reinstalling the service unit
  (unlike token/coordinator URL). No `.env` key needed.

## Acceptance criteria

- A worker registered with `--deny-isolation podman` is never assigned a
  `isolation=podman` Test even though it advertises the `podman` tag; it still
  takes `isolation=none` jobs.
- A worker with `--accept-isolation podman` (allow-list) takes only Podman jobs
  and declines bare-metal, including the default `isolation=none`.
- The same works for `--deny-toolchain nix` / `--accept-toolchain nix`.
- `--run-filter compiler=deny:gcc-7` declines that compiler; general axes work.
- Setting both accept and deny for one axis is rejected (CLI and API).
- A queued run only an opted-out worker could serve is expired by the
  unserviceable sweep with a message that names the opt-out, not "missing tags."
- Existing workers (empty `run_filters`) behave exactly as before.
- `worker list`, `worker register`, `worker update`, admin PATCH, and `/workers/me`
  all show/round-trip `run_filters`.

## Risks / open points

- **Resolution vs willingness (accepted v1 behavior).** `fleet.py` resolves loose
  axes from advertised tags, ignoring run-filters. If *every* capable worker opts
  out of the resolved value, the run becomes unserviceable and is expired (now
  with a clear message). Acceptable because (a) isolation/toolchain aren't
  fleet-resolved today, and (b) willingness is an operational drain, not a
  capability statement. Future knob: bias resolution toward values at least one
  willing worker accepts.
- **Starvation visibility.** Opt-outs can make a fleet unable to serve an axis the
  operator still submits to. The unserviceable message + `details.declined_by_filter`
  are the surfaced signal; consider a future dashboard count of filter-declined
  expirations.
- **Migration on existing DBs.** Resolved: the database is recreated (project
  convention), so no ALTER guard / alembic revision was added.

## Touch list (files)

- `opp_ci/db/models.py` — `Worker.run_filters` column.
- `opp_ci/persistence.py` — `validate_run_filters`, `_coord_value`,
  `worker_accepts_test`, `worker_can_serve`, `update_worker(run_filters=…)`,
  unserviceable sweep + retire message.
- `opp_ci/web/api.py` — poll matcher, `WorkerUpdateRequest`, `patch_worker`,
  `_worker_to_dict`, `register_worker`, `/workers/me`.
- `opp_ci/cli.py` — `worker register` / `update` / `list`, `_parse_run_filters`.
- `opp_ci/worker.py` — startup log of configured filters.
- web worker template/JS — display + edit.
- migration (ALTER guard or alembic revision).
- tests — predicate, sweep, CLI parsing, API round-trip.
