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
| `/api/runs` | POST | submitter | Submit a single test run to the queue. Body fields: `project`, `kind` (required); plus the same coordinate fields that appear on a [Test](data_model.md#test). |
| `/api/runs/matrix` | POST | submitter | Expand a named matrix and queue all jobs as one `TestMatrixRun`. Body: `{"matrix_name": "..."}`. Response includes `matrix_run_id` and `run_ids`. |
| `/api/runs` | GET | readonly | List runs. Filters: `project`, `kind`, `status` (matches `lifecycle`), `os`, `os_version`, `distro`, `distro_version`, `flavor`, `flavor_version`, `limit`. |
| `/api/runs/{id}` | GET | readonly | Run detail including `stdout`, `stderr`, and `details` (read off the same `TestRun` row). |

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

### GitHub

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/github/webhook` | POST | *(HMAC)* | Webhook receiver. Auth via `X-Hub-Signature-256`, not Bearer. |
| `/api/github/rules` | GET | readonly | List AutoTestRule entries |
| `/api/github/rules` | POST | admin | Create an AutoTestRule |
| `/api/github/rules/{id}` | DELETE | admin | Delete a rule |

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
