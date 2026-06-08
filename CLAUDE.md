# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Plans

Plans live under [`plan/`](plan/) (`plan/pending/` → `plan/done/`); the
folder workflow is in the global `~/.claude/CLAUDE.md`. Match the style
of the existing plans: code-anchored (link to `file:line`), with a
background section, design-decision table, a commit-by-commit migration
sequence, verification steps, and risks.

## Database schema

The schema is defined by the SQLAlchemy models in
[`opp_ci/db/models.py`](opp_ci/db/models.py) and a fresh database is
initialised directly via `Base.metadata.create_all`. There are no
Alembic migration scripts — change the models and recreate.

## Tests

Test modules under [`tests/`](tests/) are written with `unittest` and
run **per module** (each sets up its own throwaway SQLite DB at import
time), e.g.:

```
python -m unittest tests.test_run_by_name
```

Running the whole `tests/` directory in one process is not supported —
the modules share a process-global engine bound to one DB.
