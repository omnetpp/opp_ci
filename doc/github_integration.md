# GitHub Integration

opp_ci can be wired to GitHub in two directions:

- **Inbound** — webhooks trigger test runs on push / PR events.
- **Outbound** — opp_ci posts commit statuses, PR comments, and (via
  `workflow_dispatch`) git notes back to GitHub.

The webhook receiver, status updater, and API client live in
`opp_ci/github/`.

## Webhook receiver

`opp_ci/github/webhook.py` exposes the receiver mounted at
`POST /api/github/webhook`. The receiver:

1. Verifies `X-Hub-Signature-256` against
   `OPP_CI_GITHUB_WEBHOOK_SECRET` (HMAC-SHA256).
2. Dispatches `push`, `pull_request`, and `ping` events.
3. Looks up the `Project` by `github_owner` / `github_repo`.
4. Matches the event's branch / tag / PR head against `AutoTestRule`
   patterns using `fnmatch` glob.
5. For each matched rule, creates one `TestMatrixRun` carrying the
   event's GitHub linkage fields (`github_owner`, `github_repo`,
   `github_commit_sha`, `github_pr_number`, `github_status_url`),
   then expands the linked matrix. Each cell goes through
   `persistence.enqueue_job`, which computes a
   [`cache_fingerprint`](data_model.md#cache-fingerprint) and either
   reuses a prior finished `TestRun` (cache hit → a fresh
   [`TestVerdict`](data_model.md#testverdict) cell with
   `cache_hit=True`) or queues a new one.
6. For tag-push events, the matrix run is recorded with
   `trigger="tag"` and `ref=<tag-name>` so the project page's
   "Latest release run" card and `opp_ci list-matrix-runs --verdict`
   queries can scope to release events. Branch and PR triggers keep
   `trigger="webhook"` and leave `ref` empty.
7. Posts a `pending` commit status to the head SHA.

Webhook secret configuration: see [configuration.md](configuration.md).

## AutoTestRule

A rule says "for this project, on this kind of event, matching this
pattern, run this matrix". Stored in the `auto_test_rules` table:

| Column | Type | Purpose |
|---|---|---|
| `project_id` | FK Project | Which project to trigger on |
| `rule_type` | enum: `branch` / `pr` / `tag` | Event type |
| `pattern` | glob string | e.g. `master`, `topic/*`, `v6.*`, `*` |
| `matrix_id` | FK TestMatrix (nullable) | Matrix to expand. If null, smoke-only. |
| `enabled` | bool | Rule on/off |

Examples:

| Project | Type | Pattern | Matrix | Meaning |
|---|---|---|---|---|
| inet | branch | `master` | `inet-full` | Full matrix on every push to master |
| inet | pr | `*` | `inet-smoke` | Smoke on every PR |
| omnetpp | tag | `v6.*` | `omnetpp-release` | Release matrix on `v6.x` tags. The resulting `TestMatrixRun` has `trigger="tag"` and `ref="v6.X.Y"` so it surfaces on the project page's "Latest release run" card; `verdict == EXPECTED` ⇒ release-ready. |

Manage rules via:

- CLI: `opp_ci rule create / list / delete / test-webhook`
- Web: `/rules` (create form, delete per-row, detail at `/rules/{id}`)
- API: `GET/POST /api/github/rules`, `DELETE /api/github/rules/{id}`

`opp_ci rule test-webhook` simulates a webhook locally — useful for
verifying rule patterns without hitting GitHub.

## Outbound: status checks and PR comments

`opp_ci/github/client.py` wraps the GitHub REST API v3:

- `create_commit_status()` — pending / success / failure / error
- `set_status_pending()` and `set_status_from_run()` — convenience for
  the lifecycle: pending when queued → final state when the worker
  reports back.
- `create_pr_comment()` and `update_or_create_pr_comment()` — PR
  comments include a hidden HTML marker so subsequent runs update the
  same comment rather than spamming new ones.
- `get_pr()`, `get_commit()` — metadata reads.

`opp_ci/github/status.py:update_github_status()` is invoked from
`/api/workers/result` once a worker finishes a job. It posts the final
commit status and refreshes the PR comment.

GitHub fields stored on the parent `TestMatrixRun` (and exposed on
each child `TestRun` via proxy properties):

- `github_owner`, `github_repo`
- `github_commit_sha`
- `github_pr_number` (null for push events)
- `github_status_url` — link back to the run's web page, used in the
  status's `target_url`

For ad-hoc submissions (`opp_ci run` / `POST /api/runs`) the TestRun
has no parent `TestMatrixRun` and these fields read back as `None`.
Reading them from Python uses `run.github_owner`, `run.github_repo`,
etc. — the property delegates through `run.matrix_run`.

## Token model

opp_ci uses two separate GitHub tokens, each with the minimum scope:

| Token | Scope | Purpose |
|---|---|---|
| `OPP_CI_GITHUB_TOKEN` (or file) | classic / repo statuses | Post commit statuses and PR comments. |
| `OPP_CI_GITHUB_ACTIONS_TOKEN` (or file) | fine-grained, `Actions: Write` only | Trigger `ci-notes.yml` on target repos. opp_ci never gets `Contents: Write`; the workflow itself uses the built-in `GITHUB_TOKEN` to push notes. |

See [git_notes.md](git_notes.md) for the notes-delivery flow.

## Webhook setup

1. Generate a webhook secret:

   ```bash
   openssl rand -hex 32
   ```

2. Set `OPP_CI_GITHUB_WEBHOOK_SECRET` on the coordinator and restart.

3. In GitHub repo settings → Webhooks → Add webhook:
   - Payload URL: `https://ci.omnetpp.org/api/github/webhook`
   - Content type: `application/json`
   - Secret: the value from step 1
   - Events: `push`, `pull_request` (or "Send me everything")

4. Add a rule:

   ```bash
   opp_ci rule create --project inet --type branch --pattern master --matrix inet-full
   ```

5. Push a commit → check `/test-runs` for the queued job.

For local-only setup without a public coordinator, see
[deployment.md](deployment.md#local-github-integration).
