# Running opp_ci as a systemd service

On Ubuntu (and other systemd-based distros) opp_ci can be installed as
a system service so it starts on boot and can be managed with the usual
`systemctl` commands. The packaging artefacts live in
[`packaging/systemd/`](../packaging/systemd/).

There is no single "opp_ci" process: the systemd layer wraps the
existing `opp_ci serve` and `opp_ci worker start` subcommands. The host
role — coordinator, worker, or combined — is a matter of which units
you enable.

## Units

| Unit | Purpose | Multiplicity |
|---|---|---|
| `opp_ci-serve.service` | Runs `opp_ci serve` (web UI + API + scheduler) | Singleton |
| `opp_ci-worker@.service` | Templated unit. Each instance runs `opp_ci worker start` for one registered worker | One per worker name |
| `opp_ci.target` | Umbrella: pulls in whichever services are enabled on this host | Singleton |

## Install

From a checkout of the repo:

```bash
sudo packaging/systemd/install.sh
```

The installer is idempotent. It:

- creates the `opp_ci` system user/group (home `/var/lib/opp_ci`,
  shell `/usr/sbin/nologin`),
- copies the source tree to `/opt/opp_ci` (excluding `.venv`, `.git`,
  caches, and any local SQLite DB),
- creates `/opt/opp_ci/.venv` and `pip install -e`s opp_ci into it,
- installs the three unit files into `/etc/systemd/system/`,
- seeds `/etc/opp_ci/` with `opp_ci.env`, `serve.env`, and
  `workers/default.env` from the `.example` files (only if missing —
  existing config is preserved on re-install),
- runs `systemctl daemon-reload`.

It does **not** enable or start any unit. That is the next step.

## Role selection

### Coordinator only

```bash
sudoedit /etc/opp_ci/opp_ci.env       # set OPP_CI_DATABASE_URL
sudoedit /etc/opp_ci/serve.env        # set OPP_CI_COORDINATOR_URL, GitHub tokens
sudo systemctl enable --now opp_ci-serve.service
sudo systemctl enable opp_ci.target
```

### Worker only

Register the worker once on the coordinator (locally or via
`--remote`):

```bash
opp_ci worker register --name builder-1 --auto-tags
# Token: <copy this>
```

Paste the token into a per-instance env file on the worker host:

```bash
sudoedit /etc/opp_ci/workers/builder-1.env
# OPP_CI_COORDINATOR_URL=https://ci.example.org
# OPP_CI_WORKER_TOKEN=<paste here>
sudo chown opp_ci:opp_ci /etc/opp_ci/workers/builder-1.env
sudo chmod 0600          /etc/opp_ci/workers/builder-1.env
sudo systemctl enable --now opp_ci-worker@builder-1.service
sudo systemctl enable opp_ci.target
```

### Combined (coordinator + workers on one host)

Enable both:

```bash
sudo systemctl enable --now opp_ci-serve.service
sudo systemctl enable --now opp_ci-worker@default.service
sudo systemctl enable opp_ci.target
```

### Multiple workers on one host

Write one env file per worker name and enable one instance per file:

```bash
sudoedit /etc/opp_ci/workers/podman-builder.env
sudoedit /etc/opp_ci/workers/nix-builder.env
sudo systemctl enable --now opp_ci-worker@podman-builder.service
sudo systemctl enable --now opp_ci-worker@nix-builder.service
```

Each instance has its own poll loop, heartbeat, and token, but shares
`/etc/opp_ci/opp_ci.env`.

## Day-to-day operations

```bash
# Start / stop / restart
sudo systemctl restart opp_ci-serve.service
sudo systemctl stop    opp_ci-worker@default.service

# Whole-host view
systemctl status opp_ci.target

# Logs
journalctl -fu opp_ci-serve.service
journalctl -fu opp_ci-worker@default.service

# Apply a config change
sudoedit /etc/opp_ci/opp_ci.env
sudo systemctl restart opp_ci.target
```

The serve unit has `Restart=on-failure RestartSec=5s`; the worker has
`Restart=on-failure RestartSec=10s` and `TimeoutStopSec=60s` so the
worker has up to 60s to drain in-flight work after `SIGTERM`. Both
honour `SIGINT`/`SIGTERM` for clean shutdown — `systemctl stop` will
not strand in-flight jobs as long as they finish within the timeout.

## Environment files

| File | Read by | Mode | Purpose |
|---|---|---|---|
| `/etc/opp_ci/opp_ci.env` | both | 0640 root:opp_ci | Shared (`OPP_CI_DATABASE_URL`, `OPP_CI_PROJECT_DIR`, …) |
| `/etc/opp_ci/serve.env` | serve | 0640 root:opp_ci | `OPP_CI_SERVE_HOST`, `OPP_CI_SERVE_PORT`, `OPP_CI_COORDINATOR_URL`, GitHub tokens |
| `/etc/opp_ci/workers/<name>.env` | one worker | 0600 opp_ci:opp_ci | Per-instance `OPP_CI_COORDINATOR_URL` + `OPP_CI_WORKER_TOKEN` |

`serve.env` is referenced as `EnvironmentFile=-/etc/opp_ci/serve.env`,
so it may be absent; `opp_ci.env` is required.

Every variable referenced in these files is listed in
[`configuration.md`](configuration.md).

## Prerequisites per role

**Coordinator**: nothing beyond a working PostgreSQL (or just SQLite
in `/var/lib/opp_ci/`). The serve unit has
`After=network-online.target postgresql.service`; if your database
lives on another host, the `postgresql.service` ordering is harmless
(it just resolves to a no-op).

**Worker**: opp_env and its toolchain (Nix and/or podman) must be
available to the `opp_ci` user.

- Nix multi-user install: `nix-daemon.service` must exist and be
  active. The worker unit has `After=nix-daemon.service`. The `opp_ci`
  user needs `/nix` accessible and a real `$HOME` for
  `~/.nix-profile` (the installer points it at `/var/lib/opp_ci`).
- Podman rootless: ensure `/etc/subuid` and `/etc/subgid` have entries
  for `opp_ci`, e.g. `usermod --add-subuids 100000-165535
  --add-subgids 100000-165535 opp_ci`.

Skipping a toolchain just means the worker cannot pick up jobs with
the corresponding capability tag — there is no error at startup.

## Hardening

The serve unit ships with strict hardening:

```
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/opp_ci
```

This is safe because the coordinator does not invoke Nix or podman.

The worker unit ships with hardening **commented out**, because Nix
and podman need filesystem access that is awkward to enumerate
generically. Tighten it on a per-host basis once the worker is known
to work; sensible starting point:

```ini
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/opp_ci /nix/var
```

## Per-worker resource caps

Each templated instance can be capped independently with a drop-in:

```bash
sudo systemctl edit opp_ci-worker@nix-builder.service
```

```ini
[Service]
CPUQuota=400%
MemoryMax=8G
```

This keeps a chatty worker from starving the rest of the host.

## Log retention

Logs go to journald by default. To bound disk usage:

```bash
sudo journalctl --vacuum-time=14d
```

If you observe dropped lines during a verbose run, add a drop-in to
disable rate limiting for that unit:

```ini
[Service]
LogRateLimitIntervalSec=0
```

## Uninstall

```bash
sudo packaging/systemd/uninstall.sh
```

Removes the units. Leaves `/opt/opp_ci`, `/etc/opp_ci`,
`/var/lib/opp_ci`, and the `opp_ci` user in place so a re-install does
not lose tokens or the database.
