# Plan: opp_ci as a systemd service on Ubuntu

Goal: package `opp_ci serve` and `opp_ci worker start` so they boot with
the machine and can be controlled with the usual `systemctl
start/stop/restart/enable/disable/status` commands. The same package
should work on three deployment shapes documented in
[`doc/deployment.md`](../doc/deployment.md):

1. **Coordinator only** — only `serve` runs.
2. **Worker only** — only `worker start` runs (one or more workers).
3. **Combined host** — both `serve` and one or more workers run on the
   same machine (the "local development" shape promoted to a daemon).

The choice between these is a *configuration* decision: enabling the
relevant unit(s). There is no single "opp_ci" process; the systemd
layer just wraps the two existing CLI subcommands.

## Design

### Unit layout

Three units plus one target:

| Unit | Purpose | Multiplicity |
|---|---|---|
| `opp_ci-serve.service` | Runs `opp_ci serve` | Singleton |
| `opp_ci-worker@.service` | Templated unit. Each instance runs `opp_ci worker start` for one registered worker | One per worker name |
| `opp_ci.target` | Umbrella: pulls in whichever services are enabled on this host | Singleton |

Why a template for the worker:

- Worker identity (`--token`, optional `--name`) is per-instance. A
  template (`@.service`) maps cleanly: `opp_ci-worker@builder-1.service`
  uses `/etc/opp_ci/workers/builder-1.env`.
- Hosts with multiple distinct workers (different tag sets, different
  concurrencies, e.g. a podman-only worker and a nix-only worker on the
  same machine) are first-class — no unit duplication.
- Single-worker hosts are equally clean: enable
  `opp_ci-worker@default.service` and write one env file.

Why a target:

- Lets `systemctl enable opp_ci.target` be the single "turn this host
  on" command. The target's `Wants=` is computed at install time from
  the role chosen during setup.
- Stop/restart of the whole stack: `systemctl restart opp_ci.target`.

### Install layout on the host

```
/opt/opp_ci/                         repo checkout (git clone)
  .venv/                             python venv with `pip install -e .`
  bin/opp_ci                         existing launcher

/etc/opp_ci/
  opp_ci.env                         shared env vars (DATABASE_URL, …)
  serve.env                          serve-only overrides (HOST, PORT, …)
  workers/
    default.env                      OPP_CI_COORDINATOR, OPP_CI_WORKER_TOKEN
    builder-1.env                    one file per templated instance

/var/lib/opp_ci/                     state dir (sqlite DB if used, caches)
/var/log/opp_ci/                     reserved; journald is primary

/etc/systemd/system/
  opp_ci.target
  opp_ci-serve.service
  opp_ci-worker@.service
```

The repo's existing `setenv` already activates `.venv` and prepends
`bin/` to `PATH`. The unit files do not source `setenv`; they invoke
`/opt/opp_ci/.venv/bin/opp_ci` directly so there is no shell layer to
debug.

### System user

- Dedicated `opp_ci` system user and group, home `/var/lib/opp_ci`,
  shell `/usr/sbin/nologin`.
- Owns `/var/lib/opp_ci` and `/etc/opp_ci/workers/*.env` (mode 0600;
  these contain worker tokens).
- The serve user can be the same `opp_ci` account — no privilege split
  needed for a single-tenant deploy.

Caveat: the worker calls `opp_env`, which calls Nix. Nix multi-user
installs put the daemon under `nix-daemon.service` (system-wide store
at `/nix`) and per-user profiles under `~/.nix-profile`. The `opp_ci`
user therefore needs a real `$HOME` (`/var/lib/opp_ci` works) and
`nix-daemon.service` must be active. Podman likewise needs subuid/subgid
entries for `opp_ci`. These prerequisites are documented in the install
step rather than baked into the unit, because some hosts run neither.

### Config split

| File | Read by | Contents (examples) |
|---|---|---|
| `/etc/opp_ci/opp_ci.env` | both | `OPP_CI_DATABASE_URL`, `OPP_CI_PROJECT_DIR`, `OPP_CI_CACHE_DIR`, `OPP_CI_REFERENCE_PLATFORM` |
| `/etc/opp_ci/serve.env` | serve | `OPP_CI_SERVE_HOST`, `OPP_CI_SERVE_PORT`, `OPP_CI_COORDINATOR_URL`, `OPP_CI_GITHUB_*` |
| `/etc/opp_ci/workers/<name>.env` | one worker instance | `OPP_CI_COORDINATOR_URL`, `OPP_CI_WORKER_TOKEN`, `OPP_CI_WORKER_POLL_INTERVAL`, `OPP_CI_WORKER_HEARTBEAT_INTERVAL` |

