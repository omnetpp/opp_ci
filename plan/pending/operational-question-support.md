# Plan: features to answer the operational questions

Companion to [doc/operational_questions.md](../../doc/operational_questions.md),
which frames four questions a release manager asks opp_ci. This plan
answers those four **and** adds a fifth, development-time question a
maintainer asks *before* a release is even a candidate (the doc does not
yet frame it — see [Mirroring into the doc](#mirroring-into-the-doc)):

1. **Is this release ready?** (a project's own surface)
2. **Will dependent projects work with the release?** (downstream)
3. **Do we support this new OS / compiler?** (platform qualification)
4. **What is the current state of all opp_env projects?** (coverage)
5. **How bad is this backward-incompatible change — how badly are
   dependents affected?** (blast-radius / impact of an in-progress change)

The [test-data-model redesign](../done/test-data-model-redesign.md)
already shipped the primitives these questions reduce to: deduped `Test`
coordinates, `TestRun` observations, append-only `ExpectedTestResult`
expectations, per-cell `TestVerdict` grading, and a stored
`TestMatrixRun` rollup verdict. The
[compatibility report](../../opp_ci/compatibility.py) already overlays
empirical results on declared dependency compatibility, and
[`sync-catalog`](../../opp_ci/opp_env_adapter.py) already mirrors the
opp_env catalog.

What is missing is the layer *above* a single matrix run — the layer that
turns "this matrix passed" into "this release is ready" / "this platform
is supported" / "here is the state of everything." Per
[operational_questions.md → Summary](../../doc/operational_questions.md#summary-whats-answerable-today-vs-whats-missing),
the three missing pieces are a **declared support model**, **graph-driven
expansion** (downstream + platform sweeps), and **aggregation + freshness**.

The fifth question reuses two of those three — graph-driven expansion and
aggregation — and layers on one more *read model*: **baseline-delta
scoring**, which differences two downstream run-sets so a deliberate
breakage is measured by the *new* damage it causes rather than by an
absolute (and uninformative, since it is expected to be red) verdict.

This plan adds exactly those, as read models, generators, and one new
table — not a redesign. The fifth question adds **no** persistence beyond
what the other four already need.

## Why this revisits a previously-rejected idea

The redesign plan
[explicitly rejected](../done/test-data-model-redesign.md#future-scope) a
"support declaration" schema, on the grounds that *matrices serve as the
implicit declaration*. That rejection was specifically about **not putting
the declaration in the opp_env project descriptor** (opp_env stays a build
recipe, not a policy store). This plan honors that: the support model
lives **in opp_ci**, next to the matrices and verdicts that grade against
it. The lesson from the four questions is that "whatever matrix happened to
run" is not a yardstick — "supported" needs a *commitment* to compare
verified results against. That commitment is the support model.

## Locked design decisions

| Question | Decision |
|---|---|
| Where the support target lives | In opp_ci, as a new `support_targets` table keyed by `(project, version-or-line, dependency pins)`. **Not** in opp_env. |
| What a support target declares | A cross-product of platforms (`os`/`distro`/`flavor` + versions, `arch`, `compiler`/version), `modes`, and required dependency versions, plus the `kinds` that must pass. Effectively a *named, persisted matrix spec with the semantics "we commit to this."* |
| Relationship to `TestMatrix` | A support target **is** a TestMatrix with a `is_support_target` flag + a `support_line` label. Reuses `expand_matrix()` verbatim. No parallel expansion engine. |
| Reverse dependencies | Derived, not stored: scan `Project.dependency_names` across the catalog to build the reverse graph on demand. Transitive closure for "all downstream." |
| Downstream qualification | A generator that, given a candidate `(project, version)`, emits one anonymous matrix run per downstream project with the candidate pinned into `resolved_deps`. |
| Release readiness aggregate | A derived (not stored) rollup over a *set* of `TestMatrixRun`s: the project's own release run + every downstream qualification run for the same candidate, tied together by a shared `release_key`. |
| Platform qualification | The same generator, but fixing a new platform axis value and crossing the union of active support targets instead of pinning a dep. |
| Freshness | Derived per coordinate: a verdict is *fresh* iff its backing `TestRun.commit_sha` equals the catalog/HEAD SHA for that version **and** `recorded_at` is within a configurable window. Surfaced everywhere; never stored. |
| Coverage / state view | A read model joining catalog × support targets × latest verdict × freshness. New web **State** page, `opp_ci coverage` CLI, `GET /api/coverage` REST. No new persistence beyond `support_targets`. |
| How a breaking change is scored | As a **diff of two downstream run-sets** (candidate ref vs. baseline ref), not an absolute verdict — a breaking change is *meant* to break things, so only *new* breakage counts. Blast radius = coordinates EXPECTED-and-fresh at baseline but UNEXPECTED at the candidate. Reuses F2 expansion and the `release_key` grouping; no new table. |
| Impact candidate & baseline refs | Candidate is any ref (branch / PR head / SHA), pinned as a pseudo-version (`omnetpp@<sha>`) with the real `commit_sha` on each `TestRun`. Baseline defaults to the changed project's previous released version on the same line (whose downstream runs are likely already present); overridable with `--baseline`. |
| Auto-proposing support targets / interpolation | Deferred — see [Future scope](#future-scope). Same boundary the redesign drew. |

## How each question gets answered after this plan

**Q1 — release ready?** `opp_ci release-status omnetpp-6.4.0` aggregates
the project's own tag-triggered `TestMatrixRun` verdict **and** every
downstream qualification verdict for that candidate (joined by
`release_key`). Ready ⇔ all are `EXPECTED` **and** fresh.

**Q2 — downstream?** `opp_ci downstream omnetpp-6.4.0` resolves the
reverse-dependency closure from the catalog and launches one pinned
qualification matrix per downstream project. Results flow into the
existing compatibility overlay *and* the release aggregate.

**Q3 — new platform?** `opp_ci qualify-platform --distro "Ubuntu 26.04"
--compiler gcc-15` crosses that fixed value with the union of active
support targets, runs it, and reports a per-project support verdict for
the platform.

**Q4 — current state?** `opp_ci coverage` (and the **State** web page)
renders, for every project × version, the declared support target, the
latest verdict per supported coordinate, and whether it is fresh —
intended vs. verified vs. stale, in one table.

**Q5 — how bad is a breaking change?** `opp_ci impact omnetpp@<branch-or-PR>`
reuses the F2 downstream generator to qualify the candidate ref across the
reverse-dependency closure, then **diffs** the resulting verdicts against a
baseline ref (the changed project's previous release, or an explicit
`--baseline`). The blast radius is the set of downstream coordinates that
were EXPECTED-and-fresh at the baseline but are UNEXPECTED at the candidate
— reported as a severity rollup: how many downstream projects *newly*
break, how deep in the graph (direct vs. transitive), broken down by kind
(a `build` break outranks a `smoke` break outranks a `test` break).
Coordinates already broken at the baseline are excluded — they are not this
change's fault.

## Schema additions

One new table; everything else is derived.

### `support_targets` — declared support commitment (new table)

A support target is a persisted, gradeable commitment. It is stored as a
specialization of `test_matrices` so it reuses expansion and grading
unchanged.

| Column | Type | Notes |
|---|---|---|
| `id` | int | PK |
| `project` | text | project name |
| `support_line` | text? | release line this commits to (`6.x`, `4.6`, or NULL = a specific version) |
| `version` | text? | specific version if not a line |
| `config` | JSON | same matrix-spec shape as `test_matrices.config` (platforms, modes, kinds, dep pins) |
| `dependency_pins` | JSON? | required dep versions, e.g. `{"omnetpp": "6.4.0"}` |
| `kinds_required` | JSON | the kinds that must reach EXPECTED for the target to be "met" |
| `active` | bool | whether this target is currently in force |
| `set_by` | text | who declared it |
| `set_at` | timestamptz | when |
| `reason` | text? | justification / policy link |

"Currently supported for project P version V" is then defined, not
guessed: the active support target's cross-product, each cell graded by
its latest fresh `TestVerdict`.

Alternative considered and rejected: a boolean `is_support_target` flag
directly on `test_matrices`. Rejected because support targets carry extra
policy fields (`support_line`, `kinds_required`, `set_by`/`set_at`/`reason`
audit) that don't belong on every matrix; a dedicated table keeps
`test_matrices` lean. The `config` field is still the same shape, so
`expand_matrix()` is reused without change.

### `test_matrix_runs.release_key` — aggregation tie (new column)

| Column | Type | Notes |
|---|---|---|
| `release_key` | text? | groups the own-run + downstream qualification runs of one candidate, e.g. `omnetpp-6.4.0`. NULL for ordinary runs. |

This is the only column added outside the new table. The release-status
aggregate is a query over `WHERE release_key = ?`, not a stored rollup —
consistent with the redesign's "derive lifecycle, store only what can't be
derived" stance.

The fifth question (breaking-change impact) needs **no** further schema: a
candidate run-set and its baseline run-set each get a `release_key`, and
the impact report is a *diff* of those two queries — a read model, not a
stored artifact.

## Features in detail

### F1 — Support model (`support_targets` table + CLI/REST/UI)

- Schema: the table above, plus a thin `SupportTarget` model.
- `opp_ci support set --project inet --line 4.6 --platforms … --compilers
  … --modes release,debug --deps omnetpp=6.4.0 --kinds build,smoke
  [--reason …]` inserts/updates the active target.
- `opp_ci support show [--project inet] [--line 4.6]` prints the declared
  target and, for each cell, its latest verdict + freshness.
- REST: `POST /api/support-targets`, `GET /api/support-targets`.
- Web: a **Support** tab on the project page showing the declared grid
  with live verdict colors (reuses the compatibility-grid renderer).

Already useful on its own: "supported" stops being implicit. The grid is
gradeable against real `TestVerdict`s immediately, before any of the
generators below exist.

### F2 — Reverse-dependency graph + downstream qualification

- `opp_ci downstream <project>-<version> [--transitive] [--dry-run]`:
  1. Build the reverse-dependency graph by scanning
     `Project.dependency_names` across the synced catalog (helper in a new
     `opp_ci/graph.py`; transitive closure for `--transitive`).
  2. For each downstream project, pick the versions to qualify (its active
     support target's versions, else its latest version).
  3. Emit one **anonymous** matrix per downstream project, with the
     candidate merged into `resolved_deps` (a `--pin`), tagged with a
     shared `release_key`.
  4. `--dry-run` prints the plan; without it, launches all runs.
- REST: `POST /api/downstream` (body = candidate + options).
- The runs grade and overlay exactly like any other matrix run — the
  compatibility page (Question 2) lights up automatically because the
  candidate version is in each run's `resolved_deps`.

Already useful: the blast radius of an omnetpp/inet release is one command,
generated from the catalog rather than hand-maintained.

### F3 — Release aggregate (`release-status`)

- `test_matrix_runs.release_key` column.
- The tag-triggered own-run and the F2 downstream runs share the
  `release_key` (e.g. `omnetpp-6.4.0`).
- `opp_ci release-status <project>-<version>`: query all matrix runs with
  that key, roll their verdicts into one aggregate:
  - `EXPECTED` iff every constituent run is `EXPECTED` **and** fresh,
  - `UNEXPECTED` if any is,
  - `UNKNOWN` / `STALE` otherwise (uncharacterised or old evidence).
- REST: `GET /api/releases/<key>`.
- Web: a **Release readiness** card on the project page showing the
  aggregate + a breakdown row per constituent run (own, inet, simu5g,
  veins, …).

Already useful: Question 1 becomes a true ship/no-ship signal that
includes the downstream ecosystem, not just the project's own tests.

### F4 — Platform qualification sweep

- `opp_ci qualify-platform --distro "Ubuntu 26.04" --compiler gcc-15
  [--project …] [--dry-run]`:
  1. Collect the canonical suite = the union of active `support_targets`
     (optionally filtered to one project).
  2. Override the platform axis with the new value, cross with the suite's
     `(project, version, mode, kind)` coordinates.
  3. Launch (or, with `--dry-run`, print) the runs, tagged with a
     `release_key` like `platform:ubuntu-26.04+gcc-15`.
- `opp_ci platform-status --distro "Ubuntu 26.04" --compiler gcc-15`:
  per-project support verdict for the platform (reuses the F3 aggregate
  over the platform `release_key`).
- Worker/image prerequisite is unchanged — the platform must be reachable
  via a capability-tagged worker or a built Podman image; the command
  fails fast with a clear message if neither exists.

Already useful: Question 3 ("do we support gcc-15 / Ubuntu 26.04?") is one
command and a single verdict, against the agreed suite rather than an
ad-hoc matrix.

### F5 — Freshness / staleness

- A `opp_ci/freshness.py` helper: given a `Test` coordinate and the
  version's current head SHA (from the catalog / git), classify its latest
  `TestVerdict` as:
  - **fresh** — backing `TestRun.commit_sha` == head SHA and `recorded_at`
    within `OPP_CI_FRESHNESS_WINDOW` (config, default e.g. 30 days),
  - **stale-sha** — verified, but against an older commit,
  - **stale-age** — verified against head, but older than the window,
  - **unverified** — no finished run.
- Surfaced in F1 `support show`, F3 `release-status`, F4
  `platform-status`, and F6 coverage — never stored, always derived, so it
  can never drift from the underlying runs.

Already useful: "we verified it" gains a half-life. A green-but-ancient
cell stops masquerading as current evidence.

### F6 — Coverage / state dashboard

- `opp_ci coverage [--project …] [--platform …] [--stale-only]`: the
  ecosystem read model. For every project × version, join:
  - catalog (declared deps from `Version.resolved_dependencies`),
  - active support target (intended platforms/compilers/deps),
  - latest `TestVerdict` per supported coordinate,
  - F5 freshness.
  Output: a table with intended-vs-verified-vs-fresh per cell, plus
  per-project rollup (`fully supported & fresh` / `gaps` / `stale` /
  `undeclared`).
- REST: `GET /api/coverage`.
- Web: a top-level **State** page — the at-a-glance answer to Question 4.
  Filterable by platform/compiler (reuses the existing grouped-filter
  controls).

Already useful: a single page answers "what is the current state of all
opp_env projects" — exactly Question 4 — without a redesign.

### F7 — Breaking-change impact / blast-radius report

- `opp_ci impact <project>@<ref> [--baseline <ref>] [--transitive]
  [--kinds …] [--dry-run]`:
  1. Resolve the **candidate** (`<project>@<ref>` — a branch, PR head, or
     SHA) and the **baseline** (default: the project's previous released
     version on the same line; else `--baseline`).
  2. Reuse the F2 downstream generator **twice**: one pinned run-set for
     the candidate (`release_key = impact:<project>@<sha>`), one for the
     baseline (reused if its runs already exist and are fresh, else
     launched).
  3. **Diff** the two aggregates: the blast radius is every downstream
     coordinate that was EXPECTED-and-fresh at the baseline and is
     UNEXPECTED at the candidate. Coordinates UNKNOWN or already broken at
     the baseline are excluded — the change can't be blamed for breakage
     that pre-dates it or was never verified.
  4. Report a **severity rollup**: count of downstream projects newly
     broken, classified by graph depth (direct vs. transitive dependents)
     and by first-failing kind (`build` ≻ `smoke` ≻ `test`), with the
     offending cell per project.
  - `--dry-run` prints which runs would launch (and which baseline runs are
    reused) without launching anything.
- REST: `POST /api/impact` (candidate + baseline + options).
- Web: a **Breaking-change impact** view — the reverse-dep graph rendered
  as a tree, each downstream node colored by its delta (newly-broken /
  still-green / already-broken / unverified), under a severity headline
  ("`omnetpp@feature-x` breaks 7 of 23 downstream projects — 3 at build, 4
  at smoke").

Why it is distinct from F2/F3: those gate a *finished* candidate and expect
green; F7 measures an *in-progress* change that is *expected* to break
things, and isolates the *incremental* damage by differencing against a
baseline. Same generator, opposite expectation, plus a delta.

Already useful: a maintainer about to land a breaking API change sees the
exact downstream cost — who to warn, who to patch, and whether the change
is worth its blast radius — before it ships, not after the release
post-mortem.

## CLI surface (new commands)

| Command | Purpose |
|---|---|
| `opp_ci support set` | Declare/update a project's active support target. |
| `opp_ci support show` | Print declared target + per-cell verdict & freshness. |
| `opp_ci downstream <proj>-<ver>` | Reverse-dep closure → launch pinned qualification matrices (`--transitive`, `--dry-run`). |
| `opp_ci release-status <proj>-<ver>` | Aggregate verdict over own + downstream runs sharing a `release_key`. |
| `opp_ci qualify-platform` | Sweep the support suite across a new OS/compiler value. |
| `opp_ci platform-status` | Per-project support verdict for a platform. |
| `opp_ci coverage` | Ecosystem intended-vs-verified-vs-fresh table. |
| `opp_ci impact <proj>@<ref>` | Qualify an in-progress ref downstream and diff vs. a baseline → blast-radius severity report (`--baseline`, `--transitive`, `--dry-run`). |

All wire through the existing `@remoteable` dual-mode decorator so they
work locally and via `--remote`. No existing command changes behavior;
`run-matrix` / `show-matrix-run` / `set-expectation` are untouched.

## REST API (new endpoints)

- `POST /api/support-targets`, `GET /api/support-targets`
- `POST /api/downstream` — candidate + options → launches qualification runs
- `GET /api/releases/<key>` — release aggregate
- `GET /api/coverage` — coverage read model
- `POST /api/impact` — candidate + baseline + options → launches/reuses the
  qualification runs and returns the blast-radius diff

Mirrors of the CLI; same auth roles (submitter to launch, readonly to view).

## Web UI (new surfaces)

- **Support** tab on the project page (declared grid, live verdicts).
- **Release readiness** card on the project page (F3 aggregate + breakdown).
- Top-level **State** page (F6 coverage), reusing the grouped-filter
  controls and the compatibility-grid renderer.
- **Breaking-change impact** view (F7): the reverse-dep tree colored by
  per-node delta, under a severity headline. Reachable from a project's
  branch/PR context and standalone.

## Phased implementation

Each phase is independently useful and ships independently.

### Phase 1 — Support model (F1)

`support_targets` table + `SupportTarget` model + `support set/show` CLI +
REST + the project **Support** tab. Grades against existing `TestVerdict`s
immediately. Foundation for every later phase (F4/F6 read the targets).

### Phase 2 — Freshness (F5)

`freshness.py` + config window + wiring into `support show`. Small, pure,
and unblocks honest "verified" claims everywhere it's later reused.

### Phase 3 — Reverse-dep graph + downstream qualification (F2)

`graph.py` (reverse closure from `dependency_names`) + `downstream` CLI
(`--dry-run` first) + REST. Results overlay the existing compatibility
grid with no extra work.

### Phase 4 — Release aggregate (F3)

`release_key` column + the tag-trigger and `downstream` generator both
stamp it + `release-status` CLI/REST + project **Release readiness** card.
Turns Q1 into a downstream-inclusive ship signal.

### Phase 5 — Platform qualification (F4)

`qualify-platform` / `platform-status` reusing the F2 generator with a
fixed platform axis + the F3 aggregate over a platform `release_key`.

### Phase 6 — Coverage dashboard (F6)

`coverage` CLI/REST + the **State** web page, joining everything above.
The capstone answer to Question 4.

### Phase 7 — Breaking-change impact (F7)

`impact` CLI/REST + the **Breaking-change impact** web view. Reuses F2's
generator (downstream expansion), F3's `release_key` aggregation, and F5's
freshness (so the baseline is real evidence). Adds only a baseline resolver
and a diff read model — no new persistence. Answers Q5: the development-time
blast radius of an in-progress breaking change. Can ship any time after
Phase 4 (release aggregate); ordered last because it is the most derived.

## Mirroring into the doc

[operational_questions.md](../../doc/operational_questions.md) currently
frames only four questions. The fifth (breaking-change impact) is
introduced here in the plan first because it is the most derived and the
last to ship. When Phase 7 lands — or sooner, if the framing is agreed — it
should be mirrored into the doc as **Question 5**, in the same shape as the
others (what opp_ci answers today, the exact coordinates, the commands, and
the gap), plus a "today's manual recipe" entry (run the F2 downstream
recipe twice — once at the candidate ref, once at the baseline — and diff
the per-project verdicts by hand). Until then, the doc and this plan are
intentionally one question apart, and this section is the note that closes
the gap.

## Future scope

Carried from the redesign's
[future scope](../done/test-data-model-redesign.md#future-scope); no design
or code here.

- **Auto-proposing support targets.** Suggest a target by cross-producting
  the catalog and densifying around passing cells; human promotes it.
- **Neighbour interpolation.** When a coordinate is unverified, infer from
  adjacent platforms with a confidence score (the redesign's deferred Q2
  interpolation), feeding coverage and platform-status.
- **Nightly / per-push re-qualification.** Caching makes re-running support
  targets cheap; a scheduler could keep freshness green automatically.
  Deferred until there's demand (matches the redesign's re-test-cadence
  stance).
- **Support targets in opp_env.** Still rejected — the commitment is an
  opp_ci policy concern, not an opp_env build-recipe concern.

## Open questions

1. **Support line vs. version granularity.** A target keyed on a release
   *line* (`6.x`) implicitly covers future patch versions; one keyed on a
   *version* (`6.4.0`) does not. Which is the default when a maintainer
   runs `support set` without `--line`? Leaning: line if the project has
   one, else version.
2. **Downstream version selection.** When qualifying omnetpp-6.4.0 against
   inet, which inet versions? All currently-supported, just the latest, or
   only those whose declared `required_projects` already admit 6.4.0?
   Leaning: those whose declared range admits the candidate, else flag the
   downstream as "needs a compatibility bump" rather than running a
   guaranteed-red matrix.
3. **Freshness window default.** 30 days, per-project override, or tied to
   release cadence? Likely a config value with a sane default.
4. **Release readiness with UNKNOWN cells.** Does an UNKNOWN (uncharacterised)
   downstream cell block a release, or only an UNEXPECTED one? Leaning:
   UNKNOWN blocks the *aggregate* (you haven't said what should happen),
   matching the per-matrix-run semantics.
5. **Impact baseline selection.** What is the right baseline to diff a
   breaking change against — the changed project's previous released
   version (cheap, its downstream runs likely already exist), the
   merge-base of the candidate branch (most precise about *this* branch's
   incremental damage), or always an explicit `--baseline`? Leaning:
   previous release by default, merge-base when the candidate is a PR with a
   known base, explicit override always available.
6. **Impact and UNKNOWN baseline cells.** A downstream coordinate that is
   UNKNOWN (uncharacterised) at the baseline can't be proven *newly* broken
   — exclude it from the blast radius, or surface it as a separate
   "uncovered, can't tell" bucket? Leaning: exclude from the headline
   count, list separately so the gap is visible rather than silently
   swallowed.
