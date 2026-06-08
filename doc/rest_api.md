# REST API

JSON API exposed by `opp_ci serve`, mounted at `/api/`. All endpoints
authenticate via `Authorization: Bearer <token>` headers.

## Authentication

Bearer tokens are checked against two tables in order:

1. `api_tokens` — tokens created via `opp_ci token create` or the admin
   UI.
2. `workers` — per-worker tokens auto-generated at registration.

Tokens carry one of four roles, ordered by privilege level:

| Role | Level | Capabilities |
|---|---|---|
| `readonly` | 0 | View runs, results, workers |
| `submitter` | 1 | Submit runs (everything readonly can do) |
| `worker` | 2 | Poll jobs, heartbeat, report results (everything submitter can do) |
| `admin` | 3 | Register workers, manage tokens (everything worker can do) |

Implementation: `opp_ci/auth.py`.

## Endpoints

### Test runs

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/runs` | POST | submitter | Submit a single test run to the queue. Body fields: `project`, `kind` (required unless `test_name` is given); plus the same coordinate fields that appear on a [Test](data_model.md#test). Optional `name` labels the Test on first run (409 on a duplicate name). Alternatively pass `test_name` to run an existing named Test by name (no coordinate needed; 404 if not found). Response: `{"id", "lifecycle"}` (the value is a lifecycle value, e.g. `queued`). |
| `/api/runs/matrix` | POST | submitter | (Legacy) Expand a named matrix and queue all jobs as one `TestMatrixRun`. Body: `{"matrix_name": "..."}`. Response: `{"matrix_name", "matrix_run_id", "jobs_queued", "run_ids"}`. Prefer the more flexible `/api/matrix-runs` below. |
| `/api/runs` | GET | readonly | List runs. Filters: `project`, `kind`, `status` (union — matches a `lifecycle` *or* `result_code` value, e.g. `queued` or `PASS`; bad value → 400), `lifecycle` (strict, lifecycle values only), `result_code` (strict, outcome values only), `os`, `os_version`, `distro`, `distro_version`, `flavor`, `flavor_version`, `limit`. |
| `/api/runs/{id}` | GET | readonly | Run detail including `stdout`, `stderr`, and `details` (read off the same `TestRun` row). |
| `/api/runs/{id}` | DELETE | admin | Delete a single run. 204 on success, 404 if missing. |
| `/api/runs` | DELETE | admin | Bulk-delete by filter (`project`, `kind`, `status`, `before` = `YYYY-MM-DD`). Requires `confirm=true`, and at least one filter unless `all=true`. Returns `{"deleted": <n>}`. |

### Projects & versions

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/projects` | GET | readonly | List projects (`name`, `opp_env_name`, `github`, `git_url`, `deps`). |
| `/api/projects` | POST | submitter | Create a project. Body: `{"name", "github": "owner/repo"?, "git_url"?, "opp_env_name"?, "deps": [...]}`. 409 on duplicate. |
| `/api/projects/sync-catalog` | POST | admin | Refresh the catalog from opp_env server-side. Synchronous (30+ s). Returns `{"new_projects", "new_versions"}`. |
| `/api/projects/{name}/versions` | GET | readonly | Versions registered for one project. |
| `/api/projects/{name}/versions` | POST | submitter | Register a version. Body: `{"label", "git_ref"?, "opp_env_version"?, "deps"?}`. |
| `/api/versions` | GET | readonly | All versions across every project. |

### Matrices

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/matrices` | GET | readonly | List matrices with their `config`. |
| `/api/matrices` | POST | submitter | Create a matrix from a pre-built `config` dict. Optional `opp_file`, `ref_range`. The `opp_ci --remote create-matrix` command composes this `config` client-side via `scheduler._build_matrix_config` (same code as the local command) and posts it here. |

### Admin seed

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/admin/seed/projects` | POST | admin | Seed core projects. Returns `{"inserted", "total"}`. |
| `/api/admin/seed/platforms` | POST | admin | Seed OS/Compiler rows from `platforms.yml`. Returns `{"os_inserted", "compilers_inserted"}`. |
| `/api/admin/seed/matrices` | POST | admin | Seed the default matrices. Returns `{"inserted", "total"}`. |

### Matrix runs (rollup view + anonymous launcher)