The serve unit reads `EnvironmentFile=/etc/opp_ci/opp_ci.env` and then
`EnvironmentFile=-/etc/opp_ci/serve.env` (the `-` makes the second
optional). The worker template reads `opp_ci.env` and
`workers/%i.env`, where `%i` is the instance name.

Two new env vars need to be added to `opp_ci/config.py` so the serve
unit can drive `--host`/`--port` from the env file rather than a
command line:

- `OPP_CI_SERVE_HOST` (default `127.0.0.1`)
- `OPP_CI_SERVE_PORT` (default `8080`)

Then the unit's `ExecStart=` is fixed:

```
ExecStart=/opt/opp_ci/.venv/bin/opp_ci serve \
    --host ${OPP_CI_SERVE_HOST} --port ${OPP_CI_SERVE_PORT}
```

Worker similarly reads `OPP_CI_COORDINATOR_URL` and a new
`OPP_CI_WORKER_TOKEN` env var rather than requiring them on the command
line. This requires a small change in `cli.py:worker_start` to fall
back to env vars when `--coordinator` / `--token` are omitted.

### Unit contents (sketch)

`/etc/systemd/system/opp_ci-serve.service`:

```ini
[Unit]
Description=opp_ci coordinator (web UI + API + scheduler)
Documentation=https://github.com/omnetpp/opp_ci
After=network-online.target postgresql.service
Wants=network-online.target
PartOf=opp_ci.target

[Service]
Type=simple
User=opp_ci
Group=opp_ci
WorkingDirectory=/var/lib/opp_ci
EnvironmentFile=/etc/opp_ci/opp_ci.env
EnvironmentFile=-/etc/opp_ci/serve.env
ExecStart=/opt/opp_ci/.venv/bin/opp_ci serve \
    --host ${OPP_CI_SERVE_HOST} --port ${OPP_CI_SERVE_PORT}
Restart=on-failure
RestartSec=5s

# Hardening — safe for serve (no Nix, no podman)
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/opp_ci

[Install]
WantedBy=opp_ci.target
```

`/etc/systemd/system/opp_ci-worker@.service`:

```ini
[Unit]
Description=opp_ci worker (%i)
Documentation=https://github.com/omnetpp/opp_ci
After=network-online.target nix-daemon.service
Wants=network-online.target
PartOf=opp_ci.target

[Service]
Type=simple
User=opp_ci
Group=opp_ci
WorkingDirectory=/var/lib/opp_ci
EnvironmentFile=/etc/opp_ci/opp_ci.env
EnvironmentFile=/etc/opp_ci/workers/%i.env
ExecStart=/opt/opp_ci/.venv/bin/opp_ci worker start
Restart=on-failure
RestartSec=10s
# Worker handles SIGINT / SIGTERM cleanly per doc/workers.md.
KillSignal=SIGTERM
TimeoutStopSec=60s

# Looser hardening — worker shells out to opp_env / nix / podman.
# Keep these off until proven to work in your environment.
# NoNewPrivileges=true
# ProtectSystem=strict
# ProtectHome=true

[Install]
WantedBy=opp_ci.target
```

`/etc/systemd/system/opp_ci.target`:

```ini
[Unit]
Description=opp_ci (coordinator and/or workers)
Documentation=https://github.com/omnetpp/opp_ci
# Wants= is filled in at install time, e.g.
#   Wants=opp_ci-serve.service opp_ci-worker@default.service

[Install]
WantedBy=multi-user.target
```

### Operator UX

After install:

```bash
# Single-host (serve + one worker)
sudo systemctl enable --now opp_ci-serve.service
sudo systemctl enable --now opp_ci-worker@default.service
sudo systemctl enable opp_ci.target              # auto-start on boot

# Worker-only host with two distinct workers
sudo systemctl enable --now opp_ci-worker@podman-builder.service
sudo systemctl enable --now opp_ci-worker@nix-builder.service

# Coordinator-only host
sudo systemctl enable --now opp_ci-serve.service

# Day-to-day
sudo systemctl restart opp_ci-serve.service
sudo systemctl stop    opp_ci-worker@default.service
sudo systemctl status  opp_ci.target              # whole-host view
journalctl -fu opp_ci-serve.service               # follow logs
```

## Implementation steps

