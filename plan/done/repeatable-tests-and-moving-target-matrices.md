# Repeatable Tests & moving-target matrices

Status: **final — ready to implement.** Design is *resolve in place* (one entity,
two states); all design decisions below are settled. Phases are sequenced in
*Implementation phases*; deferred items are listed under *Out of scope for v1*.

## Goal

Two tensions that pull in opposite directions, reconciled by one pattern — plus
a capability the same pattern unlocks:

1. **A Test must be fully repeatable** — pinned *all the way down*. Re-running
   the same Test builds bit-for-bit the same inputs.
2. **A TestMatrix often tracks a moving target** — e.g. "test `base..topic` on
   repo X" while the topic branch keeps advancing.
3. **A Test may be submitted underspecified** — loose project/dep **versions**
   and coordinate axes that `resolve()` pins against the **available worker
   fleet** (worker tags), so you needn't know up front the exact build a worker
   offers. The resolved Test/TestMatrix then runs.

## The pattern: resolve in place (one entity, two states)

Each of **Test** and **TestMatrix** is a *single type* carrying `is_resolved`
and `resolved_from`. It exists in one of two states, and resolution is an
endomorphism — `resolve(unresolved) → a new resolved instance`:

| State | Role | Runnable? |
|---|---|---|
| **unresolved** | the *recipe*: coordinate axes + a source spec with **moving** refs (`base..topic`, a branch) + version **constraints** | No — must `resolve()` first |
| **resolved** | the *pinned unit*: source **commit SHA** + complete **transitive dependency lock**; content-addressed; immutable; `resolved_from` → its recipe | **Yes** |

- Resolution **mints a new instance** and preserves the recipe; re-resolving the
  same recipe later mints another resolved instance and never mutates prior
  ones. That lineage *is* the `base..topic` per-push history.
- Only resolved instances may have a **TestRun / TestMatrixRun**. The recipe is
  inert: creating a Run against an unresolved instance is **rejected at submit**
  (same spirit as `validate_test_coord`).
- **Two distinct operations** (see *Resolve vs expand* below): *resolve* pins a
  recipe (Test **or** TestMatrix) in place; *expand* fans a TestMatrix out into
  its Tests. A resolved matrix never points at unresolved Tests.
- `resolve()` pins against **three sources of truth**: the remote repo
  (refs → SHA), opp_env (dep constraints → transitive lock), and the **worker
  fleet** (loose versions / coordinate axes → a concrete value some worker's
  **tags** can satisfy). All three must succeed or the resolution is rejected.

Why a flag rather than two distinct types: one "Test" concept users reason
about, uniform resolution (`T→T`), and a small delta on the current DB — the Run
tables already exist, we just gate them. The cost: the "can't run a recipe"
invariant is enforced **by validation**, not by the schema.

### Resolve vs expand (two distinct operations)

- **resolve** — `Test → Test` or `TestMatrix → TestMatrix`. Makes every loose
  input concrete: refs → SHA, a `base..topic` range → the concrete commit set,
  loose versions / coordinate axes → worker-tag-matched values, dep constraints
  → a transitive lock. **Cardinality-preserving** — a matrix stays one matrix,
  its axes just become concrete value-sets.
- **expand** — `TestMatrix → {Test}`. The structural fan-out: cartesian product
  of the matrix's axes (the commit set contributes one point per commit). **1
  matrix → N Tests.** Only a TestMatrix expands.
- **Composition.** Expanding a *resolved* matrix yields *resolved* Tests;
  expanding an *unresolved* matrix yields *unresolved* Tests (each then
  resolvable). Canonical pipeline for a moving matrix: **resolve → expand →
  run** (the resolved matrix's `TestMatrixRun` runs the expanded Tests).
- **Caveat — worker-tag resolution is per-coordinate.** `version=latest` can
  resolve differently on a linux vs a win worker, so that slice of *resolve*
  only finishes once the coordinate exists. The design is a two-stage order:
  resolve matrix-level inputs (source ref→SHA, range→commits, shared dep
  constraints) → expand → resolve each Test's per-coordinate inputs (worker-tag
  version, per-coordinate lock).

