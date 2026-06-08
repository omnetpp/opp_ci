# Python Client

`opp_ci/client.py` provides `OppCiClient`, a thin wrapper around the
[REST API](rest_api.md) for programmatic submission and querying.

## Setup

```python
from opp_ci.client import OppCiClient

ci = OppCiClient(url="https://ci.omnetpp.org/api", token="<submitter-token>")
```

Note the `/api` suffix on the URL — `OppCiClient` does not prepend it.
The `--remote` CLI mode, by contrast, takes the bare coordinator URL
(`https://ci.omnetpp.org`) in `OPP_CI_COORDINATOR_URL` and appends `/api`
on your behalf.

Token roles are described in [rest_api.md](rest_api.md#authentication).
Use a `submitter` token for run submission and a `readonly` token for
read-only scripts.

## Submitting runs

```python
# Single run
run = ci.submit_run(project="inet", kind="smoke", git_ref="topic/my-feature")
print(run)  # {"id": 42, "status": "queued"}

# Fully-specified run on a podman/host-toolchain worker
ci.submit_run(
    project="inet-4.5", kind="fingerprint",
    mode="release", git_ref="master",
    os="Ubuntu", os_version="26.04", arch="amd64",
    compiler="clang", compiler_version="22",
    isolation="podman", toolchain="none",
    force=True,
)

# All jobs in a named matrix — returns the matrix_run_id and the list
# of run_ids spawned under it.
ci.submit_matrix(matrix_name="inet-full")
```

The `kind` parameter is what used to be called `test` before the
phase-1 schema cutover. It picks the opp_repl entry point via
`COMMAND_MAP` — see
[test_matrix_dimensions.md → Axis: kind](test_matrix_dimensions.md#axis-kind)
for the canonical list.

## Querying

```python
ci.get_run(42)
ci.list_runs(project="inet", kind="fingerprint", status="FAIL")
ci.list_workers()
```

`list_runs(status=…)` accepts either a `TestRunLifecycle` value
(`"queued"` / `"running"` / `"finished"` / `"cancelled"` /
`"timed_out"`) or a `TestResultCode` (`"PASS"` / `"FAIL"` / `"ERROR"`
/ `"SKIPPED"`).

## Admin operations (admin token required)

```python
ci.register_worker(name="builder-1", tags=["linux", "amd64"], concurrency=4)
ci.create_token(name="github-bot", role="submitter")

ci.add_project("mm1k", github="levy/mm1k", deps=["omnetpp"])
ci.add_version("mm1k", "v1.0", git_ref="main")
ci.sync_catalog()
ci.create_user("alice", "secret", role="admin")
ci.update_user("alice", enabled=False)
ci.revoke_token(7)
ci.delete_run(42)
ci.delete_runs(project="mm1k", status="FAIL", before="2025-01-01", confirm=True)
ci.create_rule("inet", "tag", "*", matrix_name="inet-default")
```

Every `opp_ci --remote <command>` is one-to-one with an `OppCiClient`
method, so the client covers the full coordinator surface: projects,
versions, matrices, runs, workers, tokens, users, and GitHub rules. See
[Remote CLI Control](remote_cli.md) for the command↔method mapping and
role requirements.

## Errors

Every method raises `OppCiClientError` on failure, with a tidy `.detail`
(the server's `detail:` field on a 4xx/5xx, or the transport error
message) and a `.status_code` (None for connection errors / timeouts):

```python
from opp_ci.client import OppCiClient, OppCiClientError

try:
    ci.list_users()
except OppCiClientError as e:
    print(e.detail, e.status_code)   # "Requires role 'admin', got 'readonly'" 403
```

## CLI equivalent

The CLI accepts `--remote` (or `OPP_CI_REMOTE=1`) to route through the
API instead of running locally:

```bash
export OPP_CI_COORDINATOR_URL=https://ci.omnetpp.org   # no /api suffix
export OPP_CI_API_TOKEN=<submitter-token>

opp_ci --remote run --project inet-4.5 --kind smoke,fingerprint --ref master
opp_ci --remote list-runs --project inet --status FAIL
```

Useful for one-off submissions, cron jobs, or scripting around the CI
without writing Python. Full walkthrough: [Remote CLI Control](remote_cli.md).
