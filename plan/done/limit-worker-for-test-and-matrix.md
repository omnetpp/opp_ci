# Limit which worker a Test / TestMatrix runs on

## Problem

Job-to-worker assignment is purely capability-driven and pull-based: a
worker advertises `tags`, a Test derives a *required* tag set
(`required_tags_for_test`), and `worker_poll` hands a queued run to the
first worker whose tags are a superset (`_worker_can_run`). There is no way
for a user to say "run *this* test/matrix only on worker `bigbox`" or "only
on the GPU workers" beyond what the platform coordinate already pins. Any
worker that satisfies the capability tags is eligible, and the user can't
narrow that set.

Concrete needs: pin perf-sensitive work to one dedicated machine; keep a
matrix off a flaky/shared worker; route to a worker with a non-capability
resource (license, big disk, specific hardware) that isn't part of the
build coordinate.

## Decision (locked)

- **Reuse the tag-subset model, don't add a parallel mechanism.** A worker
  selector is just *extra required tags* unioned onto the capability set.
  `_worker_can_run` already means "required ⊆ worker tags"; a selector adds
  to `required`. No new matching algorithm, no affinity/anti-affinity engine.

- **Selector is routing, NOT identity. It lives on `TestRun`, never in
  `coord_hash`.** Routing a build to worker A vs worker B does not change
  *what the Test is* or its result, and folding it into `Test.coord_hash`
  would fragment the build/test cache (a green run on A wouldn't satisfy the
  "same test pinned to B"). So the selector is a per-attempt knob on
  `TestRun`, alongside `git_ref` / `version` / `resolved_deps`.

- **Every worker implicitly advertises `worker:<name>`.** This unifies the
  two user intents under one tag mechanism: "pin to one worker" is the
  selector `["worker:bigbox"]`; "restrict to a class" is `["gpu"]` or
  `["team:core"]`. The implicit tag is computed at match time (worker names
  are unique), not stored, so it can't drift from `Worker.name`.

- **Selector semantics: AND (subset), like capability tags.** All selector
  tags must be present on the worker. A selector restricting to "any one of
  several workers" is out of scope for v1 — `worker:<name>` pins a single
  box, custom shared labels (`gpu`, `team:core`) cover the "a set of
  workers" case. (An OR/expression grammar is a possible follow-up; noted
  below, not built.)

- **Matrix-level selector is a single value, not a cross-product axis.** It
  is one routing constraint applied to *every* cell of the expansion, not an
  axis multiplied into the cartesian product. It is carried in the matrix
  `config` (so it is part of `matrix_hash` / matrix identity — a matrix
  recipe legitimately includes "where to run it") and copied verbatim onto
  each expanded job dict and onto each `TestRun`.

- **Unserviceable selectors are self-cleaning.** A typo'd or never-deployed
  selector (e.g. `worker:typo`) makes the run's required set unsatisfiable
  by any enabled worker, so the existing
  `expire_unserviceable_queued_runs` sweep already retires it
  (`timed_out`/ERROR) with a message naming the missing tags. No new
  expiry logic; the message just now includes the selector tag. A
  fail-fast *warning* at submit time (CLI/API) is added for UX, but
  submission is not hard-blocked (the target worker may be registered but
  offline / still coming up).

## Changes

- **db/models.py**
  - `TestRun`: add `worker_selector = Column(JSON, nullable=True)` in the
    "Per-attempt context" block (a list of required tags, or NULL = no
    constraint). Document it as a routing constraint, deliberately *not*
    part of Test identity.
  - No change to `Worker`, `Test`, or `TestMatrix` columns. The matrix
    selector rides inside the existing `TestMatrix.config` JSON.

- **alembic / migration** — add the nullable `test_runs.worker_selector`
  column (follow the existing migration pattern in the repo; NULL backfill,
  no data move).

