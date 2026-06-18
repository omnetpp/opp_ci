# Remote CLI Control

Drive a running coordinator end-to-end from your laptop with the *same*
`opp_ci` CLI you use locally — just add `--remote` (or set
`OPP_CI_REMOTE=1`). Every remote-capable command calls the coordinator's
[REST API](rest_api.md) over HTTPS instead of opening the local database,
so you never have to log in to the coordinator host.

```bash
export OPP_CI_COORDINATOR_URL=https://ci.omnetpp.org   # no /api suffix
export OPP_CI_API_TOKEN=<your token>

opp_ci --remote list-projects
opp_ci --remote run --project inet --kind smoke
opp_ci --remote run-matrix --matrix inet-default
opp_ci --remote rule create --project inet --type tag --pattern '*' --matrix inet-default
opp_ci --remote worker list
```

## How it works

`--remote` is a global flag on `opp_ci`. When set, a command dispatches
to its remote handler, which calls the matching
[`OppCiClient`](python_client.md) method; the response is rendered with
the same table/detail layout as the local command. Without `--remote`
the command runs unchanged against the local database.

- **URL.** `OPP_CI_COORDINATOR_URL` is the *bare* coordinator origin
  (`https://ci.omnetpp.org`); the CLI appends `/api`. (The Python client,
  by contrast, takes the full `…/api` URL.)
- **Token.** `OPP_CI_API_TOKEN` is sent as a bearer token. Set it once
  per shell. Missing it gives a clean
  `ERROR: Set OPP_CI_API_TOKEN env var for remote operations.`
- **Always remote.** If you only ever drive a coordinator remotely, set
  `OPP_CI_REMOTE=1` in your shell profile and drop `--remote` from every
  invocation. Pass `--local` to override it for one command.
- **TLS.** Outbound verification follows `OPP_CI_TLS_CA_BUNDLE` /
  `OPP_CI_TLS_INSECURE`, same as the worker (see [ssl.md](ssl.md)).

## Auth roles

The role of your token gates what you can do — the same hierarchy the
REST API enforces:

| Commands | Required role |
|---|---|
| `list-*`, `show-*`, `worker list`, `rule list` | `readonly` |
| `run`, `run-matrix`, `create-matrix`, `add-project`, `add-version`, `sync-catalog` | `submitter` |
| `delete-run(s)`, `seed-*`, `user *`, `token *`, `worker register`, `rule create/delete/test-webhook` | `admin` |

A token with too low a role gets a tidy
`ERROR: Requires role 'admin', got 'readonly'` — no Python traceback.

## What works remotely

| Group | Commands |
|---|---|
| Runs | `run`, `run-matrix` (named matrix only), `list-runs`, `show-run`, `show-results`, `delete-run`, `delete-runs` |
| Projects | `list-projects`, `add-project`, `sync-catalog`, `list-versions`, `add-version` |
| Matrices | `create-matrix`, `list-matrices` |
| Seed | `seed-projects`, `seed-platforms` |
| Users | `user create`, `user list`, `user disable` |
| Workers | `worker register`, `worker list` |
| Tokens | `token create`, `token list`, `token revoke` |
| Rules | `rule create`, `rule list`, `rule delete`, `rule test-webhook` |
| Images | `image build-matrix` (reads the matrix remotely, builds locally) |

### Notes & limits

- **`run-matrix --remote`** only supports a *named* matrix
  (`--matrix NAME`). Inline/anonymous specs stay local.
- **`delete-runs --remote`** prompts client-side, then sends a filtered
  `DELETE …?confirm=true`. At least one filter is required; the server
  refuses an unfiltered wipe.
- **`create-matrix --remote`** composes the matrix config from your flags
  with the same code the local command uses (`_build_matrix_config`), so
  the two paths can't drift.
- **`image build-matrix --remote`** reads the matrix definition from the
  coordinator but builds the images on *this* host (podman is local). No
  multi-GB build context is shipped over HTTP.
- **`worker register --remote --auto-tags`** detects capabilities on the
  host running the CLI — usually *not* the worker host — so `--auto-tags`
  is only meaningful when the laptop is itself the worker being
  registered.
- **`sync-catalog --remote`** runs server-side and can take 30+ seconds;
  the client uses an extended timeout.

## Host-local commands

These operate on *this* host or process and refuse `--remote` with a
non-zero exit and a one-line notice (`ERROR: <cmd> is local-only;
ignoring --remote`):

`init-db`, `reset-db`, `coordinator start`, `tls-selfsign`, `worker start`,
`worker detect-tags`, `image build`, `internal run-direct`.

`resolve-deps` is a pure local computation over opp_env metadata; with
`--remote` it prints a notice and otherwise does nothing.

## Troubleshooting

- **`ERROR: Set OPP_CI_API_TOKEN …`** — export the token.
- **`ERROR: Requires role '…'`** — your token's role is too low; use an
  admin token for writes.
- **TLS errors against a self-signed coordinator** — point
  `OPP_CI_TLS_CA_BUNDLE` at the coordinator's cert, or set
  `OPP_CI_TLS_INSECURE=1` for dev.
- **Per-request HTTP logging** — `--verbose` keeps `urllib3` at INFO so
  your bearer token doesn't leak into scrollback. Set
  `OPP_CI_HTTP_DEBUG=1` to opt into full request logging.
