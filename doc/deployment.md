# Deployment

## Local Development

No external services needed. Uses SQLite and direct command execution.

```bash
source setenv
pip install -e .
opp_ci run --project inet --test smoke
opp_ci show-results
```

The database file (`opp_ci.db`) is created in the working directory.

## Cloud Deployment (PostgreSQL + opp_env)

### Database Setup

```bash
createdb opp_ci
export OPP_CI_DATABASE_URL="postgresql://user:pass@host/opp_ci"
```

### Enable opp_env Mode

```bash
export OPP_CI_USE_OPP_ENV=1
```

This requires Nix and `opp_env` to be installed on the machine.

### Running

```bash
opp_ci run --project inet-4.5 --test smoke
```

### Web UI (Stage 2)

```bash
opp_ci serve
```

Serves at `http://localhost:8000` by default. Connect to the same database as the CLI.

## Environment Variables

| Variable | Local default | Cloud example |
|---|---|---|
| `OPP_CI_DATABASE_URL` | `sqlite:///opp_ci.db` | `postgresql://ci:secret@db.example.com/opp_ci` |
| `OPP_CI_USE_OPP_ENV` | `0` | `1` |
