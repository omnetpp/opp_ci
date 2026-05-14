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
| `/api/runs` | POST | submitter | Submit a single test run to the queue |
| `/api/runs/matrix` | POST | submitter | Expand a named matrix and queue all jobs |
| `/api/runs` | GET | readonly | List runs (filterable by project, test_type, status) |
| `/api/runs/{id}` | GET | readonly | Run detail including results |

### Workers

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/workers/register` | POST | admin | Register a new worker, returns its token |
| `/api/workers/heartbeat` | POST | worker | Keepalive — updates `last_heartbeat` |
| `/api/workers/poll` | POST | worker | Poll for the next queued job |
| `/api/workers/result` | POST | worker | Report job completion with results |
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
| `/api/github/rules` | GET / POST | admin | List or create AutoTestRule |
| `/api/github/rules/{id}` | DELETE | admin | Delete a rule |

### Git notes

| Endpoint | Method | Role | Purpose |
|---|---|---|---|
| `/api/notes/{owner}/{repo}` | GET | readonly | Pending notes for a repo (consumed by `ci-notes.yml`) |
| `/api/notes/{owner}/{repo}/ack` | POST | readonly | Acknowledge synced notes |

## Example: submit a run remotely

```bash
curl -X POST https://ci.omnetpp.org/api/runs \
  -H "Authorization: Bearer $OPP_CI_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project": "inet", "test_type": "smoke", "git_ref": "master"}'
```

Or via the CLI in remote mode:

```bash
opp_ci --remote run --project inet --test smoke --ref master
```

See [python_client.md](python_client.md) for the Python wrapper around
this API.