- **persistence.py**
  - Add `required_tags_for_run(run)` =
    `required_tags_for_test(run.test) | set(run.worker_selector or [])`.
    This is the new single source of truth for "what tags must a worker
    have to claim this run." Keep `required_tags_for_test(test)` unchanged
    (capability-only) — `expire_unserviceable_queued_runs` and the poll
    loop switch to `required_tags_for_run`.
  - `expire_unserviceable_queued_runs`: compute `required` via
    `required_tags_for_run(run)`, and build each worker's *effective* tag
    set as `set(w.tags or []) | {f"worker:{w.name}"}` so a `worker:<name>`
    selector counts a registered-but-offline target as serviceable (don't
    falsely condemn a run only that box can serve). Message already lists
    the required set, so the selector tag shows up automatically.
  - `create_test_run`: add `worker_selector=None` kwarg, set it on the
    `TestRun`.
  - `enqueue_job`: read `job.get("worker_selector")` and forward it to
    `create_test_run`. (Normalize to a sorted list / None.)

- **web/api.py**
  - `_worker_can_run(worker_tags, test)` → replace with run-aware matching:
    the poll loop computes `effective = worker_tags | {f"worker:{worker.name}"}`
    once, then checks `required_tags_for_run(candidate).issubset(effective)`.
    (Rename/replace `_worker_can_run` accordingly; it now needs the run, not
    just the test, and the worker name.)
  - The returned job spec is unaffected (the worker doesn't need the
    selector — it already won the claim). Optionally include it for
    visibility.

- **scheduler.py**
  - `_build_matrix_config`: add a `worker_selector=None` param; when set,
    store `config["worker_selector"] = <normalized list>`. Single value,
    not an axis.
  - `expand_matrix`: after building each job dict, attach
    `"worker_selector": config.get("worker_selector")` (constant across the
    expansion — *not* part of `itertools.product`).

- **cli.py**
  - `create-matrix`: add `--worker / --worker-tag` (repeatable, or
    comma-separated) → passed into `_build_matrix_config`. A bare
    `--worker bigbox` is sugar for the tag `worker:bigbox`; a value already
    containing `:` (e.g. `team:core`, `gpu`) is taken as a raw tag. Document
    both forms in the help text.
  - The JSON-spec submit path (`create_matrix_from_axes`) already passes
    `config` through — `worker_selector` in the JSON just works; add a line
    to the spec docs.
  - Direct single-run submission paths that call `create_test_run` /
    `create_test` (if any user-facing one exists) get the same
    `--worker` flag forwarding to `worker_selector`.
  - Submit-time validation helper: warn (not error) when no *enabled*
    worker's effective tags could ever satisfy the selector.
  - `_create_matrix_remote` / the remote client: thread the new flag through
    the remote create-matrix call so `--remote` behaves like local.

- **web (templates / views)**
  - Run detail: render `worker_selector` when set ("Routed to: worker:bigbox").
  - Matrix detail / create form: show / accept the selector. Surfacing it on
    the matrix create UI can land in a follow-up if the form is heavy;
    config-level support is the must-have.

## Tests

- **tests/test_worker_selector.py** (new)
  - `required_tags_for_run` unions selector tags onto capability tags;
    NULL selector == capability-only (back-compat).
  - Poll matching: a run with `["worker:bigbox"]` is claimed by `bigbox`
    (no `worker:` tag stored on it) and rejected by every other worker even
    when they satisfy all capability tags.
  - Selector by custom label (`gpu`): claimed only by workers advertising
    `gpu`.
- **tests/test_matrix_expansion.py** (extend) — `worker_selector` in config
  is copied onto every job dict unchanged and is *not* multiplied into the
  cartesian product (cell count unchanged).
- **tests/test_queue_expiry.py** (extend) — a run with `worker:typo`
  (no such worker) is retired unserviceable; a run with `worker:bigbox`
  where `bigbox` exists but is offline is *left queued* (serviceable via the
  implicit name tag).
- **CLI test** — `create-matrix --worker bigbox` produces
  `config["worker_selector"] == ["worker:bigbox"]`; `--worker-tag gpu`
  yields `["gpu"]`.

## Out of scope (possible follow-ups)

- OR / boolean-expression selectors (`bigbox OR fastbox`, `gpu AND !flaky`).
  v1 is AND-only over a tag list; `worker:<name>` + shared labels cover the
  common cases.
- Anti-affinity ("anywhere *except* worker X").
- Editing the selector on an already-queued run (today: cancel + resubmit).
