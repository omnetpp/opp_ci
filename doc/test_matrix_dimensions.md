# Test Matrix Dimensions

A test matrix in opp_ci is a named cross-product over a set of
*axes* — independent dimensions whose values are multiplied together
to produce one [TestRun](concepts.md#testrun) per combination. Matrix
expansion happens in
[scheduler.expand_matrix()](../opp_ci/scheduler.py).

This guide walks through every axis: what it controls, the JSON config
syntax, the matching `opp_ci create-matrix` CLI flag, the defaults, and
the interactions with other axes. For the higher-level vocabulary see
[concepts.md](concepts.md#test-matrix-concepts); for the CLI surface
see [cli_reference.md](cli_reference.md#matrices).

---

## Anatomy of a matrix config

A `TestMatrix` row stores three things: its `name`, the owning
`project`, and a JSON `config` dict. The dict's keys are axis names;
each present key contributes a list whose length multiplies the job
count. An axis omitted from the config contributes a single implicit
value (typically `None` or a hard-coded default).

```json
{
  "test_types": ["smoke", "fingerprint"],
  "modes": ["release", "debug"],
  "versions": ["inet-4.5", "inet-4.4"],
  "refs": ["master", "topic/my-feature"],
  "deps": {"omnetpp": ["6.1", "6.0"]},
  "os": ["Ubuntu 24.04", "Fedora 41"],
  "compiler": ["gcc-14", "clang-18"],
  "isolation": ["none", "docker"],
  "toolchain": ["none", "nix"]
}
```

Expansion is a straight `itertools.product` over the resolved axis
lists ([scheduler.py:267](../opp_ci/scheduler.py#L267)). A 2 × 2 × 2 ×
2 × 2 × 2 config produces 64 jobs; one axis with 10 values turns that
into 640. There is no implicit pruning — every combination becomes a
queued `TestRun`.

The remainder of this guide describes each axis in turn.

---

## Axis: target project

| Aspect | Value |
|---|---|
| JSON key | *(not an axis — fixed on the matrix row)* |
| CLI flag | `--project` (required) |
| Default | none |
| Cross-product | no — exactly one project per matrix |

Every matrix is attached to a single [Project](concepts.md#project).
The project name is stored on the `TestMatrix` row itself, not inside
the config dict, and it is constant across every expanded job. To test
several projects, create several matrices.

The project must already exist in the database (either seeded from the
core catalog or imported by `opp_ci sync-catalog`). When the
[scheduler](concepts.md#scheduler) needs dependency information for
the project, it consults the
[opp_env adapter](../opp_ci/opp_env_adapter.py) — the matrix itself
does not carry that.

---

## Axis: project versions

| Aspect | Value |
|---|---|
| JSON key | `versions` |
| CLI flag | `--project-versions` |
| Default | `[<project-name>]` from the CLI; `[None]` if absent from JSON |
| Cross-product | yes |

Each value names a specific [Version](concepts.md#version) row of the
matrix's project: either a released label (`inet-4.5`, `omnetpp-6.1`)
or a moving target (`inet-git`, `omnetpp-master`). The value becomes
the `version` field of the resulting `TestRun` and drives both
`opp_env install` and dependency resolution.

When omitted entirely, the CLI falls back to a single-element list
containing the project's own name — i.e. "test the project, no
specific version pin." This is the right default for projects with a
single canonical version.

Versions interact with two other axes:

- **`refs`** — when both are present they are cross-producted, so
  `versions: ["inet-4.5"], refs: ["master", "topic/x"]` produces two
  jobs that both install the `inet-4.5` opp_env package but check out
  different git refs at test time.
- **`deps`** — each version row carries its own
  `resolved_dependencies` pin; the `deps` axis overrides that pin per
  job (see [Axis: dependency versions](#axis-dependency-versions)).

---

## Axis: git refs

| Aspect | Value |
|---|---|
| JSON keys | `refs` *or* `ref_range` (mutually exclusive) |
| CLI flags | `--refs`, `--ref-range` |
| Default | `[None]` (test whatever the version installs) |
| Cross-product | yes |

Branches, tags, or commit SHAs to test against, recorded on each
`TestRun` as `git_ref`. Two forms are accepted:

### Static list (`refs`)

```json
{ "refs": ["master", "topic/my-feature", "5be3f7a"] }
```

The list is taken verbatim. Each value becomes one expanded job per
combination with the other axes.

### Dynamic range (`ref_range`)

```json
{ "ref_range": { "base": "v6.0", "head": "master" } }
```

At expansion time the scheduler calls
[GitHubClient.list_commits_in_range()](../opp_ci/github/client.py) and
substitutes the returned SHA list. This means **the matrix re-resolves
the range every time it is expanded**, so a long-lived matrix
automatically picks up new commits as `head` advances. The trade-off:
expansion is no longer a pure function of the stored config — it
depends on GitHub state and requires the project to have
`github_owner` and `github_repo` set.

If both keys are present, `refs` wins; `ref_range` is ignored. The CLI
enforces this as a hard error ([cli.py:607](../opp_ci/cli.py#L607)).

### Interaction with toolchain

When [`toolchain == "nix"`](#axis-toolchain), the executor cannot
check out an arbitrary ref against a released opp_env package — it
switches the project to its `-git` variant (e.g. `inet-git`) and pins
the commit via the `OPP_ENV_GIT_REF` env var. See
[resolve_git_project()](../opp_ci/executor.py#L187). Under other
toolchains, the ref is fed straight to git.

---

## Axis: test types

| Aspect | Value |
|---|---|
| JSON key | `test_types` |
| CLI flag | `--tests` (required) |
| Default | `["smoke"]` if the JSON key is absent |
| Cross-product | yes |

What kind of test each job runs. Recorded on the `TestRun` as
`test_type`; the executor uses it to pick the opp_repl entry point via
[COMMAND_MAP](../opp_ci/executor.py#L102).

| Value | What it runs | opp_repl entry point |
|---|---|---|
| `smoke` | Project's built-in smoke commands. Fast "did it build and run." | `opp_run_smoke_tests` |
| `build` | Compile only, no execution. | `opp_build_project` |
| `fingerprint` | Replay-and-compare deterministic regression suite. | `opp_run_fingerprint_tests` |
| `statistical` | Statistical-property checks on simulation output. | `opp_run_statistical_tests` |
| `feature` | INET-style feature-flag matrix. | `opp_run_feature_tests` |
| `chart` | Renders the project's analysis charts. | `opp_run_chart_tests` |
| `speed` | Wall-clock / perf-counter measurements. Needs perf-capable workers. | `opp_run_speed_tests` |
| `sanitizer` | ASan/UBSan/TSan runs. | `opp_run_sanitizer_tests` |
| `release` | Pre-release sanity bundle. | `opp_run_release_tests` |
| `opp` | OMNeT++ internal regression tests. | `opp_run_opp_tests` |
| `all` | Run every applicable test type in sequence. | `opp_run_all_tests` |

A single `TestRun` may produce many `TestResult` rows — a fingerprint
run, for example, can yield 48 results under one TestRun. See
[TestResult](concepts.md#testresult).

Some test types have hardware or environment requirements (`speed`
benefits from physical perf counters; `sanitizer` needs a compatible
toolchain). These are enforced via
[capability tags](concepts.md#capability-tag) on the worker, not by
the matrix axis itself.

---

## Axis: build mode

| Aspect | Value |
|---|---|
| JSON key | `modes` |
| CLI flag | `--builds` |
| Default | `["release"]` |
| Cross-product | yes |

Build mode for the C++ compilation. Typical values are `release` and
`debug`; opp_repl also accepts `sanitize` for instrumented builds. The
value is passed through to opp_repl's `build_mode` argument and is
recorded on the `TestRun` as `mode`.

Build mode is orthogonal to [Test type](#axis-test-types). For
example, `test_types: ["fingerprint"], modes: ["release", "debug"]`
produces two fingerprint runs — one against a release build, one
against a debug build — and is the standard way to catch
mode-dependent regressions.

---

## Axis: operating system

| Aspect | Value |
|---|---|
| JSON keys | `os`, optionally `os_version` |
| CLI flags | `--os`, optionally `--os-version` |
| Default | `[None]` — let the worker pick |
| Cross-product | yes (both styles) |

The OS dimension supports two input styles, detected by whether the
`os_version` key is present.

### Combined style

```json
{ "os": ["Ubuntu 24.04", "Fedora 41"] }
```

Each string is split on the last space into `(name, version)` by
[_parse_os()](../opp_ci/scheduler.py#L65). The values are taken as a
list — no further cross-product. Use this when the OS/version pairs
you want are known, finite, and irregular.

### Structured (cross-product) style

```json
{ "os": ["Ubuntu", "Fedora"], "os_version": ["24.04", "41"] }
```

The two lists are cross-producted, producing four jobs:
Ubuntu 24.04, Ubuntu 41, Fedora 24.04, Fedora 41. Use this when every
version is valid for every name (rare in practice — the combined style
is usually more honest).

Both styles populate the `os` and `os_version` columns of the
resulting `TestRun`. Combined with `compiler`, they make up the
`platform_desc` shown in the UI.

### Worker dispatch

When [`isolation == "none"`](#axis-isolation), the job is dispatched
only to workers whose `os:<name>-<version>` capability tag matches —
e.g. `os:ubuntu-24.04`. Workers can register either by hand
(`--tags os:ubuntu-24.04`) or via `--auto-tags`, which detects the
host OS automatically.

Under `isolation == "docker"`, the OS instead selects the container
image: `opp_ci image build` produces images tagged
`opp-ci-runner:host-ubuntu-24.04-gcc-14` etc., and the executor picks
the image by exact match on `(os, os_version, compiler,
compiler_version)`.

---

## Axis: compiler

| Aspect | Value |
|---|---|
| JSON keys | `compiler`, optionally `compiler_version` |
| CLI flags | `--compiler`, optionally `--compiler-version` |
| Default | `[None]` — opp_env / host default |
| Cross-product | yes (both styles) |

Same two styles as the [OS axis](#axis-operating-system), with the
combined string split on the last hyphen by
[_parse_compiler()](../opp_ci/scheduler.py#L75):

```json
{ "compiler": ["gcc-14", "clang-18"] }
```

or:

```json
{ "compiler": ["gcc", "clang"], "compiler_version": ["14", "18"] }
```

### Validation under toolchain=nix

When `toolchain == "nix"`, the `(compiler, compiler_version)` pair
must be one that opp_env can actually provide. As of writing, opp_env
exposes only:

| Compiler | Version |
|---|---|
| `gcc` | `7` |
| `clang` | unspecified (`None`) — opp_env's `llvmPackages.stdenv` |

This allow-list lives in
[_NIX_SUPPORTED_COMPILERS](../opp_ci/scheduler.py#L132). Naming any
other pair under `toolchain=nix` causes expansion to raise
`ValueError`. The intent is to keep the matrix honest: silently
falling back to opp_env's default compiler would make the recorded
`compiler` column lie about what was actually tested.

If you need a compiler opp_env does not expose, switch the relevant
sub-matrix to `toolchain=none` (host or docker).

### Worker dispatch

Under `isolation=none`, the job requires `compiler:<name>-<version>`
on the worker. Under `isolation=docker`, the compiler is part of the
image-tag key — `opp_ci image build` must already have produced a
matching image.

---

## Axis: isolation

| Aspect | Value |
|---|---|
| JSON key | `isolation` |
| CLI flag | `--isolation` |
| Default | `["none"]` |
| Allowed | `none`, `docker` |
| Cross-product | yes |

How the job's filesystem and OS are isolated from the host:

- `none` — the executor spawns a direct subprocess on the worker. Uses
  the host's installed compilers, libraries, and opp_repl.
- `docker` — the executor runs inside a Docker image selected by the
  `(os, os_version, compiler, compiler_version)` coordinates. Image
  building is driven by `opp_ci image build` /
  `image build-matrix`.

A bare string is auto-promoted to a single-element list
([scheduler.py:113](../opp_ci/scheduler.py#L113)), so
`"isolation": "docker"` and `"isolation": ["docker"]` behave
identically.

Isolation is orthogonal to [toolchain](#axis-toolchain). The four
combinations are summarised under
[Execution environment](concepts.md#execution-environment); the same
table appears in
[getting_started.md](getting_started.md#selecting-an-execution-environment).

### Worker dispatch

Each isolation value implies different worker
[capability tags](concepts.md#capability-tag):

| isolation | Required worker tags |
|---|---|
| `none` | `os:<name>-<ver>`, `compiler:<name>-<ver>` (if specified) |
| `docker` | `docker` |

A matrix that cross-products `isolation: ["none", "docker"]` therefore
needs workers covering *both* tag sets, otherwise half the queue
stalls.

---

## Axis: toolchain

| Aspect | Value |
|---|---|
| JSON key | `toolchain` |
| CLI flag | `--toolchain` |
| Default | `["none"]` |
| Allowed | `none`, `nix` |
| Cross-product | yes |

Where the C++ toolchain comes from:

- `none` — use whatever compiler is installed on the host (or inside
  the container, when isolation is `docker`).
- `nix` — `opp_env install <project-version>` first, then
  `opp_env run <project-version> -c <cmd>`. Gives a fully reproducible
  build environment.

Like isolation, a bare string is promoted to a list. The toolchain
participates in the cross-product, so `toolchain: ["none", "nix"]`
doubles the job count.

### Interaction with compiler

Under `toolchain=nix`, the `(compiler, compiler_version)` pair is
validated against opp_env's allow-list — see
[Validation under toolchain=nix](#validation-under-toolchainnix). Under
`toolchain=none`, any compiler is accepted; the executor leaves
selection to the worker / container.

### Interaction with refs

Under `toolchain=nix`, a non-trivial `git_ref` triggers the `-git`
variant switch described under [Axis: git refs](#interaction-with-toolchain).
Under `toolchain=none`, the ref is checked out conventionally.

---

## Axis: dependency versions

| Aspect | Value |
|---|---|
| JSON key | `deps` |
| CLI flag | `--deps` |
| Default | `[None]` — use the version's recorded `resolved_dependencies` |
| Cross-product | yes (within the axis) |

Pins dependency versions per job. The config maps each dependency
name to a list of versions to test against:

```json
{ "deps": { "omnetpp": ["6.1", "6.0"], "inet": ["4.5"] } }
```

[_resolve_deps_axis()](../opp_ci/scheduler.py#L172) cross-products the
*per-dep* version lists into a list of dicts, one dict per
combination:

```python
[ {"inet": "4.5", "omnetpp": "6.1"},
  {"inet": "4.5", "omnetpp": "6.0"} ]
```

Each dict becomes the `resolved_deps` field of one expanded job.
Inside the executor, it is passed to `opp_env` as concrete
package-version pins, overriding whatever the
[Version](concepts.md#version) row's `resolved_dependencies` map
would have supplied.

The CLI accepts a compact `name=ver1,ver2;name=ver1` syntax:

```bash
opp_ci create-matrix --deps "omnetpp=6.1,6.0;inet=4.5" …
```

Unlike most other axes, `deps` is a *single* axis whose internal
cardinality is itself a cross-product over dependency names. Its
contribution to the total job count is the product of the per-dep list
lengths.

When the axis is absent, expansion does *not* compute dependency pins
— it emits `resolved_deps=None` and lets the executor fall back to the
Version's stored map (or to live resolution via
[dependency.resolve()](../opp_ci/dependency.py)).

---

## Axis: features

| Aspect | Value |
|---|---|
| JSON key | `features` |
| CLI flag | *(none yet)* |
| Default | `[]` |
| Cross-product | reserved |

Listed in [concepts.md](concepts.md#test-matrix-concepts) as a
standard axis, but currently treated as a passthrough — the scheduler
does not cross-product it. INET-style feature flags are exercised via
the `feature` [test type](#axis-test-types) instead, which opp_repl
expands internally. Reserved for future per-feature matrix
control.

---

## Implicit dimensions on every TestRun

A few fields appear on the resulting `TestRun` rows even though no
axis names them, because they are derived from the others:

| Field | Source |
|---|---|
| `platform_desc` | `"<os> <os_version> / <compiler>-<compiler_version>"` — built by [_build_platform_desc()](../opp_ci/scheduler.py#L162). |
| `commit_sha` | Filled in at worker time once `git_ref` resolves to a concrete SHA. |
| `resolved_deps` | Either the `deps` axis pin or, if absent, the Version's stored `resolved_dependencies`. |
| `trigger` | Set by the caller — `manual`, `remote`, `webhook`, or `schedule`. Not part of the matrix. |

These show up in the [rollup](concepts.md#rollup) as either *primary*
or *extra* dimensions; see
[Primary / extra dimensions](concepts.md#primary--extra-dimensions).

---

## Sizing the cross-product

The expanded job count is the product of every present axis. Quick
mental model:

```
jobs = |versions|
     × |refs|              (or len(ref_range))
     × |test_types|
     × |modes|
     × |os|     (× |os_version|     in structured style)
     × |compiler| (× |compiler_version| in structured style)
     × |isolation|
     × |toolchain|
     × ∏ |deps[name]|
```

It is easy to get this very large by accident. Defining
`versions: 4`, `refs: 10` (via `ref_range`), `test_types: 3`,
`modes: 2`, `os: 2`, `compiler: 2`, `isolation: 2`, `toolchain: 2`
produces 3 840 jobs from a config that *looks* small. Cross-products
are pure multiplication — there is no implicit filter to discard
"obviously redundant" combinations.

Strategies for keeping the matrix tractable:

1. **Split into several matrices.** Use one for fast PR feedback
   (smoke + fingerprint on the reference platform) and a separate
   nightly one for the broad cross-product.
2. **Avoid cross-producting axes that don't interact.** A
   `toolchain` × `os` cross-product is usually wasteful — pick one
   axis to vary per matrix.
3. **Use `--ref-range` only on narrow ranges.** A 50-commit range
   crossed with even a small platform matrix balloons fast.
4. **Pre-flight with `opp_ci list-matrices`.** The expanded job count
   is printed; use it as a sanity check before queueing.

---

## CLI ↔ JSON mapping reference

A quick lookup from `opp_ci create-matrix` flags to the JSON keys this
guide describes. The CLI accepts comma-separated values for every list
axis and the special `name=v1,v2;name=v1` syntax for `--deps`.

| CLI flag | JSON key | Notes |
|---|---|---|
| `--project` | *(on TestMatrix row)* | Required. Constant across jobs. |
| `--project-versions` | `versions` | Defaults to `[<project>]`. |
| `--refs` | `refs` | Mutually exclusive with `--ref-range`. |
| `--ref-range` | `ref_range` | `base..head`; resolved at expansion. |
| `--tests` | `test_types` | Required. |
| `--builds` | `modes` | |
| `--os` | `os` | Combined style if `--os-version` is absent. |
| `--os-version` | `os_version` | Triggers structured cross-product. |
| `--compiler` | `compiler` | Combined style if `--compiler-version` is absent. |
| `--compiler-version` | `compiler_version` | Triggers structured cross-product. |
| `--deps` | `deps` | `name=ver1,ver2;name=ver` syntax. |
| `--isolation` | `isolation` | List; defaults to `["none"]`. |
| `--toolchain` | `toolchain` | List; defaults to `["none"]`. |
| `--opp-file` | *(on TestMatrix row)* | Project's `.opp` file path. |

See [cli_reference.md](cli_reference.md#matrices) for the rest of the
matrix command surface (`list-matrices`, `run-matrix`,
`seed-matrices`).
