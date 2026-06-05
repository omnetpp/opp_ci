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
| Per-cell verdict storage | New `TestVerdict` table — one row per cell of a matrix run. Holds `verdict`, `recorded_at`, FK to the `TestRun` whose outcome this cell attributes, and FK to the `ExpectedTestResult` row that was in force at recording time. `TestRun` is a pure execution observation — no verdict, no matrix membership, no cache back-reference. |
| Expectations | Move off `Test` into a new append-only `ExpectedTestResult` table. Each edit is an insert with `set_by` / `set_at` / `reason`. "Current expectation for a Test" = most recent row. A row with `expected_result_code IS NULL` is an explicit retraction (distinguishable from never-set). `Test` keeps only `name` mutable. |
| Counters on `TestMatrixRun` | Stored, not recomputed. Updated atomically as each child `TestVerdict` finalizes. The phase-1 app-side rollup is replaced once these columns exist. |
| Ad-hoc cartesian product without a saved matrix | Yes — *anonymous* matrices via the same launcher. (Schema-wise, anonymous matrices already get a persisted `TestMatrix` row with a generated name from phase-1.) |
| Caching | Content-addressable fingerprint on `TestRun`. Cache hit creates only a `TestVerdict` pointing at the existing `TestRun`; no `TestRun` row is duplicated. Re-running an unchanged matrix is near-instant; moving refs are detected via the fingerprint and re-executed. |
| Re-test cadence | On tagged release candidates only (cron / nightly deferred). |
| Native vs. podman | Per-matrix choice — podman for releases, native for dev. |
| Auto-expansion (algorithm / AI) | Deferred — see [Future scope](#future-scope). |

## How the two questions get answered

**Q1 — release-ready?** A maintainer tags `inet-4.5.3`. An
`AutoTestRule` matching the tag pattern fires. The bound matrix
expands (cache absorbs unchanged cells as new `TestVerdict` rows
attributed to prior `TestRun`s; fresh cells run on workers). The
`TestMatrixRun` row holds the rollup. `verdict == EXPECTED` ⇒
release-ready: every cell had a declared expectation and met it
(including XFAILs). `UNEXPECTED` ⇒ at least one cell diverged from its
expectation (any kind of mismatch — wrong outcome or unexpected
ERROR). `UNKNOWN` ⇒ no mismatches, but at least one cell ran without a
declared expectation, so the matrix doesn't yet *say* what that cell
should do — the maintainer either declares an expectation (inserts an
`ExpectedTestResult` row) or investigates the cell. `opp_ci
show-matrix-run <id>` lists the diverged and undeclared cells; the
release blocks until the verdict is `EXPECTED`.

**Q2 — would it work on X?** A direct lookup for the most recent
finished `TestRun` joined through `Test` on the relevant coordinates.
Exact match ⇒ return the actual result, optionally surfacing the
most-recent `TestVerdict.recorded_at` as "last verified at" (which can
be later than `TestRun.finished_at` if there were cache-hit
attributions since). No exact match ⇒ answer "no data" and offer to
queue a one-off:

```
opp_ci run-matrix --project inet --ref v4.5 \
    --os "Fedora 41" --compiler clang-18 --kinds smoke
```

Smarter neighbour-based interpolation ("Fedora 41 not tested, but
Fedora 40 + same compiler passed") is explicitly out of scope for this
plan — see [Future scope](#future-scope).

## Schema additions

Two new tables (`expected_test_results`, `test_verdicts`) plus columns
on the existing tables. The verdict and cache columns previously
proposed for `test_runs` move out — `test_runs` is left as a pure
execution observation.

### `tests` — coord-only after this plan

Drop `expected_result_code` and `expected_result_description` (moved
to `expected_test_results`, below). After this drop, `name` is the
only mutable column on `Test`; every other field is fixed by
`coord_hash`.

### `expected_test_results` — append-only expectation log (new table)

| Column | Type | Notes |
|---|---|---|
| `id` | int | PK |
| `test_id` | int | FK to `tests` |
| `expected_result_code` | enum? | PASS / FAIL / ERROR, or NULL for an explicit retraction |
| `expected_result_description` | text? | optional free-form note |
| `reason` | text? | why the expectation was set (issue link, justification) |
| `set_by` | text | account that set the expectation |
| `set_at` | timestamptz | when |

"Current expectation for a Test" = most recent row by `set_at`. No
row at all = "no expectation ever declared." A row with
`expected_result_code IS NULL` is an explicit retraction
(distinguishable from never-set, and itself an audited event). Edits
are inserts; nothing is ever updated. Index `(test_id, set_at DESC)`
keeps "current expectation" effectively a single-row read.

### `test_verdicts` — per-cell verdict (new table)

| Column | Type | Notes |
|---|---|---|
| `id` | int | PK |
| `matrix_run_id` | int | FK to `test_matrix_runs` |
| `test_id` | int | FK to `tests` |
| `test_run_id` | int | FK to the `test_runs` row whose outcome this cell attributes. Cache hits reuse a prior row; cache misses point at a freshly-queued row |
| `expectation_id` | int? | FK to the `expected_test_results` row in force at recording time. NULL ⇔ no expectation existed when the verdict landed |
| `verdict` | enum? | EXPECTED / UNEXPECTED / UNKNOWN; NULL until the underlying `TestRun` finalizes (stays NULL forever if the run is cancelled) |
| `recorded_at` | timestamptz? | when the verdict was written (= `TestRun.finished_at` for miss-then-execute, = insertion time for cache hits) |
| `created_at` | timestamptz | when the cell was inserted (= matrix submit time) |

Cell lifecycle (queued / running / finished / cancelled) is **derived**
from the underlying `TestRun.lifecycle` — not stored. Avoids two
sources of truth. The cell has at most one promotion event (verdict
written) and is then frozen.

### `test_runs` — pure execution observation

| Column | Type | Notes |
|---|---|---|
| `cache_fingerprint` | text | content-addressable hash; populated at submit time once F4 lands |

**Removed from earlier drafts of this plan**: no `verdict` column, no
`matrix_run_id`, no `cached_from_id` on `test_runs`. Matrix membership
and per-cell verdict live on `test_verdicts`; cache attribution is the
`test_verdicts.test_run_id` pointer (cache hits don't create a new
`TestRun` at all).

### `test_matrix_runs` — counters, verdict, summary

| Column | Type | Notes |
|---|---|---|
| `pass_count` | int | actual outcome counters (over child verdicts, via their `test_run.result_code`) |
| `fail_count` | int | |
| `error_count` | int | |
| `expected_count` | int | cells whose verdict is EXPECTED |
| `unexpected_count` | int | cells whose verdict is UNEXPECTED |
| `unknown_count` | int | cells whose verdict is UNKNOWN (no expectation existed at recording time) |
| `cache_hit_count` | int | cells whose `test_run_id` points at a TestRun that already existed at cell-insertion time (zero until F4 lands) |
| `total_count` | int | |
| `actual_summary` | enum | PASS / FAIL / ERROR — worst actual across cells |
| `verdict` | enum | EXPECTED / UNEXPECTED / UNKNOWN — same enum as the per-cell verdict, rolled up |
| `ref` | text | git ref / tag the run is against, if any (snapshotted from the triggering event) |
| `completed_at` | timestamptz? | null until the last cell finalizes |

(`trigger` already exists on `TestMatrixRun` from phase 1.)

Verdict rollup rules (evaluated in order):

- `UNEXPECTED` — at least one cell's actual diverged from its
  expectation (this covers unexpected errors as well — an unexpected
  ERROR is just an UNEXPECTED cell).
- `UNKNOWN` — no UNEXPECTED cells, but at least one cell finalized
  with `expectation_id IS NULL` (no `ExpectedTestResult` row existed
  for that `Test` at recording time, or the most recent row was a
  retraction). The matrix has actual results but isn't fully
  *characterised* yet.
- `EXPECTED` — every cell had an expectation in force at recording
  time and the actual matched it. This is the release-ready state.

Release-readiness is then a one-liner: `verdict == EXPECTED` on the
`TestMatrixRun` triggered by the release tag.

### `auto_test_rules` — tag pattern

| Column | Type | Notes |
|---|---|---|
| `tag_pattern` | text? | glob/regex matched against tag-push events |

## Features in detail

### F1 — `TestMatrixRun` rollup

`TestMatrixRun` rows already exist; this feature adds the eager rollup
over the new `TestVerdict` cells. Each time a cell finalizes (either
because its underlying `TestRun` finishes, or because it was inserted
as a cache hit with the verdict already populated), a transactional
update bumps the relevant counters on the parent row and recomputes
`actual_summary` and `verdict`. Once the final cell lands,
`completed_at` is set.

The rollup is stored, not recomputed — the UI and API never have to
fan out across thousands of cells to render a verdict. This supersedes
phase-1's "rollup in app code" placeholder, which was a deliberate
phase-1 simplification.

The per-cell `verdict` lives on `TestVerdict` so the rollup is a
simple counter increment rather than a join. Because the verdict is
computed against the `ExpectedTestResult` row in force *at the moment
the cell finalizes* (and pinned via `TestVerdict.expectation_id`),
later expectation inserts do not retroactively change historical
verdicts or counters — a `TestMatrixRun` is a snapshot of "what we
knew when this ran". This is the right behavior for append-only
history but is worth flagging to users in the UI.

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

Expectations live in `expected_test_results` — an append-only edit log
keyed by `test_id`. They are **not** part of a matrix spec, and matrix
expansion does not modify them. A `Test` with no `ExpectedTestResult`
row, or whose most recent row has `expected_result_code IS NULL`,
means "no expectation declared" — not "PASS by default".

Phase-1 stored expectations as mutable columns on `Test`. This plan
moves them out into their own table so:

- Every edit is audited natively (no bolted-on history table).
- The `reason` / `set_by` / `set_at` triple has a real home, rather
  than being lost on every overwrite.
- `Test` becomes coord-only, aligning with the content-addressable
  identity of every other field on it.

The contribution this plan makes is the *grading layer*:

- A `TestVerdict` row per cell of a matrix run, with:
  - `expectation_id` — FK to the `ExpectedTestResult` row used at
    recording time (NULL if no expectation existed)
  - `verdict` — `EXPECTED` if `test_run.result_code` matched the
    expectation, `UNEXPECTED` if it diverged (including unexpected
    ERROR), `UNKNOWN` if `expectation_id IS NULL`
  - `recorded_at` — when the verdict was written
- The matrix-run rollup over those (`expected_count`,
  `unexpected_count`, `unknown_count`, and the derived
  `TestMatrixRun.verdict`).
- A UI / REST surface to insert new `ExpectedTestResult` rows.

Inserting a new expectation applies *forward only* — historical
verdicts and rollups are not recomputed, because each `TestVerdict`
pins the specific `ExpectedTestResult` row it used via
`expectation_id`. A future matrix run picks up the newly-current row;
an old verdict keeps its pinned reference and stays
reconstructible — that's what makes the dashboard's "release run on
tag X-Y-Z had verdict EXPECTED" a stable, audit-grade claim.

CLI convenience for bulk-setting expectations (writes a batch of
`ExpectedTestResult` rows sharing `set_by`, `set_at`, and `reason`):

```
opp_ci set-expectation --project inet \
    --where os="Ubuntu 24.04" \
    --expect pass

opp_ci set-expectation --project inet \
    --where os="Fedora 41",compiler=gcc-14 \
    --expect fail \
    --reason "tracked in #432"
```

`--expect none` (or `--retract`) inserts retraction rows
(`expected_result_code = NULL`) for the matched tests — distinguishable
from never-set and itself audited. The command does not run any
matrix; it only edits expectations. Running the matrix afterwards
picks up the new expectations through normal grading.

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

Submission flow (per cell of an expanding matrix):

1. Resolve the moving parts (git ref → SHA, dep pins, recipe SHA,
   image SHA) and compute `cache_fingerprint`.
2. Look up the most recent `TestRun` with `lifecycle == finished` and
   the same `cache_fingerprint`. PASS, FAIL, and ERROR all count as
   deterministic — only cancelled / timed-out / not-yet-finished runs
   are cache-misses.
3. **Hit**: insert only a new `TestVerdict` row, with `test_run_id`
   pointing at the matched (existing) `TestRun`. Compute `verdict`
   immediately against the currently-in-force `ExpectedTestResult`
   row, set `expectation_id` and `recorded_at`. Bump `cache_hit_count`
   on the parent `TestMatrixRun`. No `TestRun` row is created — the
   prior execution stands as the system of record for the outcome.
4. **Miss**: insert a new `TestRun` with `cache_fingerprint`
   populated, queue it for a worker, and insert a `TestVerdict`
   pointing at it with `verdict = NULL`. When the run finishes, the
   verdict is computed against the then-current `ExpectedTestResult`
   and the cell finalizes.

`expected_result_code` is **not** in the cache key — expectations are
post-hoc annotations. A cached cell still gets a fresh verdict
comparing its (cached) actual against the (currently-in-force)
expectation.

Implications worth flagging:

- A single `TestRun` can be referenced by many `TestVerdict` rows
  across different matrix runs. That's the whole point of pulling the
  verdict out: provenance for the outcome is shared cleanly without
  duplicating outcome blobs.
- "Most recent observation for these coords" splits into two
  well-defined queries: most recent `TestRun.finished_at` (last actual
  execution) and most recent `TestVerdict.recorded_at` for the same
  coords (last attribution, including cache hits). The CLI can surface
  both.

`--no-cache` on `opp_ci run-matrix` bypasses cache lookup entirely and
forces a fresh `TestRun` per cell.

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
| `opp_ci set-expectation` | (new) Insert `ExpectedTestResult` rows for matching `Test`s. `--expect pass|fail|error|none`, `--reason`. Sugar over the REST endpoint. |

Modified commands:

- `opp_ci auto-test-rule create` — accepts `--tag-pattern` in addition
  to the existing branch options.

`opp_ci run` (the single-test command) is untouched.

## REST API

Mirror of the CLI:

- `POST /api/matrix-runs` — body = spec JSON → returns `{id, status: queued}`.
- `GET /api/matrix-runs/<id>` — rollup + paginated cells (`TestVerdict` rows joined to `TestRun`).
- `GET /api/matrix-runs?project=…&verdict=UNEXPECTED&since=…` — list view.
- `POST /api/tests/<id>/expectations` — body = `{expected_result_code, expected_result_description, reason}`; inserts a new `ExpectedTestResult` row. `expected_result_code: null` is a retraction.
- `GET /api/tests/<id>/expectations` — paginated history for the Test.
- `POST /api/auto-test-rules` — body now accepts `tag_pattern`.

## Web UI

Two new pages:

- **Matrix runs index** — table of recent `TestMatrixRun` rows with
  verdict, counters, trigger, and link to the underlying matrix.
- **Matrix run detail** — rollup header + per-cell `TestVerdict`
  table. UNEXPECTED rows highlighted. Cache-hit cells (whose
  `test_run.finished_at` significantly predates `recorded_at`) carry a
  "cached from TestRun #N" tag. Click a cell ⇒ underlying `TestRun`
  detail page. Inline editor on each row inserts a new
  `ExpectedTestResult` (with a "future runs only" note explaining the
  snapshot semantics).

The project page (existing) gains a "Latest release run" card showing
the most recent tag-triggered `TestMatrixRun` with its verdict — the
at-a-glance answer to Q1.

## Phased implementation

Each phase is independently useful and ships independently.

### Phase 1 — Schema reshape, rollup, counters, matrix-run pages

- Add the counter / `actual_summary` / `verdict` / `ref` /
  `completed_at` columns to `test_matrix_runs`.
- Add the `expected_test_results` table; drop `expected_result_code`
  and `expected_result_description` from `tests`. Existing values are
  migrated as initial `ExpectedTestResult` rows (one per non-null
  expectation, `set_at = migration_time`, `set_by = "migration"`).
- Add the `test_verdicts` table. Matrix expansion now creates
  `(TestRun, TestVerdict)` pairs; the verdict cell carries the
  matrix-run membership that previously rode on `TestRun`.
- Replace phase-1's app-side roll-up with an eager transactional
  update: on each `TestVerdict` finalization, recompute the parent
  rollup atomically.
- Verdict computation at finalization time: read the currently-in-force
  `ExpectedTestResult` row for the cell's `Test`, write
  `TestVerdict.expectation_id`, `TestVerdict.verdict`, and
  `TestVerdict.recorded_at`.
- `opp_ci show-matrix-run <id>` and `opp_ci list-matrix-runs` (CLI +
  REST).
- Web UI matrix-runs index + detail pages.

Already useful: every matrix run now has a single stored
`actual_summary` + `verdict` + counters answerable in O(1); expectation
edits are natively audited; `Test` is coord-only.

### Phase 2 — Anonymous-matrix launcher surface

- Extend `opp_ci run-matrix` with axis flags and `--spec-file`.
- `POST /api/matrix-runs` accepts inline spec.

Schema-wise nothing new — anonymous-matrix persistence is already in
place from phase-1 schema. This phase is purely launcher ergonomics.

Already useful: ad-hoc cartesian-product runs without authoring a
named matrix.

### Phase 3 — Expectation editing UX

- Inline expectation editor on the matrix-run detail page (rows whose
  verdict is UNKNOWN or UNEXPECTED) — each save inserts a new
  `ExpectedTestResult` row.
- `opp_ci set-expectation` CLI + matching REST (including
  `--expect none` / `--retract` for explicit retractions).
- Reason / description editor.
- History view: per-Test timeline of expectation changes (who, when,
  why), rendered straight from `expected_test_results`.
- Documentation explaining the "edits are forward-only; historical
  matrix-run verdicts pin a specific ExpectedTestResult row via
  `TestVerdict.expectation_id`" semantics.

The schema (`expected_test_results` table and
`TestVerdict.expectation_id` FK) lands in Phase 1 above. This phase is
the UX that turns it into a workflow: maintainer looks at a red matrix
run, declares which UNKNOWN cells should be expected to fail, re-runs
to confirm, achieves `verdict == EXPECTED`.

Already useful: known-broken combinations stop polluting release
verdicts, "is this characterised yet?" is queryable per-Test, and the
audit trail for who-changed-what-and-why is first-class.

### Phase 4 — Content-addressable cache

- Add `cache_fingerprint` column to `test_runs`; add `cache_hit_count`
  rollup on `test_matrix_runs`.
- Fingerprint computation in `opp_ci/fingerprint.py` — resolves moving
  refs, dep pins, recipe SHA, image SHA at submit time.
- Cache lookup at submission keyed on `cache_fingerprint`. On a hit,
  the cell's `TestVerdict.test_run_id` points at the existing
  `TestRun`; no new `TestRun` is created. On a miss, both a `TestRun`
  and a `TestVerdict` are created and the run is queued.
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
- **Retroactive verdict recomputation.** Inserting a new
  `ExpectedTestResult` row only affects future runs today; historical
  `TestVerdict.expectation_id` stays pinned to whatever was in force
  at recording time. A "recompute verdicts for matrix runs since date
  X" tool could re-grade historical rollups against the now-current
  expectation — useful if a long-standing misclassification is
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
  time to stamp each child run. This plan instead keeps expectations
  in `expected_test_results`, keyed by `Test`: one expectation per
  coordinate, audited, edited like any other metadata, not duplicated
  across every matrix that hits the same cell.

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

2. **Audit / history for `Test.name`.** Expectation history is
   now natively covered by `expected_test_results`. `Test.name`
   remains the one mutable column on `Test` and is still silently
   overwritten. Likely fine — `name` is a display label, not part of
   grading — but flag if rename history ever becomes important.

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

8. **Verdict for cached cells.** A cache hit reuses a prior `TestRun`
   and grades against the *current* `ExpectedTestResult`. If the
   expectation changed since the cached run finished, the new
   `TestVerdict` reflects the new expectation while the underlying
   `TestRun` reflects old code. That's consistent with everything else
   (verdict is a snapshot at recording time, pinned via
   `expectation_id`), but the "actual" half is staler than the
   expectation half. The UI should surface "cached from TestRun #N
   (finished at T)" on cells whose `test_run.finished_at` is
   meaningfully earlier than the cell's `recorded_at`.

9. **Conflict between matrix-expansion expectation hints and
   `ExpectedTestResult`.** If we ever bring back any form of
   per-matrix expectation override (e.g. "this release matrix expects
   PASS even though the current `ExpectedTestResult` for this Test
   says FAIL"), the storage model needs another layer. Out of scope
   for now, but flag if it becomes a real request.
