# Git-versioned dependencies — test any git ref of any dependency

Status: **implemented on branch `git-versioned-deps`.** All five phases landed
for **both** isolation modes — `isolation=none` (host nix + nixless) and
`isolation=podman` (a git-ref omnetpp is baked into a per-commit runner image).
Extends *resolve in place*
([repeatable-tests-and-moving-target-matrices.md](../done/repeatable-tests-and-moving-target-matrices.md))
and *Test identity includes deps*
([test-identity-includes-deps.md](../done/test-identity-includes-deps.md))
from the **source project** to **every dependency**.

## Implementation notes (what actually shipped)

- **Identity** ([db/models.py](../../opp_ci/db/models.py)): `dep_identity_token`
  reduces a release string to itself and a pinned git dep to `git:<sha>`;
  `normalise_deps` routes every value through it (release hashes unchanged).
- **Spec/recipe** ([scheduler.py](../../opp_ci/scheduler.py)): `_parse_deps_axis`
  + `dependency.parse_dep_value` accept `git@<ref>`; `matrix_is_recipe` flags a
  moving git dep; `pin_matrix_deps` pins dep refs → commit at resolve time
  (wired into `resolve_matrix_recipe`).
- **Lock** ([dependency.py](../../opp_ci/dependency.py)): `resolve_dependencies`
  tolerates a git pin (bypasses the compatible-version check, walks the dep's
  `-git` node); `complete_lock_for_submit` pins git-ref pins to commits via
  `_pin_git_pins`, so every submit path is pinned-all-the-way-down.
- **Build, isolation=none** ([executor.py](../../opp_ci/executor.py)):
  `_opp_env_pin_args` → `dep_build_token` emits `omnetpp-git@<sha>`; the source
  path is unified onto `name-git@<ref>` (the global `OPP_ENV_GIT_REF`, which
  opp_env never read, is gone); `_opp_env_workspace` separates distinct git
  commits by identity token.
- **Build, isolation=podman**: a git-ref omnetpp is baked into a per-commit
  runner image. The image is content-addressed by a tag-safe slug
  (`dep_tag_slug` → `git-<short8>`) and baked via the build token threaded as
  `omnetpp_build` through `_ensure_runner_image` → `build_runner_image` →
  `_build_nix_runner_image` / `render_containerfile` (the host Containerfile and
  entry-script install `{{ omnetpp_install }}`). The source ref is still the
  bind-mounted host worktree; the dead `OPP_ENV_GIT_REF` injection was removed.
- **Surfaces**: CLI `--deps`/`--pin` accept `git@<ref>`; the web omnetpp field
  (single-test form **and** matrix-create form) routes through
  `parse_dep_value`; `format_resolved_deps`/`dep_display` show
  `omnetpp@omnetpp-6.x (a1b2c3d)`.
- **Tests**: `test_test_identity_deps.py` (git identity), `test_matrix_recipe.py`
  (parse, recipe detection, `pin_matrix_deps`, git-pin transitive lock),
  `test_bare_metal_opp_env.py` (git-dep + git-source build tokens).
- **Incidental**: a back-compat `__getattr__` in
  [db/connection.py](../../opp_ci/db/connection.py) re-exposes `engine` so the
  18 test modules orphaned by the lazy-engine refactor collect again.

## Goal

Today opp_ci can build the *project under test* from any git ref (a branch,
tag, range, or SHA via the `refs` axis), but a **dependency** can only be
pinned to an **opp_env-registered version** (a release tag, or `git` which
tracks `master`). You cannot say "test inet master against the omnetpp **6.x
branch**" because `omnetpp-6.x` is a git branch, not an opp_env version.

**Target capability:** any dependency in the closure may be pinned to an
arbitrary git ref (branch / tag / commit SHA) of its repository, with the same
*resolve-in-place* guarantees the source already has — a moving ref makes the
Test/TestMatrix a **recipe**, and `resolve()` pins it to a concrete **commit
SHA** that becomes part of Test identity.

Concretely, after this lands:

