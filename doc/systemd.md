# Running opp_ci as a systemd service

On Linux (Ubuntu and other systemd distros) opp_ci installs and manages
itself as a system service straight from the CLI — no repo checkout, no
venv under `/opt`. The service runs opp_ci from GitHub via
[`uvx`](https://docs.astral.sh/uv/concepts/tools/), re-fetching the
pinned ref on **every (re)start**, so a restart picks up the latest code
on that ref.

> On **macOS**, which has no systemd, run workers as launchd
> LaunchDaemons instead — see [launchd.md](launchd.md). (macOS packaging
> is worker-only; the coordinator/`serve` side stays on Linux.)
>
> On **NixOS**, units are owned declaratively, so `service install`
> *renders* a NixOS module instead of mutating the system — see
> [nixos.md](nixos.md).

There is no single "opp_ci" process: the service layer wraps the
existing `opp_ci serve` and `opp_ci worker start` subcommands. The host
role — coordinator, worker, or combined — is a matter of which units you
install.

## How it runs (uvx)

Each unit's `ExecStart` is a single `uvx` command, for example the serve
unit:

```
uvx --from "opp_ci[web,postgres,client,podman] @ git+https://github.com/omnetpp/opp_ci.git@main" \
    --with "opp_repl[all] @ git+https://github.com/omnetpp/opp_repl.git@opp_ci" \
    --refresh-package opp_ci --refresh-package opp_repl \
    opp_ci serve
```

- The `opp_ci[...]` extras are **role-determined**: serve gets
  `web,postgres,client,podman`; a worker gets `client,podman`.
- `@main` is the opp_ci GitHub ref; set it with `--ref` at install time
  (a tag bounds surprise upgrades).
- `opp_repl` is pulled from its **`opp_ci` branch** (it is not on PyPI).
- `--refresh-package opp_ci opp_repl` is what delivers "latest each
  restart": without it, `uvx` caches the first resolved commit of a
  branch and reuses it forever.

The `opp_ci serve` / `opp_ci worker start` command line carries **no**
runtime options — all runtime config comes from env files (below).

## Units

| Unit | Purpose | Multiplicity |
|---|---|---|
| `opp_ci-serve.service` | Runs `opp_ci serve` (web UI + API + scheduler) | Singleton |
| `opp_ci-worker@.service` | Templated unit; each instance runs `opp_ci worker start` for one worker name | One per worker name |
| `opp_ci.target` | Umbrella for whichever services are installed on this host | Singleton |
| `opp_ci-serve-cert.path` + `…-cert-reload.service` | Optional TLS cert-watch auto-reload (`serve service install --tls`) | Optional |

## Install

`uv`/`uvx` must be present for the **invoking** user. `service install`
copies both binaries into the service user's `~/.local/bin/`
(e.g. `/var/lib/opp_ci/.local/bin/`) so the same version is available to
the daemon; the unit references that absolute path. It does **not**
install uv for you — if uv/uvx are missing it warns and continues.

Coordinator host:

```bash
sudo uvx opp_ci serve service install --host 0.0.0.0 --port 8080
sudo uvx opp_ci serve service install --no-postgres   # use a remote DB
```

Worker host:

```bash
# 1. Register the worker on the coordinator to mint a token:
opp_ci worker register --remote https://ci.example.org --name builder-1 --auto-tags
# 2. Install + start the worker service with that token:
sudo uvx opp_ci worker service install --name builder-1 \
    --coordinator https://ci.example.org --token <token>
```

By default `install` **enables-on-boot and starts** the service
(`systemctl enable --now`). Opt out with `--no-enable` / `--no-start`.

**Worker token guard:** if you pass no `--token` and none is already in
the env file, auto-start is skipped with a message; set the token, then
`opp_ci worker service start --name <name>`.

### Install options

`serve service install`:

| Option | Effect |
|---|---|
| `--host`, `--port`, `--cert`, `--key` | → `serve.env` |
| `--user` (default `opp_ci`) | run-as user (`User=`) |
| `--ref` (default `main`) | opp_ci GitHub ref in the uvx command |
| `--no-postgres` | skip local PostgreSQL provisioning |
| `--tls` | also install the cert-watch auto-reload units |
| `--no-enable`, `--no-start` | don't enable-on-boot / start now |
| `--dry-run` | render + print all artifacts, change nothing |

`worker service install`:

| Option | Effect |
|---|---|
| `--name` (default `default`) | instance → `opp_ci-worker@<name>.service` + `workers/<name>.env` |
| `--coordinator`, `--token`, `--poll-interval`, `--heartbeat-interval`, `--niceness` | → `workers/<name>.env` |
| `--user`, `--ref`, `--no-enable`, `--no-start`, `--dry-run` | as above |

### What install provisions

- The `--user` system account (default `opp_ci`, home `/var/lib/opp_ci`).
- `/etc/opp_ci`, `/etc/opp_ci/workers`, `/var/lib/opp_ci`, the TLS dir.
- uv/uvx copied to the service user (see above).
- serve: local PostgreSQL (role + db + grant + detected port → `OPP_CI_DATABASE_URL`), unless `--no-postgres`.
- worker: rootless podman (subuid/subgid, `enable-linger`, `podman system migrate`).

## Configuration (env files)

Runtime options live in env files; the command line stays generic.

| File | Read by | Mode | Keys |
|---|---|---|---|
| `/etc/opp_ci/opp_ci.env` | both | 0640 root:opp_ci | `OPP_CI_DATABASE_URL` |
| `/etc/opp_ci/serve.env` | serve | 0640 root:opp_ci | `OPP_CI_SERVE_HOST/PORT/TLS_CERT_FILE/TLS_KEY_FILE` |
| `/etc/opp_ci/workers/<name>.env` | one worker | 0600 opp_ci:opp_ci | `OPP_CI_COORDINATOR_URL`, `OPP_CI_WORKER_TOKEN`, `OPP_CI_WORKER_POLL_INTERVAL`, `OPP_CI_WORKER_HEARTBEAT_INTERVAL`, `OPP_CI_WORKER_NICENESS`, `OPP_CI_OPP_ENV_CMD` |

`OPP_CI_OPP_ENV_CMD` is set to `uvx --from opp-env opp_env` so the
host-nix opp_env path runs from its own isolated venv.

To change a setting, edit the env file and restart:
`sudo opp_ci worker service restart --name <name>`.

## Lifecycle

```bash
sudo opp_ci serve  service {start|stop|restart|status}
sudo opp_ci worker service {start|stop|restart|status} --name <name>
```

These drive `systemctl` directly. Logs:
`journalctl -fu opp_ci-serve` / `journalctl -fu opp_ci-worker@<name>`.

## Uninstall

```bash
sudo opp_ci serve  service uninstall            # stop + disable + remove units
sudo opp_ci worker service uninstall --name X   # only that instance
```

Conservative by default: removes the unit files and `daemon-reload`s but
**preserves** `/etc/opp_ci` config + env files (tokens), `/var/lib/opp_ci`
state, the database, and the service user. `--purge` additionally removes
config + state after a confirmation prompt (never drops the database or
deletes the user without further confirmation).

A worker uninstall removes only that instance; the shared
`opp_ci-worker@.service` template and `opp_ci.target` stay while any
other worker remains.

## No-sudo / dry-run

Every `service` operation does one up-front `geteuid()==0` check. Run
unprivileged (or pass `--dry-run`) and the CLI mutates nothing — it
prints a complete, copy-pasteable manual transcript: every file (path,
owner, mode, exact contents), the uv/uvx copy, user/dir creation, and
the `systemctl` enable/start commands. `--dry-run` prints the same
transcript even as root, for review before applying.

## TLS

Drop `fullchain.pem` + `privkey.pem` into `/etc/opp_ci/tls/` (use
`opp_ci tls-selfsign` for a lab cert, or paste a Cloudflare Origin
Certificate), set the TLS paths in `serve.env`, and install with `--tls`
to also get the cert-watch auto-reload units. See [ssl.md](ssl.md).

## Migrating from the venv-based install

A re-install transparently migrates a host that used the old shell-script
venv layout: `serve service install` overwrites the same-named unit files
(now pointing at uvx) and `daemon-reload`s; the next restart runs the uvx
model. It does **not** delete `/opt/opp_ci`, `/opt/opp_env`,
`/opt/opp_repl`, or the service user's `.profile`/`setenv` shim
(operator-owned, possibly in use) — instead it prints a note listing
those now-unused paths for manual removal.
