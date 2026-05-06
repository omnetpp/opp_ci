# CLI Reference

## Global Options

```
opp_ci [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
|---|---|
| `-v`, `--verbose` | Enable debug-level logging |

## Commands

### `opp_ci init-db`

Create database tables explicitly. Not required — tables are auto-created on first `run`.

### `opp_ci run`

Run a test for a project and store the result.

```
opp_ci run --project PROJECT --test TEST_TYPE [--skip-install]
```

| Option | Required | Description |
|---|---|---|
| `--project` | yes | Project name (e.g. `inet`, `inet-4.5`, `omnetpp`) |
| `--test` | yes | Test type to run |
| `--skip-install` | no | Skip the `opp_env install` step (opp_env mode only) |

Supported test types:
- `smoke`
- `fingerprint`
- `statistical`
- `feature`
- `speed`
- `sanitizer`
- `chart`

### `opp_ci show-results`

Display stored test results.

```
opp_ci show-results [--project PROJECT] [--test TEST_TYPE] [--status STATUS] [--limit N]
```

| Option | Default | Description |
|---|---|---|
| `--project` | all | Filter by project name |
| `--test` | all | Filter by test type |
| `--status` | all | Filter by status: `passed`, `failed`, `error` |
| `--limit` | 20 | Maximum number of rows to display |
