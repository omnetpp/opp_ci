# Plan: introduce `distro` and `flavor` as first-class test dimensions

Goal: split the conflated `os` axis into a three-level hierarchy so the
matrix can speak honestly about platforms that today get collapsed:

- **OS** — `Windows`, `Linux`, `MacOS`. The kernel/family. Coarse
  enough to support cross-OS rollup queries ("did this pass on *any*
  Linux?") and to let podman image / executor logic branch correctly.
- **Distro** — `Ubuntu`, `Fedora`, … (Linux only). What today gets
  stuffed into `os`. Carries the package manager, glibc version, and
  the version number a maintainer thinks in.
- **Flavor** — `Kubuntu`, `Xubuntu`, … (distro variant). Same base
  packages as the parent distro but a different desktop / preinstalled
  set. Matters for end-user-facing testing (charts, GUIs) but
  collapses to its parent for headless builds.

Today the `os` column does triple duty:
[`TestRun.os`](../opp_ci/db/models.py#L186) stores `"Ubuntu"` /
`"Fedora"` / `"Windows"` indiscriminately, and
[`_parse_os()`](../opp_ci/scheduler.py#L70) treats anything before the
last space as "the name." That works until you want to ask "is this
project Linux-clean?" without enumerating every distro, or until you
want to test Kubuntu without losing the fact that the underlying
package set is identical to Ubuntu.

Backward compatibility is **not** a concern: existing stored TestRuns
and matrix configs are wiped/regenerated as part of this change. The
plan rewires every surface — schema, parser, CLI, REST, worker tags,
image names — to the new vocabulary directly, with no shim and no
in-place data migration.

## Locked design decisions

| Question | Decision |
|---|---|
| Hierarchy | `os ⊃ distro ⊃ flavor` — each level is optional below the one above |
| New axes | `distro` (+ `distro_version`) and `flavor` (+ `flavor_version`) |
| `os_version` semantics | Version always attaches to the most specific named level. For Linux: `distro_version` (or `flavor_version` if flavor is set). For Windows/MacOS: `os_version` on the OS itself. See [Version placement](#version-placement). |
| Validation | `flavor` requires `distro`; `distro` requires `os == "Linux"`. Cross-OS distros (`os=Windows, distro=Ubuntu`) error at expansion. |
| Backward compatibility | None. Old `"os": "Ubuntu 24.04"` form is no longer accepted; matrices and CLI use `distro` / `flavor` directly. |
| Worker capability tags | `os:<linux\|windows\|macos>`, `distro:<name>-<ver>`, `flavor:<name>-<ver>`. No legacy aliases. |
| Podman image naming | `host-<platform-slug>-...` where `platform-slug` is the most specific named level. See [Image naming](#image-naming). |
| `platform_desc` | Built from the most specific level downward: `"Kubuntu 24.04 (amd64) / gcc-14"`, falling back through distro and OS. |
| Auto-tag detection | Worker `--auto-tags` reads `/etc/os-release` and detects all three levels on Linux; on macOS / Windows only `os:` and `os:<...>-<ver>` are emitted. |

## Mental model: the dimensions form a tree, not a cross-product

The reader's intuition for axes today is "every axis cross-multiplies
every other." That breaks here — flavor is only meaningful inside one
distro, distro is only meaningful inside one OS. The expansion code
must respect this:

- Given `os: ["Linux", "Windows"]`, `distro: ["Ubuntu"]`, the Windows
  branch ignores `distro` (would otherwise yield nonsense
  `(Windows, Ubuntu)` cells).
- Given `distro: ["Ubuntu", "Fedora"]`, `flavor: ["Kubuntu"]`, the
  Fedora branch ignores `flavor`.
- Conversely, a spec that names only `flavor: ["Kubuntu"]` implies
  `distro = "Ubuntu"` and `os = "Linux"` — we look those up rather
  than producing partially-specified cells.

A small registry maps each distro to its OS and each flavor to its
parent distro (see [Registry](#registry)). Expansion uses the registry
to fill in implied parents, and to validate that explicit parents
aren't lying.

## Version placement

`os_version` today serves two purposes that this plan separates:

1. For Linux, it's the version of the distro (24.04 = Ubuntu 24.04).
2. For Windows / macOS, it's the version of the OS itself (Windows 11,
   macOS 15.1).

The split:

| OS | Carries a version? | Field |
|---|---|---|
| `Linux` | No (Linux as a name has no useful version number for our purposes — the kernel version isn't what matrices target) | `os_version` always NULL |
| `Windows` | Yes | `os_version` |
| `MacOS` | Yes | `os_version` |
| Distro (Linux only) | Yes | `distro_version` |
| Flavor (Linux only) | Optional — defaults to the parent distro's version | `flavor_version` |

This means an existing row `{os: "Ubuntu", os_version: "24.04"}` becomes
`{os: "Linux", os_version: NULL, distro: "Ubuntu", distro_version: "24.04"}`.

For `platform_desc`, the version always attaches to the most specific
named level:

- `Kubuntu 24.04` (flavor + flavor_version, or flavor + parent distro_version)
- `Ubuntu 24.04` (distro + distro_version)
- `Linux` (just os — rare, but legal for "any-Linux" runs under podman with image picked by the worker)
- `Windows 11` (os + os_version)
- `MacOS 15.1` (os + os_version)

## Registry

A small static table in `opp_ci/platforms.py`, hand-maintained
alongside the worker images:

```python
DISTROS = {
    "ubuntu":  {"os": "Linux"},
    "fedora":  {"os": "Linux"},
    "debian":  {"os": "Linux"},
    "arch":    {"os": "Linux"},
    "rhel":    {"os": "Linux"},
}

FLAVORS = {
    "kubuntu":  {"distro": "ubuntu"},
    "xubuntu":  {"distro": "ubuntu"},
    "lubuntu":  {"distro": "ubuntu"},
    # Fedora spins (KDE, Xfce, …) belong here when we start testing them.
}
```

Names are stored case-folded (`ubuntu`, not `Ubuntu`). The CLI and
display layer title-case for output. Lookups are done via the
case-folded form.

Functions exposed:

- `resolve_platform(os=None, distro=None, flavor=None)` — fills in
  implied parents from the registry; raises `ValueError` on
  inconsistency (e.g. explicit `os=Windows` with `distro=Ubuntu`).
- `os_for_distro(distro)` / `distro_for_flavor(flavor)` — plain lookups.
- `is_linux_distro(name)`, `is_known_flavor(name)` — predicates used
  by the legacy-OS compat shim.

Unknown distro / flavor names are *not* rejected at this layer — the
matrix may genuinely want to probe a new distro before we've added it
to the registry. Behavior on unknowns:

- Unknown distro with no explicit `os`: warn + default to `os=Linux`.
- Unknown flavor with no explicit `distro`: error — we can't guess the
  parent.
- Unknown distro / flavor with explicit parent: accept, store as-is.

## Data model

### `test_runs` table

| Column | Notes |
|---|---|
| `os` | Allowed values constrained (in app code, not SQL) to `Linux`/`Windows`/`MacOS`/NULL. |
| `os_version` | Always NULL for `os=Linux`. |
| `distro` | NULL for non-Linux. |
| `distro_version` | NULL for non-Linux or when distro is unversioned. |
| `flavor` | NULL when not a flavor. |
| `flavor_version` | NULL when not a flavor or when flavor inherits its parent's version. |

`arch` and `compiler*` columns are unchanged.

### `test_matrices.config` JSON

New optional keys parallel to the existing `os` / `os_version`:

- `distro: [list]`, `distro_version: [list]` — same two-style API as
  `os` ([combined / structured](../doc/test_matrix_dimensions.md#axis-operating-system)).
- `flavor: [list]`, `flavor_version: [list]` — same.

A combined string like `"Kubuntu 24.04"` is parsed into
`(flavor=kubuntu, version=24.04)` rather than `(name, version)` — the
parse function consults the registry to figure out which level the
name lives at. `_parse_platform()` replaces `_parse_os()` and returns
a `(level, name, version)` triple where level ∈ {os, distro, flavor}.

## Matrix expansion

[`expand_matrix()`](../opp_ci/scheduler.py#L246) gets a new helper
`_resolve_platform_axis(config)` that returns a list of
`(os, os_version, distro, distro_version, flavor, flavor_version)`
6-tuples — already cross-producted across the three levels in the
hierarchy-correct way:

1. Start from the most specific axis present (`flavor` > `distro` > `os`).
2. For each named entry, fill in implied parents via the registry.
3. If multiple levels are explicitly named, cross-product *within* the
   compatible subset only — e.g. `os: [Linux, Windows]` with
   `distro: [Ubuntu]` yields `(Linux, *, Ubuntu, *, *, *)` plus
   `(Windows, *, *, *, *, *)`, *not* a `(Windows, Ubuntu)` cell.
4. Apply version cross-products inside each named level using the same
   combined / structured rules the OS axis uses today.

The job dict produced by `expand_matrix()` gains `distro`,
`distro_version`, `flavor`, `flavor_version` keys, all nullable.

`platform_desc` is built by `_build_platform_desc()` (replaces the
current function); the topmost non-null name with its version is
shown, optionally followed by the parent distro/OS in parentheses for
flavors:

- `"Kubuntu 24.04 (Ubuntu, amd64) / gcc-14"`
- `"Ubuntu 24.04 (amd64) / gcc-14"`
- `"Windows 11 (amd64) / msvc-2022"`

The parenthetical disambiguation for flavors is included because a
maintainer skimming a results table for "Ubuntu" results would
otherwise miss Kubuntu rows.

## CLI surface

New flags on `opp_ci create-matrix` and `opp_ci run-matrix`:

| Flag | JSON key | Notes |
|---|---|---|
| `--distro` | `distro` | Comma-separated. Combined style: `"Ubuntu 24.04"`. |
| `--distro-version` | `distro_version` | Triggers structured cross-product. |
| `--flavor` | `flavor` | Combined style: `"Kubuntu 24.04"`. |
| `--flavor-version` | `flavor_version` | Triggers structured cross-product. |

`--os` accepts only `Linux` / `Windows` / `MacOS`. Distro names go to
`--distro`; flavor names to `--flavor`. There is no auto-rerouting —
an unrecognised `--os` value is a hard error.

`opp_ci show-run` / `show-matrix-run` UI gains the new fields in their
default display. The `--filter` option on list commands gains
`distro=`, `flavor=` predicates.

## REST API

`TestRunOut` / `TestMatrixConfigIn` Pydantic models gain the four new
fields. List endpoints (`GET /api/runs`, `GET /api/matrix-runs`) gain
`distro=`, `distro_version=`, `flavor=`, `flavor_version=` query
params parallel to the existing `os=` / `os_version=`.

## Web UI

- Run detail page: show `os / distro / flavor` as a single
  "Platform" block in the existing format used by `platform_desc`.
- Filters bar on the runs and matrix-runs lists: add `distro` and
  `flavor` dropdowns next to the existing `os` dropdown. The dropdowns
  cascade — selecting a distro narrows the flavor list to that
  distro's flavors via the registry.
- Create-matrix form (if present): new fields parallel to the OS
  fields.

## Worker dispatch and capability tags

Auto-tag generation in
[`cli.py`](../opp_ci/cli.py#L1129) (`--auto-tags`) gets a richer
detection pass:

- Read `/etc/os-release` (Linux): emit `os:linux`, `distro:<id>-<ver>`,
  and if the host carries a recognised flavor marker (e.g.
  `VARIANT_ID=kubuntu` or the presence of `kubuntu-desktop`),
  `flavor:<flavor>-<ver>`.
- Windows: emit `os:windows`, `os:windows-<ver>`.
- macOS: emit `os:macos`, `os:macos-<ver>`.

The dispatcher's required-tag computation in
[`api.py`](../opp_ci/web/api.py#L455) is updated to require the *most
specific* named level — if the TestRun names a flavor, require
`flavor:<flavor>-<ver>`; otherwise `distro:<distro>-<ver>`; otherwise
`os:<os>` (or `os:<os>-<ver>` for Windows/macOS).

## Image naming

The podman image tag scheme changes from
[`opp-ci-runner:host-<os>-<ver>-<compiler>-…`](../opp_ci/executor.py#L429)
to:

```
opp-ci-runner:host-<platform-slug>-<compiler>-<compver>-omnetpp-<ompver>
opp-ci-runner:nix-<platform-slug>
```

where `platform-slug` is `kubuntu-24.04` / `ubuntu-24.04` /
`windows-11` / `macos-15` — the most specific named level. Existing
images are not retained; `opp_ci image build-matrix` regenerates the
set under the new naming.

`opp_ci image build` / `image build-matrix` learn the new axes;
manifest generation in `executor.py` mirrors the slug rule.

## Schema

Treat the database as fresh: drop existing `test_runs` /
`test_matrices` data and recreate. The schema lands as a single
alembic revision
`opp_ci/db/migrations/versions/<rev>_distro_and_flavor.py` that:

1. Adds `distro`, `distro_version`, `flavor`, `flavor_version` columns
   to `test_runs`.
2. (Optional during dev) `op.execute("DELETE FROM test_runs")` and
   `DELETE FROM test_matrices` — there's no data worth keeping.

No backfill, no in-place JSON rewrite of stored configs, no
downgrade-with-fidelity. `downgrade()` simply drops the four columns;
matrix configs authored under the new scheme cannot be expressed in
the old one, which is fine — the migration is one-way in practice.

## Files to touch

- [opp_ci/db/models.py](../opp_ci/db/models.py) — add the four columns.
- New `opp_ci/db/migrations/versions/<rev>_distro_and_flavor.py`.
- New `opp_ci/platforms.py` — registry + `resolve_platform()`.
- [opp_ci/scheduler.py](../opp_ci/scheduler.py) — replace `_parse_os` /
  `_resolve_os_axis` with `_parse_platform` /
  `_resolve_platform_axis`; update `expand_matrix` unpacking,
  `_build_platform_desc`, and the example docstring.
- [opp_ci/executor.py](../opp_ci/executor.py) — image-tag function
  (replace `<os>-<ver>` slot with `platform_slug`); plumb through new
  fields to `find_existing_run` cache key.
- [opp_ci/cli.py](../opp_ci/cli.py) — `--distro`, `--distro-version`,
  `--flavor`, `--flavor-version` on create-matrix and run-matrix;
  `--os` restricted to `Linux`/`Windows`/`MacOS`; auto-tag extension.
- [opp_ci/client.py](../opp_ci/client.py) — `submit_run` / `list_runs`
  parameters.
- [opp_ci/web/api.py](../opp_ci/web/api.py) — Pydantic models,
  query-param filters, required-tag computation for dispatch.
- [opp_ci/web/app.py](../opp_ci/web/app.py) — list filters, run detail
  display, create-matrix form (if applicable).
- [opp_ci/web/templates/](../opp_ci/web/templates/) — relevant Jinja
  templates for the runs list, matrix-run detail, and any platform
  badge component.
- [doc/test_matrix_dimensions.md](../doc/test_matrix_dimensions.md) —
  rewrite the "Axis: operating system" section into three sections
  (OS, distro, flavor); update sizing example; add registry note. Drop
  any reference to the old combined `"Ubuntu 24.04"` form on the `os`
  axis.
- [doc/single_test_parameters.md](../doc/single_test_parameters.md) —
  document the four new single-run flags.
- [doc/data_model.md](../doc/data_model.md), [doc/concepts.md](../doc/concepts.md) —
  update field tables.
- [doc/workers.md](../doc/workers.md) — document new capability tags
  and `--auto-tags` behavior.

## Phased implementation

The work is small enough to land as one PR but splits cleanly along
these boundaries if the diff gets unwieldy:

### Phase 1 — schema + registry + scheduler

- Registry module (`opp_ci/platforms.py`).
- Four new columns on `test_runs`; one-shot migration.
- `_resolve_platform_axis()` replaces `_resolve_os_axis()`; job dict
  carries the new fields end-to-end.

### Phase 2 — CLI + REST

- New `--distro` / `--flavor` flags (and `_version` siblings) on
  create-matrix and run-matrix.
- `--os` restricted to `Linux` / `Windows` / `MacOS`.
- REST Pydantic models and query-param filters.

### Phase 3 — worker tags + podman images

- `--auto-tags` reads `/etc/os-release` and emits `os:` / `distro:` /
  `flavor:` tags.
- Dispatcher requires the most specific named level.
- Podman image tag scheme switches to `<platform-slug>`; `opp_ci
  image build-matrix` regenerates the image set.

### Phase 4 — Web UI

- Cascading distro / flavor filters on list pages.
- Platform display block on detail pages.
- Create-matrix form fields.

## Open questions

- **Should Linux have any version field at all?** Plan above says no —
  `os=Linux` with `os_version=NULL` always. Alternative: store the
  kernel `uname -r` for diagnostic value. Likely no, because we don't
  matrix on it; drop the idea unless someone asks for it.

- **Flavor version vs. distro version.** Today Kubuntu 24.04 ≡ Ubuntu
  24.04 — they ship from the same archive on the same day. Storing
  `flavor_version` is therefore almost-always redundant with
  `distro_version`. Should we just *not* have a `flavor_version` field
  and always derive from the parent? Cleaner, but loses the ability to
  pin a flavor to a different point release than its parent (which
  *does* happen for some Ubuntu spins). Plan above keeps the field and
  defaults it to `distro_version` when unspecified.

- **Where does the registry live, code or DB?** Plan above puts it in
  Python (`opp_ci/platforms.py`), edited via PR. Alternative: a
  `platforms` table, edited by the web UI. Code is simpler; the table
  is one more thing to admin. Stay with code unless a maintainer asks.

- **Cross-matrix queries.** Q2 from the [project-test-automation
  plan](project-test-automation.md) ("would this work on X?") gets
  more interesting with a hierarchy: "would this work on Linux at
  all?" is now a meaningful query. Worth a follow-up to make that
  rollup query first-class once both plans land.