### Invariants to hold

1. `is_resolved` means **fully pinned** — a true boolean, not a spectrum ("refs
   resolved but deps not"). Otherwise "runnable iff resolved" gets fuzzy.
2. Resolved instances are **terminal and detached** snapshots — never
   re-resolved, never mutated by a later resolution of their recipe.
3. **Two hashes:** `spec_hash` over the recipe (so re-submitting the same recipe
   dedups it) and `coord_hash` / `matrix_hash` over concrete inputs (resolved
   identity). One recipe → many resolved hashes over time = the moving-target
   lineage.

## Other options considered (not chosen)

- **Distinct types (manifest → lock → pinned unit):** make the recipe a separate
  type (`MatrixSpec` / `TestMatrix` / `Test`, à la Cargo/npm/Nix
  manifest+lockfile). Gives the "can't run a recipe" invariant **by
  construction** — no Run table can even reference a spec — but more
  types/tables and a non-uniform resolution. The main runner-up.
- **Two-tier (drop the snapshot entity):** recipe expands straight to pinned
  Tests; resolution is just timestamped metadata. Simpler, but loses the "what
  the recipe looked like at time T" snapshot and run-to-run diffing.
- **Pin the run, not the identity (status-quo+):** keep Test as a moving
  coordinate; each `TestRun` records its inputs and "repeatable" means *replay
  run R*. Smallest delta — but Tests stop being the unit of repeatability and
  history is per-run, never per-commit.
- **Vendor/snapshot deps instead of version-lock:** snapshot dependency sources
  into the resolved instance rather than a version lock. Bit-for-bit even if
  upstream rewrites tags; heavier storage, partly duplicates opp_env.
- **Pure content-addressed, no entities:** a recipe is a generator function, a
  resolved unit is just its output hash. Maximally cache-friendly — but nothing
  to name, list, or hang UI/history off of.
- **Branch-tracking as first-class:** "track branch X" auto-creates a resolved
  Test per push via webhook; `base..topic` is a special case. Good CI
  ergonomics; couples the model to a push/webhook source. (Complementary — the
  webhook can just call `resolve()`.)

## "Pinned all the way down" = what `resolve()` pins (three axes)

Resolution pins three axes into the resolved instance's identity:

1. **Project source** — a specific **commit SHA**, never a moving ref
   (`branch`, `git`, `latest`).
2. **Dependencies** — a complete **transitive lock**: every project opp_env
   will build, resolved to a concrete version. Not just direct deps, and never
   "latest at build time."
3. **Coordinate / version** — loose axes (`version`, compiler, flavor, …)
   pinned to concrete values matched against **worker tags**, so the resolved
   coordinate is one the fleet can actually build and run.

## Resolving a loose axis (preference order)

When a loose axis has several candidates, resolution is **deterministic** —
re-resolving against the same fleet tags yields the same value. (A *new* worker
version legitimately changes the result, but that mints a new resolved instance,
never mutates an old one.)

Per loose axis, `resolve`:

1. **Candidate set** — worker-tag values for that axis that satisfy the recipe's
   constraint (`6.x`, `gcc>=11`, or "any"). A value **no worker offers** is not a
   candidate, so the pick is always schedulable. Empty set → reject-incomplete
   (decision #7).
2. **Pick the best** by that axis's **total preference order**:
   - **Ordered axes** (`version`, and each dep version): highest matching version
     (semver-descending; prereleases excluded unless the constraint asks) —
     i.e. "latest stable."
   - **Categorical axes** (`os`, `distro`, `flavor`, `arch`, `compiler` family,
     `mode`, `toolchain`): a **configured ranked list**, first available wins.
     Defaults (coordinator config, recipe-overridable):
     - `mode`: `release` → `debug`
     - `compiler`: `clang` → `gcc` → `msvc`, then newest version of the pick
     - `os`: `linux` → `macos` → `windows`
     - `arch`: prefer host/native, else a configured list
     - `distro` / `flavor`: a configured per-os list
3. **Tiebreak** — if candidates still tie, lexical order on the tag string
   (guarantees determinism).

**Overrides.** A recipe may pin an axis hard (`compiler: gcc-13`) or supply its
own preference list; otherwise the coordinator defaults apply.

**Inter-axis order (the per-coordinate caveat).** Resolve the platform-defining
axes first (`os` → `arch` → `compiler` → `mode`) — a version's available tags
depend on the platform — then resolve `version` and the dep lock **last**, within
the already-chosen platform's worker tags.

## Current model & the gaps it exposes

- `Test.coord_hash` keys on: project name, kind, platform (os/distro/flavor/
  arch/compiler/mode), isolation, toolchain, opp_file, **resolved_deps**.
  It does **NOT** include the project's own commit/ref/version — those live on
  `TestRun` (`commit_sha`, `git_ref`, `version`). → **A Test is not pinned to a
  source commit today; two runs of one Test can build different commits.**
  (Prior work `plan/done/test-identity-includes-deps.md` already folded deps
  into identity; this extends the same idea to the project source.)
- `resolve_dependencies` reads only **direct** `required_projects` — not
  transitive. opp_env builds the full closure, so the lock can miss versions
  that actually affect the result.
- The single-run submit path only resolves deps when `--pin` is given;
  otherwise `resolved_deps` is `None` (how run #1 ended up unpinned).
- `pins` vs `resolved_deps`: `pins` is **transient submit input** (constraints,
  not persisted); `resolved_deps` is the **lock** that keys identity. Keep that
  split, but: the lock must **always** be present, **complete**, and
  **transitive**, and it is the only persisted dependency artifact.
- **How resolve-in-place closes these:** the *resolved* state puts the source
  SHA in `coord_hash` and carries the complete transitive lock; the *recipe*
  (unresolved) holds the `pins` constraints. Same split — now mandatory and
  transitive on every resolved instance, and a recipe simply can't run.

## base..topic matrices (the moving target)

- The **unresolved TestMatrix** (the recipe) gains a **source spec**: a repo
  (the `Project` / `git_url`) + a ref expression that may be a **range**, e.g.
  `base..topic` (git: reachable from `topic`, not `base`). Sits alongside /
  generalizes today's `refs` axis (comma-separated refs/tags).
- `resolve()` pins the range → the **concrete commit set**; `expand()` then fans
  the resolved matrix out into one resolved Test per (coordinate × commit ×
  dep-lock). Running is a third step: a **TestMatrixRun** runs the resolved
  matrix's Tests.
- The topic branch advancing only matters at the *next* `resolve()`; everything
  already resolved stays pinned and replayable.

## Design decisions (settled)

1. **Project commit is in Test identity.** Realized as the *resolved* state (SHA
   in `coord_hash`). Each commit is a distinct resolved Test — gives
   `base..topic` per-commit history; "track a branch" re-resolves the recipe into
   a new resolved Test per push.
2. **`base..topic` expand policy: configurable, default = tip.** Other modes:
   every commit in the range (bisect-grade, expensive) or tip vs
   merge-base(base). Set per-recipe.
3. **Dependency policy: resolve-then-freeze the full transitive lock** (lockfile
   model). Humans pin what they care about via `pins`; the resolver locks the
   rest, once, into `resolved_deps`. We do **not** require every version to be
   explicitly pinned.
4. **Resolve and expand both run on the coordinator at submit.** `resolve` uses
   `git ls-remote`/`rev-list` (refs/range→SHA), opp_env (transitive lock), and
   the worker registry (tag matching); `expand` is the cartesian fan-out. The
   coordinator must therefore have opp_env and worker visibility.
5. **Config schema: the existing `refs` axis carries the source spec.** A `refs`
   value may be a single ref, a comma list, or a **range** (`base..topic`); the
   repo stays the matrix's `Project` / `git_url` (no new top-level key). The
   `versions` axis stays, but loose values (`latest`, `6.x`) are allowed and
   resolved against worker tags.
6. **Caching keys off resolved identity.** `cache_fingerprint` derives from
   `coord_hash` / `matrix_hash`, so a `base` commit unchanged across topic pushes
   reuses prior results — cheap incremental CI.
7. **Reject-incomplete.** A `resolve()` that can't fully pin — source isn't a
   concrete commit, the transitive lock can't be produced (e.g. omnetpp absent,
   which all opp_ci projects depend on), or no worker advertises tags satisfying
   a loose axis — fails rather than minting a half-resolved instance. An
   unresolved instance can never run anyway.
8. **DB is recreated, no migration.** Test/TestMatrix gain `is_resolved` +
   `resolved_from`; recipe and resolved rows share a table; source commit enters
   identity.
9. **Worker-tag resolution pins the value, not the worker.** A loose axis
   resolves to one value by a deterministic **per-axis preference order** (see
   *Resolving a loose axis* — ordered axes take newest, categorical axes take a
   configured ranked list), overridable per-recipe. Scheduling then picks any
   matching worker at run time, so a resolved Test re-runs on any worker still
   offering that value.

## Implementation phases

- **Phase 1 — Dependency lock:** transitive resolution; always-present;
  reject-incomplete; identity keyed on the complete lock. (Extends the parked
  "part 1".)
- **Phase 2 — Resolve in place:** add `is_resolved` + `resolved_from`; resolve
  refs→SHA at submit; add the commit SHA to `coord_hash`; gate
  TestRun/TestMatrixRun on `is_resolved`.
- **Phase 3 — Moving-target matrices:** `base..topic` source spec on the recipe;
  `resolve` pins the range, `expand` (policy: tip / all / merge-base) fans it
  into pinned Tests; TestMatrixRun runs the resolved matrix.
- **Phase 4 — Underspecified submit:** loose versions / coordinate axes in a
  recipe; `resolve` pins them against current worker tags (reject if the fleet
  can't satisfy). Independent of Phase 3 — could land alongside Phase 2.
- **Phase 5 — Caching / UI niceties:** reuse across runs when pinned inputs
  match; recipe-vs-resolved badges + lineage in the UI (see §Web UI).

## Web UI: recipe vs resolved at a glance

Yes — a colored **state badge**, but don't lean on color alone:

- **Badge = color + label + icon.** `Recipe` (unresolved) — amber, dashed
  outline, blueprint/✎ icon; `Resolved` (pinned) — green, solid, 🔒/📌 icon.
  Label + icon keep it readable for color-blind users and in dense lists.
- **Make the primary action differ — the strongest signal.** A recipe shows a
  **Resolve** button (and, for a matrix, **Expand**); only a resolved instance
  shows **Run**. State is then obvious from what you *can do*, not just decor.
- **Style recipe rows as provisional:** muted text / dashed border, and render
  loose values with a `~` hint (`~latest`, `~base..topic`, `~deps: constraints`)
  vs the resolved instance's concrete `a1b2c3d`, locked versions, matched tag.
- **Show lineage both ways:** a resolved instance links `resolved_from` → its
  recipe; a recipe shows a count + list of its resolved snapshots (the
  moving-target history). A matrix recipe also shows expand status ("not yet
  expanded" vs "N Tests").
- **Flag tracking recipes:** a recipe whose spec is moving (branch / range) gets
  a small `↻ tracking` marker — it will mint a new snapshot on next resolve.

## Out of scope for v1 (decided deferrals)

- **Bit-for-bit beyond versions — deferred.** v1 treats (commit + transitive
  dep-version lock + coordinate) as "repeatable enough." We do **not** pin
  compiler patch versions or nix store hashes in v1.
- **Build-env archiving — deferred.** A resolved Test is "replayable while the
  fleet still offers its pinned value"; if no worker advertises that tag later,
  it won't run. No env archiving in v1.
- **GitHub-webhook testing — later phase.** When added, the webhook calls
  `resolve()` on a recipe whose source spec is the PR `base..head` range. Not in
  the initial phases.
- **Naming — decided:** resolved instances are auto-named from commit +
  coordinate and always identifiable by `coord_hash` + `resolved_from`; no manual
  naming required.
