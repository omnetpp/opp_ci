# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Plans

This project keeps design/implementation plans as Markdown files under
[`plan/`](plan/), split into two folders by status:

- **`plan/pending/`** — plans not yet implemented (or in progress).
- **`plan/done/`** — plans that have been implemented to completion.

Rules:

- When you create a **new plan**, save it in `plan/pending/`.
- When a plan has been **executed to completion**, move it to
  `plan/done/` (e.g. `git mv plan/pending/<name>.md plan/done/<name>.md`).

Match the style of the existing plans: code-anchored (link to
`file:line`), with a background section, design-decision table, a
commit-by-commit migration sequence, verification steps, and risks.

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
