# Strict test-coordinate specification

## Problem

Run #32 (`mm1k` / `build`) sat queued and was retired as *unserviceable*:

    [opp_ci] expired from queue: no enabled worker advertises the required
    tags {distro:ubuntu}.

The test pinned `distro=Ubuntu` with **no `distro_version`**, so the required
tag was the bare `distro:ubuntu`. No auto-detected worker advertises a bare
distro tag (they all carry a version, e.g. `distro:ubuntu-26.04`), and the
worker/job match is exact set-subset, so nothing could ever claim it.

A first attempt "fixed" the match by making it *hierarchical* (a bare
`distro:ubuntu` requirement satisfied by `distro:ubuntu-26.04`). That was
reverted: it breaks **Test identity**. `distro_version` is part of the
`coord_hash` (`TEST_COORD_FIELDS`), so the single identity
`(distro=Ubuntu, version=None)` would run on 24.04 *and* 26.04 and file both
results under one `test_id` — dedup, expectations, and trend queries all become
lies. The same latent leak already exists one level up (`os=Linux` runs on any
distro; `os_version` is dropped from the Linux required tag entirely).

## Decision: identity must be a total specification

> Every dimension along which two runs can diverge must be concrete in the
> Test identity. An under-specified submission is rejected **at submit time**
> with a clear message — not silently run wherever a worker matches, and not
> left to time out as unserviceable.

Confirmed scope:
- **flavor** optional — a fully-versioned distro (`ubuntu-24.04`) is a valid
  leaf. (Subset matching can't express "plain Ubuntu, not Kubuntu" anyway.)
- **arch** and **mode** mandatory on every test.
- Enforced at submit-time entry points (not inside `get_or_create_test`, which
  is also a low-level primitive used by re-runs, expectations, and unit tests
  with deliberately-minimal coords).
- DB can be recreated — no migration of existing under-specified Test rows.

### Update — resolve-in-place layering (2026-06)

This invariant is unchanged and still load-bearing, but submit-time entry points
now **resolve before they validate**: loose coordinate axes are pinned to a
concrete value first — against the worker fleet for REST/web submits
(`fleet.resolve_loose_axes`) or the local host for the CLI local run
(`cli._detect_capability_tags`) — and a moving source ref is pinned to a commit,
*then* `validate_test_coord` runs. So an under-specified submission is resolved
to a concrete coordinate first and rejected only if resolution can't complete it.
`validate_test_coord` remains the guarantee that every persisted Test identity is
total — i.e. it's exactly what makes resolution safe. See
plan/done/repeatable-tests-and-moving-target-matrices.md.

## The rule — `validate_test_coord(coord)`

Raises `ValueError` (precise, user-facing) when under-specified:

- Always concrete: `project`, `kind`, `arch`, `mode`, `compiler`,
  `compiler_version`, `os`.
- `os = Linux`  → `distro` + `distro_version` required; `os_version` must be
  unset (Linux carries its version in the distro); `flavor` optional but, if
  set, needs a version (`distro_version` suffices).
- `os = Windows | MacOS` → `os_version` required; `distro`/`flavor` must be
  unset.
- Isolation-independent: `podman` runs still need full spec (the coords select
  the container image).

## Enforcement points (submit-time)

1. `web/api.py` single-run submit — before `get_or_create_test`.
2. `web/app.py` single-run form — before `get_or_create_test` (redirect with
   the error message).
3. `cli.py` single-run local path — before `get_or_create_test` (ClickException).
   The `--remote` path delegates to (1).
4. `persistence.enqueue_job` — the funnel every matrix submit passes through
   (and, unlike `expand_matrix`, never hit by previews/counts).

## Knock-on changes

- **Seed matrices** (`scheduler.DEFAULT_MATRICES`): `inet-default` and
  `omnetpp-platforms` omit `arch` → add it. `omnetpp-default` already complete.
- **Worker auto-detection** (`cli._detect_capability_tags`): make
  strict-consistent — always emit an `arch:` tag (fall back to the raw machine
  string so no worker is arch-less, since arch is now mandatory in every test);
  emit the versioned `os:<win|mac>-<ver>` form (drop the vestigial bare one
  when a version is available). Live workers `levy` (no arch) and `local`
  (mis-tagged `os:ubuntu-24.04`, no arch) must be re-detected/updated.
- **Matching stays exact subset** — with full specification it is honest again;
  no hierarchical matching.

## Tests

- `validate_test_coord`: rejects missing arch/mode/compiler_version; rejects
  Linux distro without version; rejects Linux `os_version`; rejects
  distro/flavor on Windows; accepts a fully-specified Linux and Windows coord.
- An end-to-end submit through `enqueue_job` rejects an under-specified job.

## Operational (manual, post-merge)

- Recreate / reseed the coordinator DB.
- Re-detect tags on each worker host (`opp_ci worker detect-tags` → update), so
  every worker advertises `arch:` and a correctly-keyed `distro:`.
