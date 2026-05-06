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
- **Test execution** — delegates to `opp_repl` for smoke, fingerprint, statistical, feature, and other test types
- **Result storage** — PostgreSQL database for structured querying and historical tracking
- **GitHub integration** — webhook-driven testing on push/PR, status checks
- **Web dashboard** — browse results, start tests, compare runs
- **Remote workers** — distributed execution on self-hosted or cloud machines

## Related Projects

- [opp_env](https://github.com/omnetpp/opp_env) — reproducible OMNeT++ environment management via Nix
- [opp_repl](https://github.com/omnetpp/opp_repl) — interactive Python REPL for running and testing OMNeT++ simulations

## Documentation

- [Getting Started](doc/getting_started.md) — installation, configuration, and first run
- [Architecture](doc/architecture.md) — system components and data flow
- [CLI Reference](doc/cli_reference.md) — command-line interface usage
- [Deployment](doc/deployment.md) — production deployment guide

## Status

Early development. See [PLAN.md](PLAN.md) for the design and staged development roadmap.

## License

LGPL-3.0-or-later