1. **Code changes** in `opp_ci/`:
   - Add `OPP_CI_SERVE_HOST` / `OPP_CI_SERVE_PORT` to
     `opp_ci/config.py`. Update `cli.py:serve` so the click defaults
     come from these env vars (not hard-coded literals).
   - Update `cli.py:worker_start` so `--coordinator` / `--token` fall
     back to `OPP_CI_COORDINATOR_URL` / `OPP_CI_WORKER_TOKEN` when not
     given; mark the click options optional in that case.
   - Add a short section to [`doc/configuration.md`](../doc/configuration.md)
     documenting the new vars.

2. **Packaging artefacts** under a new directory `packaging/systemd/`:
   - `opp_ci.target`
   - `opp_ci-serve.service`
   - `opp_ci-worker@.service`
   - `opp_ci.env.example`, `serve.env.example`, `worker.env.example`
   - `install.sh` — idempotent installer that:
     a. Creates `opp_ci` system user/group (`useradd --system`).
     b. Creates `/opt/opp_ci`, `/etc/opp_ci/workers`, `/var/lib/opp_ci`
        with correct ownership and mode (env dirs root:opp_ci 0750;
        token files 0640).
     c. Either clones the repo into `/opt/opp_ci` or assumes it is
        already there; creates `.venv` and runs `pip install -e .`.
     d. Copies unit files to `/etc/systemd/system/`, runs
        `systemctl daemon-reload`.
     e. Copies `*.env.example` to `/etc/opp_ci/` only if the target
        file does not yet exist, so re-running the installer does not
        overwrite live config.
     f. Prints next-step instructions (which units to enable for the
        chosen role).
   - `uninstall.sh` — disables units, removes unit files, leaves
     `/etc/opp_ci` and `/var/lib/opp_ci` untouched (config + DB
     preservation).

3. **Documentation**:
   - New `doc/systemd.md` covering: install, role selection
     (coordinator / worker / combined), env files, multi-worker setup,
     log access via `journalctl`, hardening notes, Nix/Podman
     prerequisites for worker hosts.
   - Link it from `doc/deployment.md` (replacing or supplementing the
     "Web server" subsection that currently just shows `opp_ci serve`
     on a bare shell).

4. **Verification checklist** (manual, on a clean Ubuntu 24.04 VM):
   - Install with `install.sh`; confirm `opp_ci` user exists and venv
     populated.
   - Enable + start `opp_ci-serve.service`; verify `curl
     http://127.0.0.1:8080/api/health` (or whichever health endpoint
     exists; pick one and document it).
   - Register a worker via `opp_ci worker register`, write its token
     into `/etc/opp_ci/workers/default.env`, enable + start
     `opp_ci-worker@default.service`; verify the worker appears
     `online` in the web UI within `OPP_CI_WORKER_HEARTBEAT_INTERVAL`.
   - `systemctl stop opp_ci-worker@default.service` and confirm the
     worker shuts down within `TimeoutStopSec`, with no stuck job in
     the DB (it should drop back to `queued`).
   - Reboot the VM with `opp_ci.target` enabled; confirm both units
     come up automatically.
   - Crash test: `kill -9` the serve process; confirm
     `Restart=on-failure` brings it back within `RestartSec`.

## Open questions

- **Where does the repo live on the deploy host?** The plan assumes
  `/opt/opp_ci` is a git checkout with an editable install. An
  alternative is to publish a wheel and `pip install opp_ci` into a
  venv under `/opt/opp_ci/.venv`, with no source tree on the host.
  Editable is simpler today (matches `doc/deployment.md`) but the wheel
  path is cleaner for production; decide before writing `install.sh`.

- **Database lifecycle.** On a coordinator-only host running
  PostgreSQL, the serve unit has `After=postgresql.service`. If the
  deploy uses a remote Postgres (cloud DB), that `After=` is harmless
  but cosmetically wrong; the installer should detect and omit it. Not
  blocking.

- **Hardening on the worker.** `ProtectSystem=strict` plus a `/nix`
  read-only bind mount is probably workable, but Nix store GC and
  `opp_env install` may need write access in awkward places. Defer
  hardening on the worker until after a working baseline; ship with
  hardening commented out.

- **Log retention.** Journald defaults are usually fine, but workers
  produce verbose output. Document the `journalctl --vacuum-time=…`
  knob and the option to add a drop-in `LogRateLimitIntervalSec=0` if
  rate limiting drops worker output. Not blocking.

- **Per-worker resource limits.** `CPUQuota=`, `MemoryMax=` per
  templated instance would let an operator cap a worker that runs
  alongside other services. Worth mentioning in the doc but not in the
  default unit.
