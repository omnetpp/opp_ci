# Plan: full remote control via the `opp_ci` CLI

Goal: an operator on their dev laptop can drive a running coordinator
end-to-end with the *same* `opp_ci` CLI they use locally, just by
prefixing `--remote` (or setting `OPP_CI_REMOTE=1`):

```
export OPP_CI_COORDINATOR_URL=https://ci.omnetpp.org/api
export OPP_CI_API_TOKEN=...
opp_ci --remote list-runs --project inet --limit 10
opp_ci --remote show-run 1234
opp_ci --remote run-matrix --matrix inet-default
opp_ci --remote rule create --project inet --type tag --pattern '*' --matrix inet-default
opp_ci --remote worker list
```

— and never has to log into the coordinator host. Today only
`opp_ci --remote run` honours the flag (see
[`opp_ci/cli.py:136`](../../opp_ci/cli.py#L136)); every other command
opens `SessionLocal()` against the laptop's own (empty) database and
either fails or prints nothing.

The fix is in three layers: (1) close the REST API gaps, (2) thread
`--remote` through every command via a small dispatch shim, (3) grow
`OppCiClient` to cover the new endpoints. The work is mostly
mechanical once the dispatch pattern is settled.

## Scope

### Remote-controllable (in scope)

Commands an operator wants to run from a laptop against a live
coordinator. Each is wired so `--remote` calls the REST API and the
default still runs against the local database:

| Group | Command | Today's behaviour | Endpoint needed |
|---|---|---|---|
| top-level | `run` | already remote-capable | `POST /runs` (exists) |
| top-level | `run-matrix` | local only | `POST /runs/matrix` (exists, via `submit_matrix`) |
| top-level | `list-runs` | local DB read | `GET /runs` (exists) |
| top-level | `show-run` | local DB read | `GET /runs/{id}` (exists) |
| top-level | `show-results` | local DB read (PASS/FAIL/ERROR variant of list-runs) | reuse `GET /runs` with `status=PASS,FAIL,ERROR` filter |
| top-level | `delete-run` | local DB delete | `DELETE /runs/{id}` (**new**) |
| top-level | `delete-runs` | local bulk DB delete | `DELETE /runs?…` (**new**) |
| top-level | `list-projects` | local DB read | `GET /projects` (**new**) |
| top-level | `add-project` | local DB write | `POST /projects` (**new**) |
| top-level | `sync-catalog` | local DB write driven by opp_env | `POST /projects/sync-catalog` (**new**) |
| top-level | `list-versions` | local DB read | `GET /projects/{name}/versions` (**new**) |
| top-level | `add-version` | local DB write | `POST /projects/{name}/versions` (**new**) |
| top-level | `resolve-deps` | pure function over opp_env metadata | stays local; `--remote` is a no-op (info-only message) |
| top-level | `create-matrix` | local DB write | `POST /matrices` (exists, but takes a pre-built `config` dict — we'll add a CLI-shape sibling, see below) |
| top-level | `list-matrices` | local DB read | `GET /matrices` (exists) |
| top-level | `seed-projects` / `seed-platforms` / `seed-matrices` | local DB write | `POST /admin/seed/{projects,platforms,matrices}` (**new**, admin) |
| user | `user create` / `user list` / `user disable` | local DB | `POST/GET/PATCH /users` (**new**, admin) |
| worker | `worker register` | local DB write | `POST /workers/register` (exists) |
| worker | `worker list` | local DB read | `GET /workers` (exists) |
| token | `token create` / `token list` | local DB | `POST/GET /tokens` (exists) |
| token | `token revoke` | local DB write | `DELETE /tokens/{id}` (**new**) |
| rule | `rule create` / `rule list` / `rule delete` | local DB | `POST/GET/DELETE /github/rules` (exist) |
| rule | `rule test-webhook` | local DB write (synthesizes a webhook) | `POST /github/rules/test-webhook` (**new**, admin) |

### Stays local (out of scope)

Commands that are inherently about *this* host or process and don't
make sense over the wire. With `--remote` set they print a short
"this command is host-local; ignoring `--remote`" notice and exit
non-zero, so an operator who mistyped doesn't silently no-op:

- `init-db`, `reset-db` — operate on the local DB. Reset of the *coordinator's*
  DB is a footgun that intentionally has no REST endpoint; if it's ever wanted,
  add it as a separate, more-friction "danger-zone" task (an admin token alone
  shouldn't be enough).
- `serve` — starts a coordinator process on this host.
- `tls-selfsign` — writes cert files on this host.
- `worker start` — runs a worker process on this host.
- `worker detect-tags` — probes this host's capabilities.
- `internal run-direct` — explicitly the worker's inner local run path.
- `image build` / `image build-matrix` — invokes podman on this host's
  daemon. `build-matrix` does need to talk to the coordinator to *read*
  the matrix definition; with `--remote` it should fetch via `GET /matrices`
  (read-only) and then build locally. That's a narrow special case; see
  [Implementation step 5](#5-image-buildmatrix-special-case).

### Explicitly out of scope

- New auth modes. `--remote` continues to authenticate with
  `OPP_CI_API_TOKEN` as a bearer. No mTLS, no per-user session, no
  separate CLI-token role. The existing `submitter` / `admin` /
  `readonly` hierarchy is enough — see [auth role mapping](#auth-role-mapping).
- Streaming output (`opp_ci --remote run` printing live worker logs).
  Submission stays fire-and-forget; the operator polls via
  `show-run` or watches the web UI. A future "follow" mode can be
  added separately.
- Server-Sent Events / WebSocket endpoints. Polling is fine at the
  scale of one operator on a laptop.
- Migration of the existing local-mode codepaths. Both modes remain
  supported indefinitely: the local path is also what `worker start`,
  the test suite, and CI smoke tests exercise.
- Bulk DB export / import via REST. If an operator wants the whole
  database off the coordinator, they still take a `pg_dump`.

## Design

The plan has three independent pieces that can be implemented and
landed in any order; only the CLI dispatch shim needs to land before
the per-command wire-ups.

### 1. CLI dispatch shim

Today's `run_cmd` body has the pattern:

```python
if ctx.obj.get("remote"):
    _run_remote(...)
    return
# … local SessionLocal() path …
```

Replicating that `if`/`return` block at the top of every command
function is mechanical but noisy and easy to forget. Centralize it.

Add a tiny decorator in `cli.py`:

```python
def remoteable(remote_handler):
    """
    Decorator: when `--remote` is set, dispatch to `remote_handler`
    instead of the local body. `remote_handler` receives the same
    keyword arguments the click command would, *minus* `ctx`.
    """
    def decorator(local_fn):
        @functools.wraps(local_fn)
        @click.pass_context
        def wrapper(ctx, **kwargs):
            if ctx.obj.get("remote"):
                return remote_handler(**kwargs)
            return local_fn(ctx=ctx, **kwargs) if "ctx" in local_fn.__code__.co_varnames \
                   else local_fn(**kwargs)
        return wrapper
    return decorator
```

Usage on a command:

```python
@main.command("list-runs")
@click.option("--project", default=None)
...
@remoteable(_list_runs_remote)
def list_runs(project, git_ref, kind, status, limit):
    # existing local SessionLocal() body, unchanged
    ...
```

Why a decorator and not a `dispatch(local, remote)` helper:

- Keeps click option declarations colocated with the command (you can
  read the CLI surface top-to-bottom without jumping to a registry).
- Lets each local-mode function stay a plain function — easy to test
  and easy to call from the unit tests we already have.
- Makes the "no remote handler" case impossible: if a command is
  decorated `@remoteable(...)`, both paths are wired by construction.
  Forgetting the dispatch becomes a compile-time error (the decorator
  isn't there).

The two existing pieces — `run`'s `if ctx.obj.get("remote"): _run_remote(...)`
and `_run_remote` itself — get rewritten as the first user of
`@remoteable`. No behaviour change.

### 2. REST API gaps

Endpoints to add in `opp_ci/web/api.py`. Each follows the existing
conventions (Pydantic request model, `require_role(...)` dependency,
`SessionLocal()` in a try/finally, JSON dict response).

#### `DELETE /runs/{run_id}` — admin

Single-row delete. Mirrors `cli.py:delete_run`. Returns 204 on
success, 404 if the run is missing. No cascading concerns — `TestRun`
has no children.

#### `DELETE /runs?project=&kind=&status=&before=` — admin

Bulk delete with the same filter set as `list_runs`. Returns
`{"deleted": <n>}`. Worth requiring at least one filter to land — the
unfiltered "delete everything" form must be a deliberate `?all=true`
to avoid a "deleted by typo" foot-gun. Spell that out in the
docstring and 400 on empty filters.

#### `GET /projects` and `POST /projects` — readonly / admin

Listing and creation. The list response mirrors today's `list_projects`
output (id, name, github, git_url, opp_env_name, deps_str). The create
endpoint takes the same fields as `cli.py:add_project_cmd`.

#### `POST /projects/sync-catalog` — admin

Runs `catalog.sync_from_opp_env(session)` server-side and returns
`{"added": ..., "updated": ..., "removed": ...}`. The local CLI body
already does this; the endpoint just exposes the same call.

#### `GET /projects/{name}/versions` and `POST /projects/{name}/versions` — readonly / admin

Mirrors `list_versions` / `add_version`. Project-scoped path
(`{name}`) is the natural REST shape and keeps the URL pretty.

#### `POST /admin/seed/{projects,platforms,matrices}` — admin

Three sibling endpoints that invoke the existing seed functions
(`seed_projects` / `seed_platforms` / `seed_matrices`). The "what was
seeded" output today is a single click.echo; in the REST response
return the inserted-row count so the CLI can still print something
useful.

#### `DELETE /tokens/{token_id}` — admin

Disables (`enabled = False`) — matches `token_revoke`. Returns 204.
*Does not* hard-delete the row, mirroring current behaviour.

#### `POST/GET/PATCH /users` — admin

User CRUD for the local-login bootstrap path. Three sub-endpoints:

- `POST /users` — body `{username, password, role, update_password?}`,
  same semantics as `user_create`.
- `GET /users` — list, same shape as `user_list`.
- `PATCH /users/{username}` — body `{enabled?: bool, role?: str,
  password?: str}`. Covers both `user_disable` and future
  enable/role-change without growing extra endpoints.

Passwords land hashed via `opp_ci.passwords.hash_password` before
storage, exactly like the CLI today. Plaintext arrives over TLS — the
[`ssl-support.md`](../done/ssl-support.md) plan is a hard prerequisite
for treating this as production-safe.

#### `POST /github/rules/test-webhook` — admin

Drives the webhook handler with a synthesized payload. Body:
`{project, ref, event_type, sha?, pr_number?}`. Calls the same code
path as `rule_test_webhook` in the CLI. Returns the handler's result
dict.

#### Endpoints **not** added

- No "delete project" / "delete version" endpoint: there's no
  matching CLI command today; adding REST without a CLI sibling would
  be dead surface.
- No "edit matrix in place" endpoint: matrices are immutable
  today (CLI uses `--replace` which deletes-then-creates). Add only
  if the CLI grows the same affordance.
- No "create rule with `--replace`" knob server-side: the CLI's
  `--replace` is implemented as "delete existing, then post new",
  which the new client can do without server help.

### 3. `OppCiClient` extensions

`opp_ci/client.py` already covers `submit_run`, `submit_matrix`,
`get_run`, `list_runs`, `list_workers`, `register_worker`,
`create_token`, `list_tokens`. Add methods for everything the new
endpoints expose, plus the existing endpoints not yet wrapped:

```
delete_run(run_id)
delete_runs(*, project=None, kind=None, status=None, before=None, confirm=False)
list_projects()
add_project(name, *, github=None, git_url=None, opp_env_name=None, deps=None)
sync_catalog()
list_versions(project=None)
add_version(project, label, *, git_ref=None, opp_env_version=None, deps=None)
list_matrices()
create_matrix(name, project, config, *, opp_file=None, ref_range=None)
seed_projects(); seed_platforms(); seed_matrices()
revoke_token(token_id)
create_user(username, password, role="admin", update_password=False)
list_users()
update_user(username, *, enabled=None, role=None, password=None)
list_rules()
create_rule(project, rule_type, pattern, *, matrix_name=None, enabled=True)
delete_rule(rule_id)
test_webhook(project, ref, event_type, *, sha=None, pr_number=None)
```

Each is a one-liner over `_get` / `_post` / new `_delete` /
`_patch` helpers. Add the four-verb support to the bottom of
`OppCiClient`:

```python
def _delete(self, path):
    resp = self._session.delete(f"{self.url}{path}", timeout=30)
    resp.raise_for_status()
    return None if resp.status_code == 204 else resp.json()

def _patch(self, path, payload):
    resp = self._session.patch(f"{self.url}{path}", json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()
```

The `_run_*_remote` helpers in `cli.py` are thin: build the kwargs,
call the client method, format the response with the same click
output the local body used (table headers, totals, etc.). Pulling the
**formatting** out of the local body into a small `_format_runs(...)`
/ `_format_workers(...)` helper means local and remote paths print
identical output. Worth doing once, in step 2 below.

### 4. `create-matrix` shape

The current `POST /api/matrices` endpoint takes
`{name, project, opp_file, config, ref_range}` where `config` is the
fully-expanded matrix dict (axes, refs, etc.).

The CLI `create-matrix` command takes 15+ flags that get composed into
that `config` dict server-side in `cli.py`. Two ways to expose this
over REST:

| Option | Pros | Cons |
|---|---|---|
| **A.** Client composes the `config` dict from the CLI flags and posts to the existing endpoint | No new endpoint. Server stays slim. | Composition logic lives in two places (local CLI body and the client). Schema drift risk. |
| **B.** New `POST /matrices/from-cli-args` endpoint that takes the flat flag shape and composes server-side | Single source of truth for "CLI flags → matrix config." | One more endpoint; shape of the request mirrors click options, which is a CLI concern leaking into the REST API. |

Plan picks **A** with a single refactor first: extract the
flag-to-`config` composition out of `create_matrix` in `cli.py` into
a pure function `_build_matrix_config(...)` in `opp_ci/scheduler.py`
(beside `expand_matrix`). Both the local CLI and the `OppCiClient`
import that function. No new endpoint, and the schema lives in one
place. The "schema drift" concern collapses because there's exactly
one composition function.

### 5. `image build-matrix` special case

`opp_ci image build-matrix --matrix <name>` reads a matrix from the
DB and then shells out to local podman for each unique image. With
`--remote`, the *read* must hit the coordinator (since the local
laptop DB doesn't have the matrix), but the *build* must stay local
(the coordinator host doesn't necessarily have podman, and we don't
want to ship multi-GB build context over HTTP).

Handle it explicitly in that command's `@remoteable(...)` handler:
the "remote handler" calls `client.list_matrices()`, finds the named
matrix, runs `expand_matrix(...)` locally, then proceeds with the
existing local podman path. No new endpoint.

### Auth role mapping

| Command | Required role |
|---|---|
| `list-*`, `show-*` | `readonly` |
| `run`, `run-matrix`, `create-matrix`, `add-project`, `add-version`, `sync-catalog` | `submitter` |
| `delete-run(s)`, `seed-*`, `user *`, `token *`, `rule *`, `worker register` | `admin` |

These match the existing patterns in `web/api.py`. The CLI emits a
helpful error when the loaded token's role is insufficient — the
server returns 403 with a JSON body; `OppCiClient` should surface the
detail string instead of leaking the bare `HTTPError`.

### Error surface

Today the `_run_remote` `except` block catches every exception and
prints `f"ERROR submitting …: {e}"`. That's OK for a single
fire-and-forget submission but ugly for, say, `list-runs` returning
`requests.exceptions.ReadTimeout("HTTPSConnectionPool …")`. Add a
small wrapper in `opp_ci/client.py` so every `OppCiClient` method
raises a typed `OppCiClientError` with a tidy `.detail` (the
server-side `detail:` field on 4xx, or the requests exception
message). The CLI catches that one type and prints `f"ERROR: {e.detail}"`.

### Config: `OPP_CI_REMOTE=1`

Add `REMOTE = os.environ.get("OPP_CI_REMOTE", "0") == "1"` to
`config.py`. The top-level `@click.option("--remote", ...)` keeps
working; absence of the flag defaults to `cfg.REMOTE`. Operators who
**only** drive their CLI remotely can set the env var once in
`~/.bashrc` and skip `--remote` on every invocation. Local-only
commands ignore it (per [Scope](#scope)).

## Implementation steps

Roughly bottom-up so each commit is independently testable:

### 1. Client helpers (one commit)

- Add `_delete`, `_patch` to `OppCiClient`.
- Add `OppCiClientError` with `.detail`; wrap `_get`/`_post`/etc. to
  raise it on `requests.HTTPError` and `requests.RequestException`.
- No new behaviour on the wire yet; the existing methods just get
  prettier errors.

### 2. CLI dispatch shim + output formatters (one commit)

- Add `remoteable(remote_handler)` decorator in `cli.py`.
- Rewrite `run_cmd` to use it. `_run_remote` becomes the
  `@remoteable(_run_remote)` handler.
- Pull row-formatting out of `list_runs`, `show_run`, `list_workers`,
  `list_matrices`, `list_projects`, `list_versions`, `user_list`,
  `token_list`, `rule_list` into `_format_*` helpers that take dicts
  (so the same code formats DB rows and REST dicts).
- Add `cfg.REMOTE` default; `main(ctx, verbose, remote)` uses it.

### 3. REST endpoints, group A: read-only (one commit)

The reads close most of the daily operator workflow. They land
together and are cheap to review:

- `GET /projects`, `GET /projects/{name}/versions`,
  `GET /github/rules` already exists; add the missing reads.
- New Pydantic response shapes that mirror the dicts the CLI already
  prints, so both sides serialize the same way.

### 4. REST endpoints, group B: writes (one commit per logical group)

- Project / version CRUD: `POST /projects`, `POST /projects/sync-catalog`,
  `POST /projects/{name}/versions`. Refactor
  `cli.py:add_project_cmd` / `add_version` / `sync_catalog_cmd` to
  delegate to small helpers in `opp_ci/catalog.py` shared with the
  REST handlers.
- Run management: `DELETE /runs/{id}`, `DELETE /runs` with filters.
  Reuse the filter-building code from `web/api.py:list_runs`.
- Seed endpoints: three small admin handlers.
- Token / user / rule writes: `DELETE /tokens/{id}`,
  `POST/GET/PATCH /users`, `POST /github/rules/test-webhook`.

### 5. `create-matrix` extraction (one commit)

- Move composition out of `cli.py:create_matrix` into
  `opp_ci/scheduler.py:_build_matrix_config(...)`.
- CLI body now calls `_build_matrix_config` and then either
  `_create_matrix_local(...)` or `client.create_matrix(...)` with the
  composed dict.

### 6. CLI wire-up (one commit per command group)

For each command group (top-level reads, top-level writes, `user`,
`worker`, `token`, `rule`):

- Add the matching `_xxx_remote(...)` handler.
- Decorate the command with `@remoteable(_xxx_remote)`.
- For the "stays local" commands, decorate with
  `@remoteable(_refuse_remote("init-db is local-only"))` — a
  one-line helper that emits the friendly message and exits with
  click.UsageError. Easier to extend later than scattering
  `if ctx.obj.get("remote"): …` early-returns.

### 7. Tests (one commit per group; can run in parallel with step 6)

- For each new REST endpoint: a FastAPI TestClient test that hits
  the endpoint with a `submitter` / `admin` / `readonly` token and
  asserts the response shape.
- For each new client method: a `responses`-mocked test that posts
  to the right URL with the right body.
- For the CLI dispatch shim: one test asserting that
  `@remoteable(_handler)` runs `_handler` when `--remote` is set and
  the local body otherwise — covers all commands by construction.
- End-to-end smoke test: spin up `opp_ci serve` against a tmp
  sqlite DB in a fixture, set
  `OPP_CI_COORDINATOR_URL=http://127.0.0.1:<port>/api`, and run
  `opp_ci --remote run …; opp_ci --remote show-run …` against it. One
  test per major command group.

### 8. Docs (one commit)

- New `doc/remote_cli.md`: "Drive the coordinator from your laptop"
  walkthrough — env vars, token role, one example per command group.
- Update `doc/cli_reference.md`: per-command "Remote behaviour" line
  noting whether `--remote` is supported and what role it needs.
- Update `doc/python_client.md`: short note that every CLI command's
  remote handler is one-to-one with an `OppCiClient` method, with
  example.
- README "Key Features" line: change "Remote workers" to "Remote
  workers and remote CLI control" so it's discoverable.

## Verification

Manual checklist after step 8 lands. Run against a freshly installed
coordinator on a separate VM, driving it from a laptop:

### Setup

```
# On the coordinator host (running opp_ci serve):
opp_ci token create --name laptop-admin --role admin
# Copy the returned token to the laptop.

# On the laptop:
export OPP_CI_COORDINATOR_URL=https://ci.lab.local/api
export OPP_CI_API_TOKEN=<token>
opp_ci --remote list-projects     # smoke test: should match the coordinator
```

### Reads

- `opp_ci --remote list-runs --project inet --limit 5` returns the
  same rows as running `list-runs` directly on the coordinator host.
- `opp_ci --remote show-run <id>` shows full details, stdout, stderr.
- `opp_ci --remote list-matrices`, `list-projects`, `list-versions`,
  `worker list`, `token list`, `rule list`, `user list` all return
  the coordinator's state.

### Writes

- `opp_ci --remote add-project --name testproj --github
  org/testproj` lands a row visible via `list-projects` from both
  laptop and coordinator host.
- `opp_ci --remote add-version --project testproj --label v1.0
  --ref main` likewise.
- `opp_ci --remote create-matrix --name lab-test --project testproj
  --kinds smoke --refs main` — coordinator-side `list-matrices`
  shows it.
- `opp_ci --remote rule create --project testproj --type tag
  --pattern '*' --matrix lab-test` — `rule list` on coordinator
  shows it.
- `opp_ci --remote rule delete <id>` removes it.
- `opp_ci --remote token create --name throwaway --role readonly`
  returns a token; `token list` shows it.
- `opp_ci --remote token revoke <id>` flips it to disabled (visible
  in `token list`'s `Enabled` column).
- `opp_ci --remote user create --username alice --role admin
  --password ...` creates a local-login user usable in the web UI.
- `opp_ci --remote user disable alice` flips the user's enabled
  column to no.
- `opp_ci --remote sync-catalog` updates the project catalog from
  opp_env on the coordinator host (visible in `list-projects` diff).

### Runs

- `opp_ci --remote run --project mm1k --kind smoke` queues a run; a
  worker picks it up; `opp_ci --remote show-run <id>` shows the
  outcome.
- `opp_ci --remote run-matrix --matrix lab-test` queues N runs; the
  return value lists their IDs.
- `opp_ci --remote delete-run <id>` removes a single row.
- `opp_ci --remote delete-runs --project testproj --status FAIL
  --before 2025-01-01` removes matching rows; bare `delete-runs`
  with no filters errors out with a "refuse to delete all"
  message.

### Auth

- Re-export `OPP_CI_API_TOKEN=<readonly-token>`. All reads still
  work; every write fails with a clean `ERROR: insufficient role
  (need admin)` message — no Python tracebacks, no bare HTTPError.
- Unset `OPP_CI_API_TOKEN` entirely. `opp_ci --remote list-runs`
  fails with `ERROR: Set OPP_CI_API_TOKEN env var for remote
  operations.` (the existing message; extended from just
  submission to all remote calls).

### Local-only refusal

- `opp_ci --remote init-db` exits non-zero with `ERROR: init-db is
  local-only; ignoring --remote`. Same for `reset-db`, `serve`,
  `tls-selfsign`, `worker start`, `worker detect-tags`.

### Side-channel: web UI consistency

- After each write through the CLI, the coordinator's web UI shows
  the same row immediately. No browser refresh needed beyond the
  normal page reload.

### Image build special case

- On a laptop with podman installed, `opp_ci --remote image
  build-matrix --matrix lab-test` reads the matrix definition from
  the coordinator and builds the images locally. (Validate by
  watching `podman images` on the laptop, not the coordinator.)

### Env-var convenience

- Set `OPP_CI_REMOTE=1` in the shell. Run `opp_ci list-runs`
  without `--remote` — it hits the coordinator as if the flag were
  given.

### Regression: pure-local mode

- Unset all `OPP_CI_*` env vars. On the coordinator host, run
  `opp_ci list-runs`, `opp_ci run --project mm1k --kind smoke`, etc.
  All work against the local DB as before. The
  `@remoteable` decorator only changes the early-dispatch path; the
  default-local behaviour is byte-for-byte unchanged.

## Open questions

- **Should `--remote` be implicit when `OPP_CI_COORDINATOR_URL` is
  set?** Tempting: a laptop that has the var exported probably
  always wants remote. Counterargument: the var is also read by
  `opp_ci worker start`, where the worker *isn't* "doing CLI things
  remotely" in the same sense. Keep `--remote` / `OPP_CI_REMOTE`
  explicit for now; revisit if operators report tripping over it.

- **Plaintext passwords over the REST API.** `POST /users` and
  `PATCH /users/{name}` accept a plaintext password. With TLS on
  (the [ssl-support](../done/ssl-support.md) plan landed) this is
  defensible, but it does mean the password is briefly in the
  coordinator's HTTP access log if logging-of-bodies is ever
  enabled by accident. Mitigation: explicitly redact the
  `password` field in the FastAPI request-logger middleware. Not a
  blocker, but worth a follow-up note in `doc/web-login.md`.

- **`delete-runs` confirmation.** The local CLI already prompts
  before bulk delete (unless `--yes`). The REST endpoint can't
  prompt; the client either passes `--yes` through (and trusts the
  operator) or the CLI prompts before issuing the DELETE. Plan
  picks the latter: the CLI prompts client-side, then sends the
  filtered DELETE with `?confirm=true`. The server still requires
  `confirm=true` so a script that forgets to pass it 400s rather
  than silently nukes data.

- **Bulk delete API shape.** `DELETE /runs?…filters…` is the
  RESTful choice but some HTTP clients (and our `requests` calls)
  treat DELETE with a body as suspect. Plan keeps filters in the
  query string; that limits filter expressiveness if we ever want
  e.g. "delete runs matching this list of 200 IDs", but matches
  what the CLI exposes today. Revisit if the filter set grows.

- **Schema for `worker register` over REST.** The existing endpoint
  doesn't take `auto-tags`-detection on the server side (it can't
  probe the *laptop's* environment). The CLI's `--auto-tags` flag
  has to detect on the laptop first, then send the union of
  detected + explicit tags. Worth a one-line note in the remote
  handler's docstring: "auto-tag detection happens on the host
  where the CLI is run, which for `--remote` is usually *not* the
  worker host — so `--auto-tags` is only meaningful when the
  laptop is itself the to-be-registered worker."

- **REST API versioning.** All new endpoints land at `/api/...`
  unprefixed by version. The existing endpoints aren't versioned
  either, and the CLI ships in lockstep with the server (one wheel,
  same version pin). If we ever ship a stable Python client to
  third parties, revisit `/api/v1/` then; not now.

- **`sync-catalog` runtime.** Calling `opp_env` to refresh the
  catalog can take 30+ seconds. The endpoint is currently designed
  as synchronous. Acceptable for an interactive `opp_ci --remote
  sync-catalog` invocation; if it grows we can move it to the
  worker job queue and return `{"job_id": ...}` instead. Note the
  endpoint's 60s timeout in `OppCiClient` (override the default 30s
  on the single call).

- **`internal run-direct` over `--remote`.** This command is the
  worker's inner local-execution path; routing it remotely is
  nonsensical. It's also `hidden=True` in click — operators don't
  see it. Treat the same as `init-db`: friendly refusal. No-op
  alternative would be silently passing it through, which would
  break the worker.

- **CLI verbose flag and REST.** `opp_ci --verbose --remote
  list-runs` currently flips Python logging to DEBUG. That gets the
  laptop's `requests` library to log every HTTP call, which is
  useful for debugging. Worth confirming the coordinator doesn't
  end up shipping per-request DEBUG output back; the
  `Authorization:` header in a urllib3 debug log would be a token
  leak in shell scrollback. Mitigation: tighten `urllib3`'s log
  level to INFO even when our top-level is DEBUG, unless an
  explicit `OPP_CI_HTTP_DEBUG=1` is set. Add to step 1.
