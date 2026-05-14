# Concepts

`opp_ci` is the orchestration layer of a three-tool stack that tests
OMNeT++ simulation projects across version, dependency, platform, and
test-type matrices.

## The three tools

| Tool | Role |
|---|---|
| **opp_env** | Installs and manages versions of supported simulation projects in isolated Nix environments. Provides the project catalog, version list, dependency graph (`required_projects`), smoke test commands, and build commands. Invoked as `opp_env install <pkg-version>` and `opp_env run <pkg-version> -c <cmd>`. |
| **opp_repl** | Runs the tests (smoke, fingerprint, statistical, feature, chart, …) inside the environment set up by `opp_env`. Always prints human-readable text to stdout; structured per-test results go to a JSON file passed via `--result-file` or to the last line of stdout when `--output-format json` is used. |
| **opp_ci** | The orchestrator: expands test matrices, schedules jobs, invokes `opp_env` and `opp_repl`, stores results in the database, integrates with GitHub. No test logic of its own. |

The boundary is strict: opp_ci never duplicates test logic, and opp_repl
never owns the environment. Anything reproducible about a CI run is
either in opp_env's recipe or in the matrix config.

## Supported projects

`opp_ci` can test any project in the opp_env catalog. Projects are
grouped into two tiers based on how heavily they are tested.

### Tier 1 — active development, full test suite

| Project | Example versions | Dependencies |
|---|---|---|
| omnetpp | 6.1, 6.0, 5.7, git | — |
| inet | 4.5, 4.4, 4.3, git | omnetpp |
| simu5g | 1.3, 1.2, git | inet, omnetpp |
| veins | 5.3, 5.2, 5.1, git | omnetpp |

### Tier 2 — smoke + build verification

The opp_env catalog ships ~60 additional projects (simulte, plexe, flora,
artery_allinone, core4inet, nesting, …). These are auto-imported as
Tier 2 with a default `build + smoke` matrix on the reference platform.

A Tier 2 project is promoted to Tier 1 by attaching a richer
`TestMatrix` (more platforms, more test types) and optional
`AutoTestRule` entries for branch/PR triggers. No code change is needed
— see [Test Matrices](#test-matrices) and [GitHub Integration](github_integration.md).

The reference platform used for Tier 2 is configurable via the
`OPP_CI_REFERENCE_PLATFORM` env var (default: `Ubuntu 24.04/gcc-13`).

## How opp_env drives the matrix

When configuring a test run, opp_ci queries the opp_env catalog (via
the `opp_env_adapter` module) to:

1. **Resolve dependencies** — testing `simu5g-1.3.0` automatically
   includes compatible `inet` and `omnetpp` versions.
2. **Validate version combos** — incompatible combinations are rejected
   based on opp_env's `required_projects` constraints.
3. **Generate smoke tests** — the project's built-in
   `smoke_test_commands` are used as the baseline test.
4. **Discover available versions** — `opp_env list` populates the
   version selectors in the web UI and matrix configs.

`opp_ci sync-catalog` upserts the discovered projects and versions into
opp_ci's database and generates default matrices for new Tier 2
entries.

## Test matrices

A matrix is a named cross-product over independent axes:

| Axis | Examples |
|---|---|
| Target project | omnetpp, inet, simu5g, veins, simulte, … |
| Target version | `master`, `git`, or any released version |
| Dependency versions | auto-resolved or manually pinned |
| Build mode | release, debug |
| OS type | Ubuntu, Fedora, macOS, Windows |
| OS version | per OS |
| Compiler type | gcc, clang |
| Compiler version | per compiler |
| Isolation | none, docker |
| Toolchain | none, nix |
| Features | INET feature flags |
| Test types | build, fingerprint, statistical, chart, feature, module, unit, packet, queueing, protocol, validation, smoke, sanitizer, speed |

Not every combination is tested — the matrix config defines which axes
to cross. Matrix expansion happens in `opp_ci/scheduler.py:expand_matrix`.

## What opp_ci improves over per-project GitHub Actions

Historically each project (omnetpp, inet, simu5g, …) has run its own
hand-rolled GitHub Actions workflows. Common limitations of that setup:

- **Hardcoded version coupling** — INET workflows check out
  `omnetpp/omnetpp` at a fixed `omnetpp-6.x` branch.
- **Duplicated environment setup** — each workflow re-installs system
  packages, builds omnetpp, then builds inet.
- **No cross-version testing** — testing INET against multiple OMNeT++
  versions requires N hand-written workflows.
- **No historical tracking** — results are scoped to a single Actions
  run; trends and regression hunting are impossible.
- **PR-level feedback is thin** — typically only fingerprint and
  build-linux run on PRs.
- **No speed tests** — GitHub-hosted runners have no hardware
  performance counters.

opp_ci addresses all of these:

- **Decoupled versions** — any omnetpp × inet (× …) combination via
  matrix configs.
- **One environment, reused** — `opp_env` builds an environment once
  per (project-version, dependency-versions) tuple; multiple test types
  share it.
- **Cross-version matrices** — test INET against multiple OMNeT++
  versions in a single matrix.
- **Postgres history** — every run, every result, every dimension
  stored; trends and regressions are queries.
- **Full PR feedback** — webhook-driven full-matrix runs, status
  checks, PR comments.
- **Self-hosted workers** — speed tests with perf counters become
  possible.

## Design decisions

- **opp_env for reproducible builds** — Nix-based isolation guarantees
  every CI job gets the exact same dependencies.
- **opp_repl is the test engine** — opp_ci orchestrates; opp_repl
  builds and runs tests. No test logic is duplicated.
- **Structured results via opp_repl `--result-file`** — human-readable
  text stays in stdout; opp_ci reads JSON from the result file for
  per-test breakdowns. ANSI codes are stored raw in the DB and
  converted to colored HTML at render time.
- **Postgres for persistence** — structured querying of historical
  results, easy aggregation for dashboards. SQLite is supported for
  local development.
- **GitHub-native** — webhooks for automation, status checks for
  feedback, fine-grained PATs for least privilege (see [git_notes.md](git_notes.md)).
