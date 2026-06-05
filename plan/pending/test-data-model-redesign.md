# Plan: test automation features on the new data model

Phase 1 of the data model redesign (the Test / TestMatrix / TestRun /
TestMatrixRun schema, the `kind` rename, the persistence helpers, and
the cutover across REST / web / worker / CLI) is shipped. The closed
record is in
[plan/done/test-data-model-phase-1-schema.md](../done/test-data-model-phase-1-schema.md);
the schema itself lives in
[`opp_ci/db/models.py`](../opp_ci/db/models.py) and the helpers in
[`opp_ci/persistence.py`](../opp_ci/persistence.py).

This plan is the next layer up: operational features built on top of
that schema so opp_ci can answer two operational questions for every
opp_env simulation project without anyone hand-authoring a fresh matrix
for each investigation:

1. **"Is this release ready to publish?"** — for a tagged release
   candidate, does the project pass on every combination we currently
   intend to support? If not, what regressed?
2. **"Would this project work on system X?"** — given a (project,
   version, ref, OS, OS version, compiler, mode, …) tuple, is there a
   recent PASS / FAIL / ERROR record for it?

Today neither is directly answerable. The shipped pipeline can run any
matrix you hand-author (see
[doc/test_matrix_dimensions.md](../doc/test_matrix_dimensions.md)) and
produces per-`TestRun` results, but several pieces are missing:

- **No ad-hoc runs.** Every matrix must be named and stored before it
  can execute. Investigative work pays a tax for each one-off.
- **No group-level rollup verdict.** A matrix expansion produces N
  `TestRun` rows; counting outcomes and grading them as a release
  verdict isn't built in. The phase-1 schema deliberately left
  `TestMatrixRun` without a stored lifecycle / verdict; status is
  rolled up in app code as a placeholder.
- **No expected outcomes wired into grading.** The `Test` row carries
  `expected_result_code` / `expected_result_description`, but nothing
  reads them. Known-broken combinations (e.g. INET on macOS-arm64 with
  a specific clang) show up as red on every release run, drowning out
  real regressions.
- **No content-aware result cache.** Re-running a matrix re-executes
  every cell even when nothing changed. The phase-1 cutover removed
  the legacy identity-tuple dedup and explicitly deferred caching.
- **No release-tag trigger.** Maintainers run matrices manually after
  tagging.

The two questions reduce to: roll up across `TestRun`s of a
`TestMatrixRun` (Q1), and look up `TestRun` by the relevant `Test`
coordinates (Q2).

## Locked design decisions

