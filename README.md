# opp_ci

Continuous integration and testing service for [OMNeT++](https://omnetpp.org)
simulation projects (OMNeT++ is a discrete-event network simulator). Tests any
project in the [opp_env](https://github.com/omnetpp/opp_env) catalog — OMNeT++,
INET, Simu5G, Veins, and 60+ others — across version, platform, and feature
matrices.

## The three-tool stack

opp_ci is the orchestration layer of a three-tool stack. The boundary is
strict: opp_ci never duplicates test logic, and opp_repl never owns the
environment.

| Tool | Role |
|---|---|
| **opp_env** | Installs and manages versions of simulation projects in isolated Nix environments. Provides the project catalog, version list, and dependency graph. |
| **opp_repl** | Runs the tests (smoke, fingerprint, statistical, feature, …) inside the environment set up by `opp_env`. |
| **opp_ci** | The orchestrator: expands test matrices, schedules jobs, invokes `opp_env` and `opp_repl`, stores results, integrates with GitHub. No test logic of its own. |

See [Concepts](doc/concepts.md) for the full vocabulary and how the pieces
connect.

## Why opp_ci over per-project GitHub Actions

Historically each project (omnetpp, inet, simu5g, …) ran its own hand-rolled
GitHub Actions workflows, which couple to fixed dependency versions, duplicate
environment setup, can't easily test cross-version combinations, and don't
preserve history. opp_ci decouples versions via matrix configs, reuses one
`opp_env` build across multiple tests, stores every run in Postgres for
trend/regression queries, and supports self-hosted workers (needed for speed
tests with hardware perf counters). See the [full comparison](doc/concepts.md#what-opp_ci-improves-over-per-project-github-actions).

## Key Features

- **Multi-project testing** — any project supported by `opp_env`
- **Version matrices** — test across multiple OMNeT++/INET/model versions with automatic dependency resolution
- **Platform matrices** — OS, compiler type/version, build mode (debug/release)
- **Reproducible builds** — `opp_env` provides isolated Nix environments
- **Test execution** — delegates to `opp_repl` for smoke, fingerprint, statistical, feature, and other tests
- **Result storage** — PostgreSQL database for structured querying and historical tracking
- **GitHub integration** — webhook-driven testing on push/PR, status checks
- **Web dashboard** — browse results, start tests, compare runs
- **Remote workers** — distributed execution on self-hosted or cloud machines

## Related Projects

- [opp_env](https://github.com/omnetpp/opp_env) — reproducible OMNeT++ environment management via Nix
- [opp_repl](https://github.com/omnetpp/opp_repl) — interactive Python REPL for running and testing OMNeT++ simulations

## Documentation

Start here:

- [Concepts](doc/concepts.md) — opp_env / opp_repl / opp_ci roles, vocabulary, matrices
- [Getting Started](doc/getting_started.md) — installation and first run
- [CLI Reference](doc/cli_reference.md) — command-line interface
- [Configuration](doc/configuration.md) — environment variables
- [Troubleshooting](doc/troubleshooting.md) — common first-run problems

Day-to-day:

- [Web UI](doc/web_ui.md) — dashboard, run detail, multi-dimensional results search
- [Deployment](doc/deployment.md) — local, cloud, hybrid
- [systemd service](doc/systemd.md) — run as a service on Ubuntu
- [Workers](doc/workers.md) — register and run remote workers
- [GitHub integration](doc/github_integration.md) — webhooks, statuses, rules
- [Git notes](doc/git_notes.md) — per-commit CI summaries delivered via `git fetch`
- [Python client](doc/python_client.md) — programmatic API access

Reference (deep dive):

- [Test Matrix Dimensions](doc/test_matrix_dimensions.md) — every matrix axis in detail
- [Single Test Parameters](doc/single_test_parameters.md) — every field on a single test run

For developers:

- [Architecture](doc/architecture.md) — components, schema, execution flow
- [Data Model](doc/data_model.md) — every database table and column
- [REST API](doc/rest_api.md) — endpoints and authentication

## License

LGPL-3.0-or-later