# Admin Worker Management

Give admins full lifecycle control over registered workers — **delete**,
**update concurrency**, **update tags**, and **enable/disable** — across all
three existing surfaces: CLI (`opp_ci worker …`), REST API (`/api/workers/…`),
and the web admin UI (`/admin`).

Today admins can only **register** and **list** workers. There is no way to
change a worker's concurrency/tags after registration, no explicit on/off
switch, and no way to remove a stale worker except by hand-editing the DB.

## Background — what exists today

| Surface | Register | List | Update | Enable/Disable | Delete |
|---------|:--------:|:----:|:------:|:--------------:|:------:|
| CLI     | ✅ `worker register` | ✅ `worker list` | ❌ | ❌ | ❌ |
| REST    | ✅ `POST /workers/register` | ✅ `GET /workers` | ❌ | ❌ | ❌ |
| Web UI  | ✅ form on `/admin` | ✅ table on `/admin` | ❌ | ❌ | ❌ |

Key code anchors:

- **Model** — [Worker](opp_ci/db/models.py#L97-L116): `id, name, token, tags,
  concurrency, status, last_heartbeat, registered_at, current_job_count`.
  `is_available` = `status == "online" and current_job_count < concurrency`.
- **CLI** — worker group [cli.py:2415](opp_ci/cli.py#L2415); `register`
  [2549](opp_ci/cli.py#L2549), `list` [2593](opp_ci/cli.py#L2593). Remote
  handlers `_worker_register_remote`/`_worker_list_remote`
  [cli.py:459](opp_ci/cli.py#L459). The `@remoteable`
  decorator [cli.py:49](opp_ci/cli.py#L49) dispatches to a remote handler when
  `--remote` is set. Mirror token revoke [cli.py:2670](opp_ci/cli.py#L2670).
- **REST** — `register_worker` [api.py:376](opp_ci/web/api.py#L376),
  `worker_poll` [api.py:453](opp_ci/web/api.py#L453) (gates on `is_available`),
  `worker_heartbeat` [api.py:425](opp_ci/web/api.py#L425). Mirror
  `DELETE /tokens/{id}` [api.py:1650](opp_ci/web/api.py#L1650) and
  `PATCH /users/{username}` [api.py:1763](opp_ci/web/api.py#L1763).
- **Web** — register route [app.py:2589](opp_ci/web/app.py#L2589); token
  revoke route [app.py:2640](opp_ci/web/app.py#L2640); user role/disable routes
  [app.py:2706-2768](opp_ci/web/app.py#L2706). Template: workers table +
  register form [admin.html:59-106](opp_ci/web/templates/admin.html#L59-L106);
  tokens table with per-row action form is the pattern to copy
  [admin.html:108-159](opp_ci/web/templates/admin.html#L108-L159).
- **Client** — `list_workers`/`register_worker`
  [client.py:216-227](opp_ci/client.py#L216); `revoke_token` uses `_delete`,
  `update_user` uses `_patch` [client.py:239-259](opp_ci/client.py#L239).
- **Auth** — admin gate is `Depends(require_role("admin"))` (REST) /
  `Depends(require_user("admin"))` (web); CSRF via `Depends(require_csrf)` on
  web POSTs.

## Key design decisions

### 1. Add an `enabled` Boolean column — do NOT overload `status`

`status` (`online`/`busy`/`offline`) is **automatically managed** by the
heartbeat ([api.py:446](opp_ci/web/api.py#L446)) and poll
([api.py:492](opp_ci/web/api.py#L492)) endpoints and by the stale-worker
sweeper. If "disable" just set `status="offline"`, the next heartbeat would flip
it straight back to online. We need an independent, admin-controlled flag —
exactly like `ApiToken.enabled` and `User.enabled` already do.

Add to `Worker` ([models.py:97](opp_ci/db/models.py#L97)):

```python
enabled = Column(Boolean, default=True, nullable=False)
```

and tighten availability ([models.py:115](opp_ci/db/models.py#L115)):

```python
@property
def is_available(self):
    return (
        self.enabled
        and self.status == "online"
        and self.current_job_count < self.concurrency
    )
```

A disabled worker can still heartbeat (so we see it's alive) but `worker_poll`
will hand it no jobs. Running jobs are allowed to finish naturally — disable is
a **drain**, not a kill.

### 2. Schema migration

Alembic is scaffolded ([alembic.ini](alembic.ini),
[db/migrations/](opp_ci/db/migrations/)) but `versions/` is empty — fresh DBs
are built with `Base.metadata.create_all`. Adding a column to an existing DB
won't happen via `create_all`. **Decision needed** (see Open questions):
generate the first real Alembic migration, or add a tiny idempotent
`ALTER TABLE workers ADD COLUMN enabled …` runtime guard. Recommend Alembic
since it's already wired up; this becomes revision 0001.

### 3. Delete semantics — hard delete, guarded

Unlike tokens/users (which soft-disable), a removed worker should actually
leave the roster. But `TestRun.worker_id` FKs to `Worker.id`, and a worker may
have **running** jobs.

- **Refuse** to delete a worker with `current_job_count > 0` (running jobs),
  returning a 409 with guidance to disable + drain first — unless `--force`.
- On `--force` (or after drain): requeue any still-`running` runs owned by the
  worker (`lifecycle = queued`, clear `worker_id`, `started_at`) — reuse the
  existing orphan-reclaim logic in [persistence.py](opp_ci/persistence.py) —
  then delete the row.
- Historical/finished `TestRun.worker_id` references: set the column nullable
  on delete (SET NULL) or leave the id dangling but harmless. Prefer keeping
  finished rows intact; only running ones are reclaimed.

So `disable` (drain, reversible) and `delete` (remove, guarded) are distinct
operations — both exposed.

### 4. Update propagation caveat (document, optionally fix)

A running `WorkerAgent` reads its tags/concurrency once at startup via
`GET /workers/me` ([api.py:403](opp_ci/web/api.py#L403)). After an admin edits
concurrency/tags, the **coordinator** immediately respects the new values for
scheduling (poll checks the DB live), but the **agent process** keeps its old
self-view until restart. That's acceptable for v1 — scheduling is
coordinator-side. Optional enhancement: return updated config in the heartbeat
response so the agent can adopt changes live. Out of scope unless requested.

## Implementation

A shared helper centralizes the mutation + validation so CLI-local, REST, and
web all behave identically.

### Step 0 — Model + migration
- [models.py](opp_ci/db/models.py): add `enabled` column; update `is_available`.
- Add Alembic revision `0001_add_worker_enabled` (or runtime ALTER guard).

### Step 1 — persistence helpers
In [persistence.py](opp_ci/persistence.py), add session-taking helpers so logic
lives in one place:
- `update_worker(session, worker_id, *, concurrency=None, tags=None, enabled=None) -> Worker`
  — validates (concurrency ≥ 1), applies only provided fields, commits.
- `delete_worker(session, worker_id, *, force=False) -> None`
  — 409-equivalent error if `current_job_count > 0 and not force`; else reclaim
  running runs + delete.

### Step 2 — REST API ([web/api.py](opp_ci/web/api.py))
Add request model near the worker endpoints:

```python
class WorkerUpdateRequest(BaseModel):
    concurrency: int | None = None
    tags: list[str] | None = None
    enabled: bool | None = None
```

Endpoints (all `Depends(require_role("admin"))`), mirroring tokens/users:

- `PATCH /workers/{worker_id}` → `update_worker(...)`, returns worker dict.
- `DELETE /workers/{worker_id}` (query `force: bool = False`) → `delete_worker`,
  204; 409 if it has running jobs and not forced.
- Enable/disable are just `PATCH {enabled: true|false}` (no separate route).
- Extend the `GET /workers` list dict to include `enabled` so all surfaces can
  show/act on it.

### Step 3 — Python client ([client.py](opp_ci/client.py))
Add, mirroring `update_user`/`revoke_token`:
- `update_worker(self, worker_id, *, concurrency=None, tags=None, enabled=None)`
  → `self._patch(f"/workers/{worker_id}", payload_of_non_None)`.
- `delete_worker(self, worker_id, force=False)`
  → `self._delete(f"/workers/{worker_id}" + ("?force=true" if force else ""))`.

### Step 4 — CLI ([cli.py](opp_ci/cli.py))
New commands in the existing `worker` group, each `@remoteable(...)` with a
local DB path and a `_*_remote` handler (copy the token-revoke shape at
[2670](opp_ci/cli.py#L2670)):

- `opp_ci worker update <id> [--concurrency N] [--tags a,b,c] [--add-tags …] [--remove-tags …]`
  — at least one option required; `--tags` replaces, `--add/--remove-tags`
  edit the set. Helper `_worker_update_remote`.
- `opp_ci worker enable <id>` / `opp_ci worker disable <id>`
  — thin wrappers over update(enabled=…). Disable prints a "draining; running
  jobs finish" note.
- `opp_ci worker delete <id> [--force]`
  — refuses on running jobs without `--force`; `_worker_delete_remote`.
- Update `worker list` output ([2607](opp_ci/cli.py#L2607)) to add an
  `Enabled` column.

### Step 5 — Web admin UI
- **Routes** ([app.py](opp_ci/web/app.py)), all `Depends(require_user("admin"))`
  + `Depends(require_csrf)`, redirecting back to `/admin` with a flash message,
  mirroring token-revoke / user-disable:
  - `POST /admin/workers/{worker_id}/update` (Form: `concurrency`, `tags`).
  - `POST /admin/workers/{worker_id}/toggle` (Form: `enabled`) — enable/disable.
  - `POST /admin/workers/{worker_id}/delete` (Form: `force`).
- **Template** ([admin.html:59-106](opp_ci/web/templates/admin.html#L59-L106)):
  - Add `Enabled` column + an `Actions` column to the workers table, copying the
    tokens table's per-row inline `<form>` pattern
    ([admin.html:124-132](opp_ci/web/templates/admin.html#L124-L132)).
  - Per row: an Enable/Disable toggle button, a small inline edit
    (concurrency number input + tags text input → Save), and a Delete button
    (danger style; confirm dialog). Dim disabled rows (`opacity:0.5`) like
    revoked tokens.
  - Pass `enabled` through whatever populates `workers` for `/admin`.

### Step 6 — Tests
- API: PATCH changes concurrency/tags/enabled; non-admin → 403; bad concurrency
  → 400; DELETE with running job → 409, with `--force` → reclaims + 204.
- Scheduling: a disabled worker gets `{"job": None}` from `worker_poll`;
  re-enable restores assignment.
- CLI: `update`/`enable`/`disable`/`delete` local + `--remote` paths.

## Out of scope (note, don't build unless asked)
- Bulk / multi-select actions.
- Live config push to running agents (the heartbeat-config enhancement in §4).
- Audit log of admin worker actions.
- Worker search/filtering in the UI.

## Open questions
1. **Migration mechanism** — first real Alembic revision (recommended) vs.
   runtime `ALTER TABLE` guard?
2. **Delete vs. disable** — is hard-delete wanted at all, or is "disable +
   hide" enough? (Plan includes both; delete is guarded.)
3. **Tag editing UX in the web row** — full replace via one text field
   (simplest, matches register form) vs. add/remove chips? Plan assumes
   replace-via-text-field for v1.