```
# CLI
opp_ci matrix create inet --refs master --deps "omnetpp=git@omnetpp-6.x"
# resolves to: inet@<sha1>  ×  omnetpp git @ <sha2>   (both pinned, repeatable)
```

## Key insight: opp_env already does the hard part

opp_env's downloader already supports **per-project git refs** via the
`name@ref` syntax — `chop_branch_names`
([opp_env.py:1718](../../../opp_env/opp_env/opp_env.py#L1718)) splits a project
token on `@` and feeds the suffix to a full-clone `git checkout <ref>`
([opp_env.py:1269](../../../opp_env/opp_env/opp_env.py#L1269)). Because it is a
full clone (not `--single-branch`), the ref may be a **branch, tag, or bare
commit SHA**. Every project that has a `-git` variant (omnetpp-git, inet-git,
mm1k-git, …) can therefore be built at any commit:

```
opp_env install omnetpp-git@omnetpp-6.x        # branch tip
opp_env install omnetpp-git@<40-hex-sha>       # exact commit (repeatable)
```

This is **per-project**, so it scales to *multiple* git projects in one
workspace — unlike the current single global `OPP_ENV_GIT_REF` env var the
source path uses ([executor.py:1621](../../opp_ci/executor.py#L1621)), which
can only pin one project. So this plan also **unifies the source and dep build
paths** onto the `name-git@<commit>` pin.

The remaining work is entirely in **opp_ci**: carry a git ref through the deps
axis → recipe detection → resolution → Test identity → the build command.

## What exists today (and what each piece must learn)

| Concern | Source project (works) | Dependencies (today) | Change |
|---|---|---|---|
| Spec | `refs` axis / `ref_range` ([scheduler.py:14](../../opp_ci/scheduler.py#L14)) | `deps` axis = version strings ([scheduler.py:42](../../opp_ci/scheduler.py#L42)) | deps values may carry a git ref |
| Recipe? | branch/range ⇒ recipe ([`matrix_is_recipe`](../../opp_ci/scheduler.py#L365)) | never (deps ignored) | moving dep ref ⇒ recipe |
| Resolve | `resolve_source_commit` / `pin_matrix_refs` ([scheduler.py:422](../../opp_ci/scheduler.py#L422)) | `complete_lock_for_submit` → version lock ([dependency.py:226](../../opp_ci/dependency.py#L226)) | resolve dep ref → SHA |
| Identity | `commit_sha` in `TEST_COORD_FIELDS` ([models.py:232](../../opp_ci/db/models.py#L232)) | `resolved_deps` via `normalise_deps` ([models.py:244](../../opp_ci/db/models.py#L244)) | dep value carries its commit |
| Build | `-git` variant + `OPP_ENV_GIT_REF` ([executor.py:265](../../opp_ci/executor.py#L265)) | `_opp_env_pin_args` → `omnetpp-6.4.0` ([executor.py:492](../../opp_ci/executor.py#L492)) | emit `omnetpp-git@<sha>` |

## Design decisions

### D1 — Deps-axis spec syntax

A deps-axis cell may name a **version** (today) or a **git ref**. Reuse
opp_env's `@` convention so the user-facing syntax matches the tool:

- CLI `--deps` string: `omnetpp=6.4.0,git@omnetpp-6.x;inet=4.5`
  — a value with `@` (or the bare token `git`) is a git ref; otherwise a
  version. `_parse_deps_axis` ([scheduler.py:89](../../opp_ci/scheduler.py#L89))
  keeps splitting on `;` and `,`; `@` survives inside a value.
- Canonical JSON form in `TestMatrix.config["deps"]`: a cell is either a
  version string `"6.4.0"` or a **git-ref object**
  `{"git": "omnetpp-6.x"}` (unpinned recipe) →
  `{"git": "omnetpp-6.x", "commit": "<sha>"}` (resolved).

A git ref always resolves through the `-git` opp_env variant (its `from-git`
download is default — [omnetpp.py:404](../../../opp_env/opp_env/database/omnetpp.py#L404)),
so the dep must have a `-git` variant in the catalog (validated at resolve).

### D2 — Identity is the resolved commit, not the ref label

Mirror the source: `commit_sha` is identity, `git_ref`/branch is descriptive.
A resolved dep's **identity token** is its commit SHA (for a git ref) or its
version string (for a release). So:

- omnetpp@6.x→`sha1` and omnetpp@6.x→`sha2` (branch advanced) are **distinct
  Tests** — correct, the build differs.
- two refs that resolve to the **same** SHA collapse to one Test.
- renaming the branch you came from does **not** re-key identity.

`normalise_deps` ([models.py:244](../../opp_ci/db/models.py#L244)) gains a
canonicalization rule: a string value `"6.4.0"` → identity `"6.4.0"` (back-compat,
unchanged hash); an object `{"git": ref, "commit": sha}` → identity
`"git:" + sha`. The descriptive `ref` is **excluded** from the hash. Same rule
goes into `fingerprint._normalised_deps` so identity and cache fingerprint stay
in agreement ([test-identity-includes-deps.md](../done/test-identity-includes-deps.md)).

> Open question O1: should an *unpinned* git dep (`{"git": ref}` with no
> `commit`) ever reach `compute_test_coord_hash`? No — by D3 it's a recipe and
> recipes aren't run. The hash only sees pinned deps. Guard with an assertion.

### D3 — A moving dep ref makes the matrix/Test a recipe

`matrix_is_recipe` ([scheduler.py:365](../../opp_ci/scheduler.py#L365)) gains:
*any* deps cell that is a git-ref object **without** a `commit` (i.e. a branch
or tag, or a SHA-less ref) ⇒ recipe. A cell already pinned to a 40-hex commit
is resolved. This is the exact analogue of the existing
`any(r and not _is_full_sha(r) for r in refs)` check, applied to the deps axis.

### D4 — Dep resolution: ref → SHA against the dep's own repo

Generalize `resolve_source_commit` ([scheduler.py:422](../../opp_ci/scheduler.py#L422))
to resolve a ref against **any** project's GitHub repo, then call it per git dep
during resolve. The dep must be a registered `Project` with `github_owner`/
`github_repo` — the catalog sync already fills these from opp_env's `git_url`
([opp_env_adapter.py](../../opp_ci/opp_env_adapter.py)). Strict (decision #7 of
the repeatable-tests plan): a git dep whose ref can't be pinned **rejects** the
resolution rather than leaving a moving dep.

Resolution order extends the existing matrix-level stage: source ref→SHA, **dep
refs→SHA**, then the transitive version lock for the rest of the closure, then
expand, then per-coordinate resolution.

### D5 — Transitive lock tolerates a git pin

`resolve_dependencies` ([dependency.py:124](../../opp_ci/dependency.py#L124))
walks the closure and rejects a pin that isn't in a node's `compatible` list. A
git ref is an explicit override and won't appear in that list, so:

- a git-ref pin **bypasses** the `chosen not in compatible` check (the user
  named that ref deliberately — same spirit as the existing "explicit pin lands
  even for a project opp_env can't describe" rule in
  `complete_lock_for_submit`).
- the git dep's **own** transitive requirements come from `opp_env info
  <dep>-git --raw` (the `-git` variant's `required_projects`), which may differ
  from a release's. Query the `-git` node when a dep is pinned to a git ref.

### D6 — Build: per-project `name-git@<commit>` pins, drop global env

`_opp_env_pin_args` ([executor.py:492](../../opp_ci/executor.py#L492)) emits:

- release dep → `omnetpp-6.4.0` (today)
- git dep → `omnetpp-git@<commit>` (new)

The build coordinate hash in `_opp_env_workspace`
([executor.py:533](../../opp_ci/executor.py#L533)) already folds `deps` into the
per-coordinate workspace digest; once `resolved_deps` carries the commit, two
git refs of a dep correctly get distinct workspaces (omnetpp built once per
pinned commit, reused). **Unify the source path** onto the same mechanism:
`<project>-git@<commit_sha>` instead of `OPP_ENV_GIT_REF`, so source and deps
share one code path and multiple git projects coexist (the current global env
var cannot pin more than one project). `OPP_ENV_GIT_REF` is removed once the
podman path ([executor.py:1314](../../opp_ci/executor.py#L1314)) is migrated too.

### D7 — Surfaces (CLI, web, display)

- CLI: `--deps` already accepts the extended syntax via D1; document `git@<ref>`.
- Web matrix/test forms: a deps row gains a "git ref" option beside the version
  dropdown (free-text ref). Reuse the `refs`-axis input pattern.
- Display: `format_resolved_deps` ([dependency.py:276](../../opp_ci/dependency.py#L276))
  renders a git dep as `omnetpp@omnetpp-6.x (a1b2c3d)` (ref + short SHA);
  add a `short_commit`-style helper for deps.

## Implementation phases

1. **Schema/identity core.** `normalise_deps` + `fingerprint._normalised_deps`
   handle the git-ref object shape (D2). Unit-test that a release string and a
   git object hash as designed; that branch-label changes don't re-key; that
   same-SHA refs collapse. No behavior change yet (no producer emits objects).
2. **Spec + recipe detection.** `_parse_deps_axis` accepts `git@<ref>`;
   `_resolve_deps_axis` carries the object through the cartesian product;
   `matrix_is_recipe` flags unpinned git deps (D1, D3). Tests in
   `tests/test_matrix_recipe.py`.
3. **Resolution.** Generalize `resolve_source_commit` to any project; resolve
   git deps → SHA in the matrix-level resolve stage; relax the transitive lock
   for git pins; query the `-git` node for its closure (D4, D5). Tests in
   `tests/test_resolve_in_place.py`, `tests/test_matrix_recipe.py`.
4. **Build.** `_opp_env_pin_args` emits `name-git@<commit>`; migrate the source
   path off `OPP_ENV_GIT_REF` onto the same pin; verify the per-coordinate
   workspace separates distinct dep commits (D6). End-to-end via mm1k podman CI
   (see project memory) building `inet master × omnetpp git@omnetpp-6.x`.
5. **Surfaces.** CLI docs, web form fields, `format_resolved_deps` display (D7).

## Out of scope for v1

- **Auto re-resolution on a *dependency* branch push.** The branch-tracking
  webhook auto-resolve covers the source repo; re-resolving when a *dep's*
  tracked branch advances needs webhooks from the dep repo. Defer — manual /
  on-submit re-resolution works in the meantime.
- **Registering `.x` versions in opp_env.** The `is_git_branch =
  version.endswith(".x")` machinery exists but the entries are commented out
  ([omnetpp.py:443](../../../opp_env/opp_env/database/omnetpp.py#L443)). Not
  needed — `git@omnetpp-6.x` reaches the same branch without minting a version.
  (Could still add them later as friendly aliases.)
- **Git refs for projects with no `-git` opp_env variant.** Require a `-git`
  variant; reject otherwise with a clear message.
- **Git-ref *non-omnetpp* deps under podman.** The per-commit image baking
  threads only the omnetpp dep (the one baked into the image); a git ref for a
  *second* dep under podman would need the same treatment. Release deps of any
  project are unaffected, and isolation=none handles any git-ref dep.

## Risks / verify during implementation

- **R1 — opp_env SHA checkout from a fresh clone.** The clone is full, so
  `git checkout <sha>` should always work; confirm against a shallow-clone
  optimization if one is added (the `--single-branch` TODO at
  [opp_env.py:1270](../../../opp_env/opp_env/opp_env.py#L1270)).
- **R2 — debug `print(git_branch)`** at
  [opp_env.py:1268](../../../opp_env/opp_env/opp_env.py#L1268) should be removed
  on the opp_env side; harmless but noisy in build logs.
- **R3 — existing data.** Per the deps-identity plan, schema is recreated
  (`Base.metadata.create_all`, empty alembic) — no backfill; string-valued
  `resolved_deps` keep their hash (D2 back-compat), so already-resolved
  release Tests are unaffected.
