# Test identity must include resolved dependency versions

## Problem

A `Test` is supposed to fully define **what** is tested, **how**, and
**where** — including the versions of every dependent project. It does
not. Running mm1k against omnetpp 6.4.0 and then 6.3.0 reuses the *same*
`Test` row, so the two are indistinguishable at the identity level and a
single `Test` conflates results from different environments.

### Root cause

`Test` identity is `coord_hash = SHA-256(TEST_COORD_FIELDS)`
([models.py:208-219](../../opp_ci/db/models.py#L208-L219)). The field
list ([models.py:196-205](../../opp_ci/db/models.py#L196-L205)) covers
project / kind / mode / platform / compiler / isolation / toolchain /
opp_file — **no dependency versions**.

`resolved_deps` (e.g. `{"omnetpp": "6.4.0"}`) is computed *before* every
`get_or_create_test` call but only stored on `TestRun.resolved_deps`
(per-attempt) — never folded into Test identity. So distinct dep sets
collapse to one `coord_hash`.

### Design decision (settled)

- **Dependency versions become part of `Test` identity.** A Test's
  environment (platform + compiler + isolation + **dep versions**) is
  fixed; that is the "how/where".
- **The project's own version/commit stays per-run** (`TestRun.version`
  / `git_ref` / `commit_sha`). One Test is rerun across many commits of
  the project-under-test over time — the normal CI tracking model. Only
  `resolved_deps` moves into identity.
- **Existing DB data is wiped & recreated** (no split-by-run migration).
  Schema is currently materialized via `Base.metadata.create_all`; the
  `alembic versions/` dir is empty. We drop/recreate rather than write a
  backfill that would have to split conflated Tests.

## Useful prior art in the codebase

`fingerprint.py` already treats deps as outcome-relevant:
- `_normalised_deps()` ([fingerprint.py:92-102](../../opp_ci/fingerprint.py#L92-L102))
  canonicalizes `resolved_deps` to a sorted-keys dict and treats `None`
  == `{}`. **Reuse this** so identity and fingerprint agree.
- `compute_cache_fingerprint` folds `resolved_deps` into the cache key
  ([fingerprint.py:126](../../opp_ci/fingerprint.py#L126)). Note it is a
  *superset* of Test identity (also includes `git_ref`/`version`), and
  stays that way — fingerprint = identity + per-run knobs. No change to
  its semantics.

## Changes

### 1. `opp_ci/db/models.py` — fold deps into the hash + new column

- Add a normalized-deps key to the hash payload in
  `compute_test_coord_hash`:
  ```python
  payload = {field: coord.get(field) for field in TEST_COORD_FIELDS}
  payload["resolved_deps"] = _normalised_deps(coord.get("resolved_deps"))
  ```
  `_normalised_deps` ensures `None`/`{}`/unsorted dicts hash identically,
  so a pinned `omnetpp=6.4.0` and an auto-resolved `6.4.0` produce the
  **same** Test (identity = resolved versions, not pin intent — correct).
- Own `_normalised_deps` here (lowest-level module) and have
  `fingerprint.py` import it instead of defining its own, keeping the two
  canonicalizations identical. models.py must **not** import fingerprint
  (one-directional dep; avoids the import cycles noted for this repo).
- Add column to `Test`:
  ```python
  resolved_deps = Column(JSON, nullable=True)  # part of identity; see coord_hash
  ```
  Update the "immutable coordinate fields" comment block to mention deps.
- Leave `TEST_COORD_FIELDS` as-is (scalar columns); `resolved_deps` is a
  dict handled explicitly, not a scalar coord field.

### 2. `opp_ci/persistence.py` — carry deps into the coord

- `job_to_coord()` ([persistence.py:69](../../opp_ci/persistence.py#L69))
  currently filters a job to `TEST_COORD_FIELDS`, dropping the dict.
  Carry it through: `coord["resolved_deps"] = job.get("resolved_deps")`.
- `get_or_create_test()` ([persistence.py:77-93](../../opp_ci/persistence.py#L77-L93)):
  the hash now varies with deps automatically; also persist the column on
  create: `resolved_deps=coord.get("resolved_deps")` alongside the
  `TEST_COORD_FIELDS` scalars.

### 3. The four `get_or_create_test` call sites — put deps in `coord`

Each already has `resolved_deps` in scope; add it to the coord dict:
- CLI `run`: [cli.py:833](../../opp_ci/cli.py#L833) (var at line 796).
- REST `/runs`: [web/api.py:160](../../opp_ci/web/api.py#L160) (var at 175).
- Web UI form: [web/app.py:688](../../opp_ci/web/app.py#L688) (var at 657).
- Matrix expansion goes through `job_to_coord` (covered by #2); each
  `expand_matrix` job already carries `resolved_deps`
  ([scheduler.py:465](../../opp_ci/scheduler.py#L465)).

`create_test_run(..., resolved_deps=...)` stays — the per-run column is
still recorded (now redundant with Test, but harmless and useful as the
as-run record). Optionally assert they match.

### 4. API / UI surfacing

- `_run_to_dict()` ([web/api.py:1804](../../opp_ci/web/api.py#L1804))
  omits deps; add `"resolved_deps": test.resolved_deps` so clients see
  the full identity (today it's invisible). Worker-poll response already
  sends `claimed_run.resolved_deps` — leave it.
- Any Test list / detail view in `web/app.py` templates: show the dep
  versions as part of the Test's coordinates.

### 5. Schema reset

- Wipe & recreate the coordinator DB (drop `tests` + `test_runs` +
  dependent tables, or reset the database) so fresh `coord_hash` values
  include deps. Confirm the deploy path that runs
  `Base.metadata.create_all` rebuilds cleanly.
- No alembic migration authored (versions/ stays empty for now).

## Verification

1. Unit: `compute_test_coord_hash` differs for `{"omnetpp":"6.4.0"}` vs
   `{"omnetpp":"6.3.0"}`, and is equal for `None` vs `{}` vs reordered
   keys.
2. Unit: `get_or_create_test` creates two distinct Tests for the same
   coords with different `resolved_deps`, and reuses one for identical
   deps (incl. pinned-vs-resolved-same-version).
3. Integration via `opp_ci/bin/test-mm1k`: run mm1k with
   `--pin omnetpp=6.4.0` then `--pin omnetpp=6.3.0`; assert two
   different `test_id`s, each with the right `resolved_deps`.
4. Regression: matrix run with a `deps` axis
   (`{"omnetpp": ["6.3.0","6.2.0"]}`) yields one Test per dep cell.
5. Confirm cache fingerprint behavior unchanged (it already keyed on
   deps).

## Blast radius / references

- Identity: `TEST_COORD_FIELDS`, `compute_test_coord_hash`, `coord_hash`,
  `get_or_create_test`, `job_to_coord`.
- Callers: cli.py:833, web/api.py:160, web/app.py:688, enqueue_job
  (persistence.py:467) via job_to_coord.
- Serialization: `_run_to_dict` (web/api.py:1804); worker poll
  (web/api.py:490, already includes deps).
- `fingerprint.py`: refactor to import shared `_normalised_deps`; no
  semantic change.