Each `TestMatrixRun` carries a stored, O(1) rollup with a three-state
verdict (`EXPECTED` / `UNEXPECTED` / `UNKNOWN`) computed eagerly as
child [`TestVerdict`](data_model.md#testverdict) cells finalize.

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/matrix-runs` | POST | submitter | Launch a matrix run. Body: either `{"matrix_name": "..."}` (existing named matrix) **or** `{"project": "...", "kinds": [...], "modes": [...], "os": [...], ...}` (anonymous matrix; same axis keys as a `TestMatrix.config`). Optional `name`, `opp_file`, `deps`, and `no_cache: true` to bypass the content-addressable cache. An inline spec with no `name` persists as an **anonymous** `TestMatrix` (name = NULL); pass `name` to make it reusable. A duplicate `name` returns 409. Response: `{"matrix_name", "matrix_run_id", "jobs_queued", "run_ids", "status"}` — `matrix_name` is `null` for an anonymous matrix. |
| `/api/matrix-runs` | GET | readonly | Recent `TestMatrixRun` rows with their rollup verdict. Filters: `project`, `verdict` (`EXPECTED` / `UNEXPECTED` / `UNKNOWN`), `since` (ISO date), `limit`. |
| `/api/matrix-runs/{id}` | GET | readonly | Rollup header plus a `cells` array of `TestVerdict` rows joined to their `TestRun` + `Test` (per-cell verdict, actual, expected, cache_hit, recorded_at, …). |

### Expectations

Append-only edit log per Test — see
[`ExpectedTestResult`](data_model.md#expectedtestresult). Editing
applies forward-only; historical verdicts pin their own
`ExpectedTestResult` row via `expectation_id`.

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/tests/{id}/expectations` | POST | submitter | Append a new expectation row. Body: `{"expected_result_code": "PASS"\|"FAIL"\|"ERROR"\|"SKIPPED"\|null, "expected_result_description": "...", "reason": "..."}`. `expected_result_code: null` records an explicit *retraction* (distinguishable from never-set, itself audited). `set_by` is taken from the bearer token's name. |
| `/api/tests/{id}/expectations` | GET | readonly | Expectation edit history for one Test, newest first. Optional `limit`. |

### Workers

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/workers/register` | POST | admin | Register a new worker, returns its token |
| `/api/workers/me` | GET | worker | Worker fetches its own registered name, tags, and concurrency at startup |
| `/api/workers/heartbeat` | POST | worker | Keepalive — updates `last_heartbeat` |
| `/api/workers/poll` | POST | worker | Poll for the next queued job. Returns the job spec with the joined Test's coordinate fields (including `kind`). |
| `/api/workers/snapshot` | POST | worker | Optional: post the `system_snapshot` JSON captured at run start. Body: `{"run_id": ..., "snapshot": {...}}`. |
| `/api/workers/result` | POST | worker | Report job completion: writes `result_code` / `stdout` / `stderr` / `details` / `duration_seconds` / `commit_sha` directly onto the same TestRun row and flips lifecycle to `finished`. |
| `/api/workers` | GET | readonly | List registered workers |

### Tokens

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/tokens` | POST | admin | Create a new API token |
| `/api/tokens` | GET | admin | List API tokens (values masked) |
| `/api/tokens/{id}` | DELETE | admin | Disable (revoke) a token. Does not hard-delete the row. 204 on success. |

### Users

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/users` | POST | admin | Create (or, with `update_password: true`, update) a local-login user. Body: `{"username", "password", "role", "update_password"?}`. Password is hashed before storage and never echoed back. |
| `/api/users` | GET | admin | List web UI users. |
| `/api/users/{username}` | PATCH | admin | Patch `enabled`, `role`, and/or `password`. |

### GitHub

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/github/webhook` | POST | *(HMAC)* | Webhook receiver. Auth via `X-Hub-Signature-256`, not Bearer. |
| `/api/github/rules` | GET | readonly | List AutoTestRule entries |
| `/api/github/rules` | POST | admin | Create an AutoTestRule |
| `/api/github/rules/{id}` | DELETE | admin | Delete a rule |
| `/api/github/rules/test-webhook` | POST | admin | Drive the webhook handler with a synthesized payload. Body: `{"project", "ref", "event_type": "push"\|"pr", "sha"?, "pr_number"?}`. Returns the handler's result dict. |

### Git notes

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/notes/{owner}/{repo}` | GET | readonly | Pending notes for a repo (consumed by `ci-notes.yml`) |
| `/api/notes/{owner}/{repo}/ack` | POST | readonly | Acknowledge synced commit SHAs (body: `{"shas": [...]}`). Currently a no-op log line. |

## Example: submit a run remotely

```bash
curl -X POST https://ci.omnetpp.org/api/runs \
  -H "Authorization: Bearer $OPP_CI_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project": "inet", "kind": "smoke", "git_ref": "master"}'
```

Or via the CLI in remote mode:

```bash
opp_ci --remote run --project inet --kind smoke --ref master
```

See [python_client.md](python_client.md) for the Python wrapper around
this API.
