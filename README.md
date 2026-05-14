# opp_ci

Continuous integration and testing service for OMNeT++ simulation projects.
Tests any project in the [opp_env](https://github.com/omnetpp/opp_env) catalog
— OMNeT++, INET, Simu5G, Veins, and 60+ others — across version, platform,
and feature matrices.

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

User guides:

- [Getting Started](doc/getting_started.md) — installation and first run
- [Concepts](doc/concepts.md) — opp_env / opp_repl / opp_ci roles, matrices
- [CLI Reference](doc/cli_reference.md) — command-line interface
- [Web UI](doc/web_ui.md) — page map and result-search modes
- [Configuration](doc/configuration.md) — environment variables
- [Deployment](doc/deployment.md) — local, cloud, hybrid
- [Workers](doc/workers.md) — register and run remote workers
- [GitHub integration](doc/github_integration.md) — webhooks, statuses, rules
- [Git notes](doc/git_notes.md) — per-commit CI summaries delivered via `git fetch`
- [Python client](doc/python_client.md) — programmatic API access

For developers:

- [Architecture](doc/architecture.md) — components, schema, execution flow
- [REST API](doc/rest_api.md) — endpoints and authentication

## License

LGPL-3.0-or-later