| Question | Decision |
|---|---|
| Grading rollup at the matrix-run level | Three-state verdict on `TestMatrixRun`: `EXPECTED` / `UNEXPECTED` / `UNKNOWN`. Release-ready ⇔ `verdict == EXPECTED`. |
| Per-cell verdict storage | A `verdict` column on `TestRun`, written when the outcome lands by comparing `TestRun.result_code` against the *then-current* `Test.expected_result_code`. Editing `Test.expected_result_code` later does not retroactively change historical verdicts. |
| Counters on `TestMatrixRun` | Stored, not recomputed. Updated atomically as each child `TestRun` lands. The phase-1 app-side rollup is replaced once these columns exist. |
| Ad-hoc cartesian product without a saved matrix | Yes — *anonymous* matrices via the same launcher. (Schema-wise, anonymous matrices already get a persisted `TestMatrix` row with a generated name from phase-1.) |
| Caching | Content-addressable fingerprint on `TestRun`. Re-running an unchanged matrix is near-instant; moving refs are detected via the fingerprint and re-executed. |
| Re-test cadence | On tagged release candidates only (cron / nightly deferred). |
| Native vs. podman | Per-matrix choice — podman for releases, native for dev. |
| Auto-expansion (algorithm / AI) | Deferred — see [Future scope](#future-scope). |

## How the two questions get answered

**Q1 — release-ready?** A maintainer tags `inet-4.5.3`. An
`AutoTestRule` matching the tag pattern fires. The bound matrix
expands (cache absorbs unchanged cells, fresh cells run on workers).
The `TestMatrixRun` row holds the rollup. `verdict == EXPECTED` ⇒
release-ready: every cell had a declared expectation and met it
(including XFAILs). `UNEXPECTED` ⇒ at least one cell diverged from its
expectation (any kind of mismatch — wrong outcome or unexpected
ERROR). `UNKNOWN` ⇒ no mismatches, but at least one cell ran without a
declared expectation, so the matrix doesn't yet *say* what that cell
should do — the maintainer either declares an expectation (edits the
`Test` row) or investigates the cell. `opp_ci show-matrix-run <id>`
lists the diverged and undeclared cells; the release blocks until the
verdict is `EXPECTED`.

**Q2 — would it work on X?** A direct lookup against `TestRun` joined
through `Test` on the relevant coordinates. Exact match ⇒ return the
actual result. No exact match ⇒ answer "no data" and offer to queue a
one-off:

```
opp_ci run-matrix --project inet --ref v4.5 \
    --os "Fedora 41" --compiler clang-18 --kinds smoke
```

Smarter neighbour-based interpolation ("Fedora 41 not tested, but
Fedora 40 + same compiler passed") is explicitly out of scope for this
plan — see [Future scope](#future-scope).

## Schema additions

Columns to add on top of the existing tables. No new tables.

### `test_matrix_runs` — counters, verdict, summary

| Column | Type | Notes |
|---|---|---|
| `pass_count` | int | actual outcome counters |
| `fail_count` | int | |
| `error_count` | int | |
| `expected_count` | int | cells whose verdict is EXPECTED |
| `unexpected_count` | int | cells whose verdict is UNEXPECTED |
| `unknown_count` | int | cells whose verdict is UNKNOWN (no expectation set on the targeted Test) |
| `cache_hit_count` | int | cells served from cache (zero until F4 lands) |
| `total_count` | int | |
| `actual_summary` | enum | PASS / FAIL / ERROR — worst actual across cells |
| `verdict` | enum | EXPECTED / UNEXPECTED / UNKNOWN — same enum as the per-cell verdict, rolled up |
| `ref` | text | git ref / tag the run is against, if any (snapshotted from the triggering event) |
| `completed_at` | timestamptz? | null until the last cell lands |

(`trigger` already exists on `TestMatrixRun` from phase 1.)

Verdict rollup rules (evaluated in order):

- `UNEXPECTED` — at least one cell's actual diverged from its declared
  expectation (this covers unexpected errors as well — an unexpected
  ERROR is just an UNEXPECTED cell).
- `UNKNOWN` — no UNEXPECTED cells, but at least one cell ran against a
  `Test` whose `expected_result_code` is `NULL`. The matrix has actual
  results but isn't fully *characterised* yet.
- `EXPECTED` — every cell targeted a `Test` with a declared expectation
  and the actual matched it. This is the release-ready state.

Release-readiness is then a one-liner: `verdict == EXPECTED` on the
`TestMatrixRun` triggered by the release tag.

### `test_runs` — verdict and cache columns

| Column | Type | Notes |
|---|---|---|
| `verdict` | enum | EXPECTED / UNEXPECTED / UNKNOWN; written at result time by comparing `result_code` to the targeted `Test.expected_result_code` *as of that moment* |
| `cache_fingerprint` | text | content-addressable hash; populated at submit time once F4 lands |
| `cached_from_id` | int? | nullable; if set, this row reused another run's outcome |

### `auto_test_rules` — tag pattern

| Column | Type | Notes |
|---|---|---|
| `tag_pattern` | text? | glob/regex matched against tag-push events |

## Features in detail

### F1 — `TestMatrixRun` rollup

`TestMatrixRun` rows already exist; this feature adds the eager rollup.
Each time a child `TestRun` finishes (live or via cache, once caching
exists), a transactional update bumps the relevant counters on the
parent row and recomputes `actual_summary` and `verdict`. Once the
final child lands, `completed_at` is set.

The rollup is stored, not recomputed — the UI and API never have to
fan out across thousands of `TestRun`s to render a verdict. This
supersedes phase-1's "rollup in app code" placeholder, which was a
deliberate phase-1 simplification.

The per-cell `verdict` lives on `TestRun` so the rollup is a simple
counter increment rather than a join. Because the verdict is computed
against `Test.expected_result_code` *at the moment the run finishes*,
later edits to `Test.expected_result_code` do not retroactively change
historical verdicts or counters — a `TestMatrixRun` is a snapshot of
"what we knew when this ran". This is the right behavior for
append-only history but is worth flagging to users in the UI.

### F2 — Anonymous matrices

Phase-1 schema already supports anonymous matrices: a spec is
expanded, a `TestMatrix` row is written with a generated name, and a
`TestMatrixRun` row is created against it. This feature adds the
launcher surface — inline axis flags and `--spec-file` — so users
don't have to hit the REST API or write a `create-matrix` config
first:

```bash
opp_ci run-matrix \
    --project inet --ref v4.5 \
    --kinds smoke \
    --modes release,debug \
    --os "Ubuntu 24.04,Fedora 41" \
    --compiler gcc-14,clang-18 \
    --isolation podman \
    --toolchain none
```

Equivalent JSON spec (also accepted via `--spec-file path.json` or
`--spec-file -` for stdin):

```json
{
  "project": "inet",
  "ref": "v4.5",
  "kinds": ["smoke"],
  "modes": ["release", "debug"],
  "os": ["Ubuntu 24.04", "Fedora 41"],
  "compiler": ["gcc-14", "clang-18"],
  "isolation": ["podman"],
  "toolchain": ["none"]
}
```

Both forms go through the same `expand_matrix()` code path that named
matrices use. The downstream effect is identical — phase-1's
generated-name behavior handles the persistence, this feature only
provides the launcher ergonomics.

### F3 — Expected results and per-cell verdict

Expectations live on `Test` (`expected_result_code`,
`expected_result_description`), set by phase-1 schema and edited
through CRUD on the `Test` row. They are **not** part of a matrix
spec, and matrix expansion does not modify them. A `Test` whose
`expected_result_code` is `NULL` means "no expectation declared" — not
"PASS by default".

The contribution this plan makes is the *grading layer*:

- A `verdict` column on `TestRun`, populated when the outcome lands:
  - `EXPECTED` — `result_code == expected_result_code`
  - `UNEXPECTED` — `result_code != expected_result_code` (including
    unexpected ERROR)
  - `UNKNOWN` — `expected_result_code IS NULL`
- The matrix-run rollup over those (`expected_count`,
  `unexpected_count`, `unknown_count`, and the derived
  `TestMatrixRun.verdict`).
- A UI / REST surface to edit `expected_result_code` and
  `expected_result_description` on a `Test` row.

Editing an expectation in the UI applies *forward only* — historical
verdicts and rollups are not recomputed. A new matrix run will use the
updated expectation; an old one keeps its snapshot. This is the
append-only-history posture that lets the dashboard's "release run on
tag X-Y-Z had verdict EXPECTED" stay a stable, audit-grade claim.

CLI convenience for bulk-setting expectations (writing onto matching
`Test` rows):

```
opp_ci set-expectation --project inet \
    --where os="Ubuntu 24.04" \
    --expect pass

opp_ci set-expectation --project inet \
    --where os="Fedora 41",compiler=gcc-14 \
    --expect fail \
    --reason "tracked in #432"
```

This is sugar over the REST endpoint that updates `Test` rows matching
the `--where` predicate. It does not run anything — it edits the
expectation. Running the matrix afterwards picks up the new
expectations through normal grading.

### F4 — Content-addressable cache

The phase-1 cutover removed the legacy `find_existing_run()` dedup
along with the legacy models, and explicitly deferred caching. This
feature reintroduces caching, content-addressable from the start so
the "every tagged release re-runs the matrix" cadence is cheap.

```
cache_fingerprint = hash(
    resolved_project_sha,   # `master` → actual SHA at submit time
    resolved_dep_shas,      # opp_env install plan, fully pinned
    opp_env_recipe_sha,
    platform_image_sha,     # only for isolation=podman
    kind,
    mode,
    isolation, toolchain,
    build_flags,
)
```

Submission flow:

1. Resolve the moving parts (git ref → SHA, dep pins, recipe SHA,
   image SHA) and compute `cache_fingerprint`.
2. Look up the most recent `TestRun` with `lifecycle == finished` and
   the same `cache_fingerprint`. PASS, FAIL, and ERROR all count as
   deterministic — only cancelled / timed-out / not-yet-finished runs
   are cache-misses.
3. **Hit**: insert the new `TestRun`, copy `result_code` and the
   outcome columns from the matched row, set `cached_from_id`, mark
   `lifecycle = finished` without queuing. Compute and store
   `verdict`. Bump `cache_hit_count` on the parent `TestMatrixRun`.
4. **Miss**: queue as normal; `cache_fingerprint` is stored on the new
   row so the next submission can hit it.

`expected_result_code` is **not** in the cache key — expectations are
post-hoc annotations on `Test`. A cached cell still gets a fresh
verdict comparing its (cached) actual against the targeted `Test`'s
(current) expectation.

`--no-cache` on `opp_ci run-matrix` bypasses cache lookup entirely.

### F5 — Release-tag triggers

Extend `AutoTestRule` so it can bind a matrix to a tag pattern (e.g.
`inet-*` or `v*.*.*-rc*`). On GitHub tag-push events the existing
webhook handler ([opp_ci/github/](../opp_ci/github/)) dispatches the
bound matrix with `TestMatrixRun.trigger = "tag"` and
`TestMatrixRun.ref` set to the tag name.

Existing branch-push rules remain unchanged. A project can have
multiple rules — e.g. a lightweight `smoke` matrix on every push to
master, plus a heavyweight full matrix on release-candidate tags.

### F6 — Native vs. podman as a per-matrix choice

Already supported as axes (`isolation`, `toolchain`); calling it out
so the docs make the convention explicit:

| Matrix kind | `isolation` | `toolchain` | Why |
|---|---|---|---|
| Release | `podman` | `nix` | High fidelity; reproducible; per-OS container images carry the native package set |
| Dev / quick | `none` | `none` | Fast; relies on the worker's host environment |

No code change for F6 — pure documentation + recommended defaults in
the `opp_ci create-matrix` scaffolding.

## CLI surface

`opp_ci run-matrix` is the universal launcher — named matrix,
anonymous matrix from flags, or anonymous matrix from a spec file.
Every invocation creates exactly one `TestMatrixRun`.

| Command | Purpose |
|---|---|
| `opp_ci run-matrix --matrix NAME` | (existing) Launch a named matrix. |
| `opp_ci run-matrix [axis flags…]` | (new) Anonymous matrix from inline axis flags. `--follow` streams progress until termination. `--no-cache` forces fresh execution. |
| `opp_ci run-matrix --spec-file path.json` | (new) Anonymous matrix from a full JSON spec; `-` reads from stdin. |
| `opp_ci show-matrix-run <id>` | (new) Print rollup + per-cell table for one `TestMatrixRun`. `--unexpected-only` filters to diverged cells. |
| `opp_ci list-matrix-runs` | (new) Recent `TestMatrixRun` rows. Flags: `--project`, `--verdict`, `--since`, `--limit`. |
| `opp_ci set-expectation` | (new) Bulk-edit `expected_result_code` / `expected_result_description` on matching `Test` rows. Sugar over the REST update endpoint. |

Modified commands:

- `opp_ci auto-test-rule create` — accepts `--tag-pattern` in addition
  to the existing branch options.

`opp_ci run` (the single-test command) is untouched.

## REST API

Mirror of the CLI:

- `POST /api/matrix-runs` — body = spec JSON → returns `{id, status: queued}`.
- `GET /api/matrix-runs/<id>` — rollup + paginated cells.
- `GET /api/matrix-runs?project=…&verdict=UNEXPECTED&since=…` — list view.
- `PATCH /api/tests/<id>` — update `expected_result_code` and
  `expected_result_description`.
- `POST /api/auto-test-rules` — body now accepts `tag_pattern`.

## Web UI

Two new pages:

- **Matrix runs index** — table of recent `TestMatrixRun` rows with
  verdict, counters, trigger, and link to the underlying matrix.
- **Matrix run detail** — rollup header + per-cell table. UNEXPECTED
  rows highlighted. Click a cell ⇒ existing `TestRun` detail page.
  Inline editor on each row to set the targeted `Test`'s expectation
  (with a "future runs only" note explaining the snapshot semantics).

The project page (existing) gains a "Latest release run" card showing
the most recent tag-triggered `TestMatrixRun` with its verdict — the
at-a-glance answer to Q1.

## Phased implementation

Each phase is independently useful and ships independently.

### Phase 1 — Rollup, counters, verdict, matrix-run pages

- Add the counter / `actual_summary` / `verdict` / `ref` /
  `completed_at` columns to `test_matrix_runs`, and the `verdict`
  column to `test_runs`.
- Replace phase-1's app-side roll-up with an eager transactional
  update: on each child `TestRun` lifecycle write, recompute the
  parent rollup atomically.
- Verdict computation at result-write time: read the targeted
  `Test.expected_result_code` and write `TestRun.verdict`.
- `opp_ci show-matrix-run <id>` and `opp_ci list-matrix-runs` (CLI +
  REST).
- Web UI matrix-runs index + detail pages.

Already useful: every matrix run now has a single stored
`actual_summary` + `verdict` + counters answerable in O(1).

### Phase 2 — Anonymous-matrix launcher surface

- Extend `opp_ci run-matrix` with axis flags and `--spec-file`.
- `POST /api/matrix-runs` accepts inline spec.

Schema-wise nothing new — anonymous-matrix persistence is already in
place from phase-1 schema. This phase is purely launcher ergonomics.

Already useful: ad-hoc cartesian-product runs without authoring a
named matrix.

### Phase 3 — Expectation editing UX

- Inline expectation editor on the matrix-run detail page (rows whose
  verdict is UNKNOWN or UNEXPECTED).
- `opp_ci set-expectation` CLI + matching REST.
- Reason / description editor.
- Documentation explaining the "edits are forward-only; historical
  matrix-run verdicts are snapshots" semantics.

Schema columns exist from phase-1; verdict computation lands in Phase
1 above. This phase is the UX that turns those into a workflow:
maintainer looks at a red matrix run, declares which UNKNOWN cells
should be expected to fail, re-runs to confirm, achieves `verdict ==
EXPECTED`.

Already useful: known-broken combinations stop polluting release
verdicts, and "is this characterised yet?" is queryable per-Test.

### Phase 4 — Content-addressable cache

- Add `cache_fingerprint` and `cached_from_id` columns to `test_runs`;
  add `cache_hit_count` rollup on `test_matrix_runs`.
- Fingerprint computation in `opp_ci/fingerprint.py` — resolves moving
  refs, dep pins, recipe SHA, image SHA at submit time.
- Cache lookup at submission keyed on `cache_fingerprint`.
- `--no-cache` flag on `opp_ci run-matrix`.

Already useful: re-running an unchanged matrix is near-instant, and
moving-ref runs (`master`, `inet-git`) stop returning stale results.

### Phase 5 — Release-tag triggers

- `tag_pattern` column on `auto_test_rules`.
- Webhook handler dispatches matrix runs on matching tag-push events,
  setting `TestMatrixRun.trigger = "tag"` and `TestMatrixRun.ref`.
- "Latest release run" card on the project page.

Already useful: tagging a release auto-runs the full matrix; the
project page shows green/red without anyone running anything by hand.

## Future scope

Listed so the data model and APIs don't paint us into a corner — but
no design or code in this plan.

- **Auto-expansion via algorithm.** opp_ci proposes new matrix cells
  by cross-producting opp_env's catalog, probing neighbours of passing
  cells, and densifying around pass/fail boundaries. Lands proposals
  in a staging area; promotion is a human one-click.
- **Auto-expansion via AI agent.** Same idea but with judgment — reads
  project READMEs, sibling-project matrices, recent issues, and
  proposes additions with rationale.
- **Neighbour interpolation for Q2.** When no exact `TestRun` exists,
  answer "would X work?" by interpolating from adjacent cells (same OS
  family, neighbouring compiler version) with a confidence score.
- **Per-push / nightly re-runs.** Currently only tagged release
  candidates trigger. Caching makes higher cadences cheap; deferred
  until there's demand.
- **macOS / aarch64 worker fleet.** The new entities are
  platform-agnostic; expanding worker coverage is a separate ops
  exercise.
- **Retroactive verdict recomputation.** Editing
  `Test.expected_result_code` only affects future runs today. A
  "recompute verdicts for matrix runs since date X" tool could re-grade
  historical rollups — useful if a long-standing misclassification is
  corrected, but it breaks the audit-grade snapshot guarantee, so it
  would be an opt-in admin action.
- **Support declaration on opp_env side.** A structured "this version
  is intended to work on these platforms" schema living in the opp_env
  project descriptor. Explicitly rejected for this plan — matrices
  serve as the implicit declaration.

## Out of scope (explicitly dropped)

- **`MatrixSet` entity** (a curated list of named matrices per
  project, with its own runs and rollups). Dropped as too complicated
  — anonymous matrices plus tag-triggered named matrices cover the
  operational needs.
- **Spec-time expectation rules.** An earlier draft put rule-based
  `expected_results` blocks in the matrix spec, evaluated at expansion
  time to stamp each child `TestRun`. The phase-1 schema puts
  expectations on `Test` as user-editable state instead, which is
  cleaner: one expectation per coordinate, edited like any other
  metadata, not duplicated across every matrix that hits the same cell.

## Open questions

Carried over from the phase-1 redesign plan and from
project-test-automation. Not blockers for any of the phases above;
flag if any becomes a real concern.

1. **Filtered matrix rerun.** Phase 1 of *this* plan adds an O(1)
   rollup, but rerunning a `TestMatrixRun` still re-expands every
   child. Add "rerun only the failed/errored children" as a UI/CLI
   option? If so, does the new `TestMatrixRun` re-expand the (possibly
   evolved) `TestMatrix` and pick a subset, or copy the surviving
   subset of `Test`s from the old `TestMatrixRun` directly? Different
   semantics when the matrix has been edited between attempts.

2. **Audit / history for mutable columns on `Test`.** `Test` has three
   mutables (`name`, `expected_result_code`,
   `expected_result_description`). A silent rename loses context.
   `expected_result_code` already has snapshot-style isolation via the
   per-cell verdict snapshot (an edit only affects future runs), but
   `name` and `expected_result_description` are still silently
   overwritten. Do we want a single audit mechanism covering all three?

3. **`TestRun.details` JSON schema.** Phase-1 schema stores it as a
   free-form blob. If a particular field becomes a common query target
   (per-subtest breakdown for a comparison view, fingerprint mismatch
   data for triage), promote it to its own column or codify a JSON
   schema. Risk of becoming a junk drawer otherwise.

4. **`system_snapshot` retention policy.** Pruning is `UPDATE … SET
   system_snapshot = NULL WHERE …` — operationally cheap, semantically
   safe (lifecycle row + outcome stay). The policy is open: drop after
   N months, drop above a size threshold, drop only for
   `result_code = PASS` runs, never drop? Likely a deployment-config
   question rather than a code one.

5. **Cancel / abort for running `TestRun`s.** Phase 1 lets running
   runs finish — cancel only transitions queued rows. If we ever want
   a real abort (worker-side signal, polling flag), it lands here. Out
   of scope until there's a concrete need.

6. **Suite-internal granularity.** Deferred: a `Test` represents a
   *full suite* at coordinates, and the suite's internal per-test
   results collapse into one aggregate outcome on `TestRun`. When we
   eventually want per-individual-test results, the path is to add a
   per-individual-test entity below `Test` and split the outcome
   accordingly (re-introducing a child outcome table is one option).
   The names should not block this future split.

7. **`expect: ERROR` legitimacy.** ERROR usually means infrastructure
   failure (worker died, podman crash, timeout). Allowing
   `expected_result_code = ERROR` is honest about known-flaky
   environments but risks papering over real infra issues. Including
   for completeness; may deprecate.

8. **Verdict for cached cells.** A cache hit copies the actual outcome
   from a prior run, then grades against the *current*
   `Test.expected_result_code`. If the expectation changed since the
   cached attempt finished, the new `TestRun`'s verdict reflects the
   new expectation while its actual reflects old code. That's
   consistent with everything else (verdict is a snapshot at
   result-write time), but the "actual" half is staler than the
   expectation half. The UI should probably surface "cached at SHA X"
   when a cell's `cached_from_id` is set.

9. **Conflict between matrix-expansion expectation hints and
   `Test.expected_result_code`.** If we ever bring back any form of
   per-matrix expectation override (e.g. "this release matrix expects
   PASS even though the Test row says FAIL"), the storage model needs
   another layer. Out of scope for now, but flag if it becomes a real
   request.
