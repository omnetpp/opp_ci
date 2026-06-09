# Operational questions opp_ci can answer

This guide is about *using opp_ci as a decision tool*, not as a test
runner. The mechanics of matrices, workers, and results are covered in
[concepts.md](concepts.md), [test_matrix_dimensions.md](test_matrix_dimensions.md),
and [web_ui.md](web_ui.md). Here the framing is the questions a release
manager or maintainer actually asks:

- **Is this OMNeT++ (or INET) release ready to publish?**
- **Will the dependent projects still work against the new release?**
- **Do we support this newly released OS / compiler version?**
- **What is the current state of every opp_env project — what is
  supported, on which platforms, with which compilers?**

For each question this guide states **what opp_ci answers today**, the
**exact coordinates you test**, the **commands/queries to run**, and the
**gaps** where the answer is still manual or unavailable. The gaps are
the input to [plan/pending/operational-question-support.md](../plan/pending/operational-question-support.md).

The vocabulary used below — *Test*, *coordinate*, *TestMatrixRun*,
*Verdict*, *ExpectedTestResult*, *compatibility matrix* — is defined in
[concepts.md](concepts.md). Read that first if a term is unfamiliar.

---

## The unit of an answer: a coordinate with a verdict

Every operational answer reduces to the same primitive. A **Test** is an
immutable coordinate — the 16-field tuple plus resolved dependencies that
[concepts.md → Test](concepts.md#test) describes:

```
project · kind · mode ·
os · os_version · distro · distro_version · flavor · flavor_version · arch ·
compiler · compiler_version · isolation · toolchain · opp_file ·
resolved_deps          (e.g. {"omnetpp": "6.4.0"})
```

A **TestRun** is one observed execution of that coordinate, carrying a
`result_code` (PASS / FAIL / ERROR / SKIPPED) and the git `commit_sha` it
ran against. A **TestVerdict** grades that run against the **expectation**
in force for the coordinate, yielding EXPECTED / UNEXPECTED / UNKNOWN.

So "do we support X" is always: *find the most recent finished TestRun at
the coordinate that encodes X, check its verdict, and check it is recent
enough and against the right SHA.* Each question below is a different way
of selecting the coordinate set and rolling up the verdicts.

---

## Question 1 — Is this release ready to publish?

**Example:** OMNeT++ 6.4.0 is tagged. Ship it or not?

### What opp_ci answers today

For *the project's own test surface*, this is the designed primary
workflow (see [concepts.md → Release-tag trigger](concepts.md#release-tag-trigger)):

1. A maintainer pushes the release tag.
2. An [AutoTestRule](concepts.md#autotestrule) with `rule_type=tag` and a
   matching `pattern` fires and expands the bound matrix into one
   [TestMatrixRun](concepts.md#testmatrixrun) with `trigger="tag"`,
   `ref="omnetpp-6.4.0"`.
3. When every cell finalizes, the stored rollup `Verdict` is the answer:
   - `EXPECTED` ⇒ every coordinate met its declared expectation
     (including known-XFAIL cells) ⇒ **release-ready**.
   - `UNEXPECTED` ⇒ at least one coordinate diverged ⇒ a regression to
     investigate.
   - `UNKNOWN` ⇒ results exist but at least one coordinate has no declared
     expectation ⇒ characterise it, then re-run.

### Coordinates you test

The release matrix is the cross-product you commit to support for that
project: `{modes} × {os/compiler platforms} × {isolation/toolchain} ×
{kinds}` at the tagged version. For a release, the convention is
`isolation=podman, toolchain=nix` for reproducible, high-fidelity builds
(see [test-data-model-redesign → F6](../plan/done/test-data-model-redesign.md)).

### Commands

```bash
# Manually (without waiting for the tag webhook):
opp_ci run-matrix --matrix omnetpp-release --ref omnetpp-6.4.0 --follow

# Read the verdict:
opp_ci show-matrix-run <id>              # rollup + per-cell table
opp_ci show-matrix-run <id> --unexpected-only
opp_ci list-matrix-runs --project omnetpp --verdict UNEXPECTED
```

The project page's "Latest release run" card surfaces the same verdict in
the web UI.

### The gap

The verdict above only covers **the project's own tests**. For a
foundational project like OMNeT++ or INET, "release-ready" must also mean
*the downstream ecosystem still works against the candidate* — see
Question 2. opp_ci has no single "release readiness" rollup that spans the
project's own matrix run **and** the downstream qualification runs. Today
you read each matrix run's verdict separately and combine them by hand.

→ Plan: **Release aggregate** (a verdict spanning many TestMatrixRuns) and
**downstream qualification** (below).

---

## Question 2 — Will dependent projects work with the new release?

**Example:** OMNeT++ 6.4.0 is the candidate. Do INET, Simu5G, Veins, and
the ~60 external models still build and pass against it?

This is the **reverse-dependency** (downstream / blast-radius) question.

### What opp_ci answers today

Two partial answers exist:

**(a) Declared compatibility, per project.** The
[compatibility matrix](../opp_ci/compatibility.py) builds, for a project,
a grid of *its versions × each dependency's versions*, populated from
opp_env's declared `required_projects` and **overlaid** with empirical
PASS/FAIL from real TestRuns where the dependency version can be
determined. Viewable per project in the web UI; filterable by OS,
compiler, mode, etc.

```
# Conceptually: inet rows × omnetpp columns, cells = declared|PASS|FAIL|mixed
get_compatibility_matrix(session, "inet")
```

This answers "which (inet, omnetpp) pairs are *declared* compatible, and
of those, which have we actually *verified*." It is the right read model
for the answer — but it is **forward** (a project against the things *it*
depends on), and it only shows what has already been run.

**(b) A hand-authored downstream matrix.** You can author a matrix for
each downstream project that pins `omnetpp=6.4.0` and run it. The results
flow into the compatibility overlay above.

### Coordinates you test

For candidate `omnetpp=6.4.0`, the downstream coordinate set is:

```
for each project P that (transitively) requires omnetpp:
  for each currently-supported version V of P:
    P · {kinds} · {modes} · {platforms} · resolved_deps={omnetpp: 6.4.0, …}
```

The dependency pin is the load-bearing field — it is part of the Test
coordinate (`resolved_deps`), so a run against omnetpp 6.4.0 is a
*different* Test from the same project/version against 6.3.0, and the
compatibility overlay attributes it to the 6.4.0 column.

### The gap

There is **no first-class reverse-dependency expansion**. To qualify a
candidate against its downstream you must:

1. Manually work out who depends on omnetpp (derivable from
   `Project.dependency_names` across the catalog, but not exposed as a
   command).
2. Hand-author or hand-launch a pinned matrix per downstream project.
3. Read each result separately — there is no aggregate "downstream
   verdict for omnetpp 6.4.0."

→ Plan: **Reverse-dependency graph** + **downstream qualification
generator** + **Release aggregate verdict**.

---

## Question 3 — Do we support a new OS or compiler?

**Example:** Ubuntu 26.04 ships, or gcc-15 lands. Can we say OMNeT++ /
INET are supported there?

This is a **new-axis-value sweep**: fix one new platform coordinate and
run the things we commit to support across it.

### What opp_ci answers today

The platform axes already exist — `os`, `os_version`, `distro`,
`distro_version`, `flavor`, `flavor_version`, `arch`, `compiler`,
`compiler_version` — with a full OS ⊃ distro ⊃ flavor hierarchy
(see [test_matrix_dimensions.md](test_matrix_dimensions.md)). To test a
new platform you:

1. Stand up (or tag) a worker that advertises the platform's
   [capability tags](concepts.md#capability-tag) (`distro:ubuntu-26.04`,
   `compiler:gcc-15`, `arch:amd64`), or build a Podman image for it via
   `opp_ci image build`.
2. Author a matrix fixing the new value and crossing the project versions
   you care about, and run it.

```bash
opp_ci run-matrix \
    --project omnetpp --ref omnetpp-6.4.0 \
    --distro "Ubuntu 26.04" --compiler gcc-15 \
    --kinds build,smoke --modes release,debug --isolation podman
```

The resulting per-cell verdicts say whether that platform passed. Q2 of
the [data-model redesign](../plan/done/test-data-model-redesign.md#how-the-two-questions-get-answered)
("would it work on system X?") is exactly a lookup against these
coordinates.

### Coordinates you test

```
fix: distro="Ubuntu 26.04" (or compiler="gcc-15")
cross: {actively-supported (project, version) pairs} × {modes} × {kinds}
```

### The gap

There is no notion of a **canonical "platform qualification suite"** — the
agreed set of (project, version) coordinates the new platform must satisfy
before we declare support. You re-derive that set and re-author the matrix
by hand for every new OS/compiler. There is also no **platform support
report** that rolls the sweep into a single "Ubuntu 26.04: supported /
not / partial" verdict per project.

→ Plan: **Support model** (declares the canonical set) + **platform
qualification launcher** + **platform support report**.

---

## Question 4 — What is the current state of all opp_env projects?

**Example:** Show me every project, every version, what it's supposed to
work with, and what we've actually verified — platforms, compilers,
OMNeT++ versions — and how fresh that evidence is.

### What opp_ci answers today

**The catalog half is solid.** `opp_ci sync-catalog`
([opp_env_adapter.py](../opp_ci/opp_env_adapter.py)) mirrors the entire
opp_env catalog into opp_ci:

- every **Project** (name, GitHub owner/repo, `dependency_names`),
- every **Version** with its `resolved_dependencies` (the declared
  dep-name → compatible-versions map from opp_env's `required_projects`),
- the **OS** and **Compiler** lookup tables, seeded from opp_env's option
  metadata.

```bash
opp_ci sync-catalog            # pull fresh catalog from opp_env
opp_ci list-projects
opp_ci list-versions --project inet
opp_ci resolve-deps simu5g-1.4.4   # → omnetpp + inet pinned
```

So "what projects/versions exist and what they *declare* they work with"
is fully answerable. The compatibility matrices (Question 2) add the
empirical "what have we verified" overlay, per project.

### The gap

The **state half** — a single, ecosystem-wide view of *intended vs.
verified support with freshness* — does not exist:

- **No declared support target.** opp_ci knows declared *dependency*
  compatibility (from opp_env) but not declared *platform/compiler*
  support. The data-model redesign deliberately
  [rejected](../plan/done/test-data-model-redesign.md#future-scope)
  putting a support declaration in the opp_env descriptor and let
  "whatever matrices exist" be the implicit declaration. That is exactly
  why "what platforms are supported for inet 4.6?" has no crisp answer
  today — there is no *commitment* to compare verified results against.
- **No freshness signal.** "We verified it" is only meaningful if the
  evidence is recent and against the current head SHA. Cache fingerprints
  capture the SHA, but nothing reports "this cell's last EXPECTED verdict
  is 4 months old / against a stale commit."
- **No unified coverage dashboard.** There is no page or query that joins
  *(every project × version × declared support)* with *(latest verdict +
  age)* to render one ecosystem state table.

→ Plan: **Support model**, **freshness/staleness**, **coverage
dashboard**.

---

## Summary: what's answerable today vs. what's missing

| Question | Answerable today | Missing |
|---|---|---|
| 1. Is *this project's* release ready? | ✅ tag-triggered `TestMatrixRun.verdict == EXPECTED` | Release-level aggregate across own + downstream runs |
| 2. Will dependents work with the release? | ⚠️ per-project compatibility grid; hand-authored pinned matrices | Reverse-dep graph, downstream qualification generator, downstream verdict |
| 3. Do we support a new OS/compiler? | ⚠️ run a hand-authored sweep; per-cell verdicts | Canonical qualification suite, platform support report |
| 4. State of all opp_env projects? | ✅ catalog (projects/versions/declared deps); ⚠️ per-project empirical overlay | Declared support model, freshness, unified coverage dashboard |

The common thread in every gap is the same three missing pieces:

1. **A declared support model** — what we *commit* to support
   (platforms, compilers, dep-versions) per project-version or release
   line. Without it, "supported" and "release-ready" have no yardstick
   beyond "some matrix happened to pass."
2. **Graph-driven expansion** — reverse dependencies (downstream
   qualification) and new-axis sweeps (platform qualification) generated
   from the catalog instead of hand-authored.
3. **Aggregation + freshness** — rolling many TestMatrixRuns into a
   release/platform/coverage verdict, and knowing how recent and how
   on-SHA the underlying evidence is.

All three are specified in
[plan/pending/operational-question-support.md](../plan/pending/operational-question-support.md).

---

## How to use opp_ci for each question *today* (recipes)

Until the plan lands, these are the manual recipes.

**Release readiness (own surface):**

```bash
opp_ci run-matrix --matrix omnetpp-release --ref omnetpp-6.4.0 --follow
opp_ci show-matrix-run <id> --unexpected-only
# ship iff verdict == EXPECTED
```

**Downstream qualification (manual blast-radius):**

```bash
# 1. Find downstream (who lists omnetpp in dependency_names):
opp_ci list-projects                       # then read deps, or query the DB
# 2. For each downstream project, pin the candidate and run:
opp_ci run-matrix --project inet   --kinds build,smoke --pin omnetpp=6.4.0
opp_ci run-matrix --project simu5g --kinds build,smoke --pin omnetpp=6.4.0 --pin inet=4.6.0
opp_ci run-matrix --project veins  --kinds build,smoke --pin omnetpp=6.4.0
# 3. Read the per-project verdicts and the compatibility grid overlay.
```

**New platform qualification:**

```bash
# Ensure a worker advertises the platform (capability tags) or build the image:
opp_ci image build --distro "Ubuntu 26.04" --compiler gcc-15
# Sweep the projects you support across the new value:
opp_ci run-matrix --project omnetpp --distro "Ubuntu 26.04" --compiler gcc-15 \
    --kinds build,smoke --modes release,debug --isolation podman --toolchain nix
```

**Current state:**

```bash
opp_ci sync-catalog
opp_ci list-projects
opp_ci list-versions --project inet
# Per-project verified compatibility: open the project's Compatibility page
# in the web UI (declared grid + empirical overlay, filterable by platform).
```

---

## How to extend opp_ci to answer these better

The extension work is specified as a phased plan in
[plan/pending/operational-question-support.md](../plan/pending/operational-question-support.md).
At a glance, the new capabilities it adds:

- `opp_ci support set / show` — declare and read the support target
  (platforms × compilers × dep-versions) per project-version or release
  line.
- `opp_ci downstream <project>-<version>` — list reverse dependencies and
  generate/launch the downstream qualification matrices for a candidate.
- `opp_ci qualify-platform --distro … --compiler …` — run the canonical
  support suite across a new platform value and emit a support verdict.
- `opp_ci release-status <project>-<version>` — the aggregate verdict over
  the project's own run plus all downstream qualification runs.
- `opp_ci coverage` + a **State** web page — the ecosystem-wide
  intended-vs-verified-vs-fresh table answering "what is supported right
  now."

These build entirely on the existing schema (Test coordinate, TestRun,
TestVerdict, compatibility overlay, catalog sync) — they are read models,
generators, and one new support-declaration table, not a redesign.
