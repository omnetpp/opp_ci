# Running an opp_ci worker as a launchd service (macOS)

On macOS opp_ci workers run as **launchd LaunchDaemons** so they start on
boot, restart on crash, and are controlled with `launchctl`. This is the
macOS counterpart of the Linux [systemd setup](systemd.md), but scoped to
**workers only** — there is no `serve`, no coordinator, and no PostgreSQL
provisioning on macOS. A macOS worker polls a remote coordinator
(typically the Linux `ci.example.org`) and needs only **outbound** network
access (see [workers.md](workers.md)).

The packaging artefacts live in
[`packaging/launchd/`](../packaging/launchd/). As on Linux, the worker
runs under a dedicated unprivileged **`opp_ci`** service account.

## How launchd differs from systemd

macOS has no systemd; the service manager is **launchd**, driven by
`.plist` files and the `launchctl` CLI. The mapping is not 1:1:

| systemd | launchd | In this packaging |
|---|---|---|
| `opp_ci-worker@.service` template | *no instance templating* | One plist rendered per worker name from `worker.plist.template` |
| `EnvironmentFile=…/%i.env` | *no file-sourcing* | A wrapper script sources the env file, then `exec`s the worker |
| `Restart=on-failure` + `RestartSec=10s` | `KeepAlive{SuccessfulExit:false}` + `ThrottleInterval=10` | Direct mapping |
| `TimeoutStopSec=60s` | `ExitTimeOut=60` | SIGTERM, then SIGKILL after the timeout |
| `journalctl -fu …` | `StandardOutPath`/`StandardErrorPath` + `newsyslog` | Log to a file, rotate with newsyslog |
| `opp_ci.target` umbrella | *none* | `opp_ci-workers {start\|stop\|restart\|status}` helper |
| `User=opp_ci` | `UserName`/`GroupName` + a **LaunchDaemon** | Daemon (not Agent) so it runs with no login |
| `NoNewPrivileges`, `ProtectSystem`, … | *no equivalent* | No hardening on macOS (see below) |

**Daemon, not Agent.** Because the worker must run with no user logged in
and as a fixed service account, it is a **LaunchDaemon** in
`/Library/LaunchDaemons/` (loaded into the `system` domain), not a
per-user LaunchAgent. The `UserName`/`GroupName` keys make it run as
`opp_ci` rather than root.

## The env-injection model (the central design point)

systemd injects per-worker env vars with
`EnvironmentFile=/etc/opp_ci/workers/%i.env`. launchd has no equivalent —
its `EnvironmentVariables` dict is static and can't read a file. Rather
than bake the worker token into a world-readable plist, the plist's
`ProgramArguments` calls a small wrapper,
[`/opt/opp_ci/bin/opp_ci-worker-run <name>`](../packaging/launchd/opp_ci-worker-run),
which does the equivalent of systemd's two `EnvironmentFile=` lines:

```bash
set -a
[ -f /etc/opp_ci/opp_ci.env ]          && . /etc/opp_ci/opp_ci.env
[ -f "/etc/opp_ci/workers/$name.env" ] && . "/etc/opp_ci/workers/$name.env"
set +a
exec /opt/opp_ci/.venv/bin/opp_ci worker start
```

This keeps the token in a **0600 file owned by `opp_ci`** (not in the
plist), and makes token rotation an "edit the env file +
`launchctl kickstart -k`" with no plist change. The env files are
byte-for-byte the same format as the Linux ones — the
[`worker.env.example`](../packaging/launchd/worker.env.example) and
[`opp_ci.env.example`](../packaging/launchd/opp_ci.env.example) under
`packaging/launchd/` are **symlinks** into `packaging/systemd/`, so the
two platforms never drift.

> Note: `opp_ci.config` also auto-loads `/etc/opp_ci/opp_ci.env` on import
> (that path exists on macOS since `/etc` → `/private/etc`). The wrapper
> sources it too, so it also reaches `exec`'d subprocesses (`opp_env`)
> that don't import `config.py`. Precedence matches Linux: explicit env
> wins, the file fills the gap. **No opp_ci code change is needed** — this
> is purely packaging.

## Install layout on the host

Mirrors Linux where macOS conventions allow; `/etc/opp_ci` and
`/opt/opp_ci` are identical so config files, the `config.py` autoload
path, and operator muscle memory all carry over. State and logs use
`/usr/local/var/...` because macOS has no `/var/lib` or `/var/log` by
default.

```
/opt/opp_ci/                       repo checkout
  .venv/                           venv with `pip install -e .[client,podman]`
  bin/opp_ci-worker-run            env-sourcing wrapper
  bin/opp_ci-workers               umbrella start/stop/status helper

/etc/opp_ci/                       (same paths as Linux; /etc is /private/etc)
  opp_ci.env                       shared env (0640 root:opp_ci)
  workers/
    default.env                    OPP_CI_COORDINATOR_URL + OPP_CI_WORKER_TOKEN (0600 opp_ci)
    <name>.env                     one file per worker

/usr/local/var/opp_ci/             state dir = $HOME for opp_ci (Nix profile, caches)
/usr/local/var/log/opp_ci/         worker logs (launchd has no journald)

/Library/LaunchDaemons/
  org.omnetpp.opp_ci.worker.<name>.plist   one plist per worker name

/etc/newsyslog.d/opp_ci.conf       log rotation drop-in
```

## Install

Prerequisites on the host: a `python3` (system or Homebrew), `git`, and
`rsync` (optional). A working `.git/` must be present in the source so
`setuptools-scm` can derive the version.

From a checkout of the repo, run as root:

```bash
sudo packaging/launchd/install.sh                       # → worker "default"
sudo packaging/launchd/install.sh builder-1 nix-builder  # → two workers
```

The installer is idempotent. It:

- creates the hidden `opp_ci` service account via `dscl` (a free
  UID/GID < 500, `IsHidden=1`, home `/usr/local/var/opp_ci`, shell
  `/bin/bash`, password disabled) — skipped if it already exists,
- creates `/opt/opp_ci`, `/etc/opp_ci/workers`, `/usr/local/var/opp_ci`,
  and `/usr/local/var/log/opp_ci` with the right owners/modes (workers
  dir 0750 opp_ci; token files 0600 opp_ci),
- syncs the source tree to `/opt/opp_ci`, builds `/opt/opp_ci/.venv`, and
  `pip install -e`s opp_ci with the **worker-only** extras
  (`client`, `podman` — no `web`/`postgres`); also syncs sibling
  `opp_env` and `opp_repl` exactly as the Linux installer does,
- installs the wrapper and the `opp_ci-workers` helper into
  `/opt/opp_ci/bin/`,
- seeds `/etc/opp_ci/opp_ci.env` and `workers/default.env` from the
  `.example` files (only if missing — existing config is preserved),
- renders `worker.plist.template` for each requested name into
  `/Library/LaunchDaemons/`, then `chown root:wheel` + `chmod 644`s each
  plist (**launchd refuses to load a plist that isn't owned by root or is
  group/world-writable — a common gotcha**),
- installs the `newsyslog.d` drop-in.

It does **not** bootstrap (start) any daemon — that is the next step,
after you paste the token.

## Register and start a worker

Register the worker once on the coordinator and copy the token.
`--auto-tags` reads `/etc/os-release`, which doesn't exist on macOS, so
pass explicit tags instead (see the tag scheme in
[workers.md](workers.md#capability-tags)):

```bash
opp_ci worker register --remote https://ci.example.org \
    --name builder-1 \
    --tags os:macos,os:macos-15,arch:arm64
# → Token: <copy this>
```

Paste the token into the per-worker env file (0600 `opp_ci:opp_ci`):

```bash
sudo vi   /etc/opp_ci/workers/builder-1.env
# OPP_CI_COORDINATOR_URL=https://ci.example.org
# OPP_CI_WORKER_TOKEN=<paste here>
sudo chown opp_ci:opp_ci /etc/opp_ci/workers/builder-1.env
sudo chmod 600           /etc/opp_ci/workers/builder-1.env
```

Bootstrap the daemon (`RunAtLoad` starts it immediately):

```bash
sudo launchctl bootstrap system \
    /Library/LaunchDaemons/org.omnetpp.opp_ci.worker.builder-1.plist
```

## Day-to-day operations

```bash
# Restart (e.g. after editing config or rotating the token)
sudo launchctl kickstart -k system/org.omnetpp.opp_ci.worker.builder-1

# Stop + unload
sudo launchctl bootout system/org.omnetpp.opp_ci.worker.builder-1

# Status
launchctl print system/org.omnetpp.opp_ci.worker.builder-1

# All workers at once (the opp_ci.target analogue)
sudo /opt/opp_ci/bin/opp_ci-workers status
sudo /opt/opp_ci/bin/opp_ci-workers restart

# Logs
tail -f /usr/local/var/log/opp_ci/worker-builder-1.log
log stream --predicate 'process == "opp_ci"' --info   # unified logging

# Token rotation — no plist change needed
sudo vi /etc/opp_ci/workers/builder-1.env
sudo launchctl kickstart -k system/org.omnetpp.opp_ci.worker.builder-1
```

`bootstrap`/`bootout`/`kickstart`/`print` are the modern domain-based
subcommands. The legacy `launchctl load`/`unload`/`list` still work but
are deprecated.

The plist sets `ExitTimeOut=60`, so `bootout` gives the worker up to 60s
to drain in-flight work after `SIGTERM` before `SIGKILL` —
`bootout` won't strand a job that finishes within the timeout (the
coordinator re-queues anything still in flight).
`KeepAlive{SuccessfulExit:false}` relaunches the worker after a crash
(non-zero exit), throttled to `ThrottleInterval=10` seconds.

## Worker prerequisites on macOS

Same toolchain shape as Linux, different provisioning:

- **opp_env + toolchain** must be runnable as the `opp_ci` user, with a
  real `$HOME` (`/usr/local/var/opp_ci`) for `~/.nix-profile`. The plist
  sets `HOME` accordingly.
- **Nix**: the macOS multi-user install runs `org.nixos.nix-daemon` (a
  launchd daemon) over a `/nix` volume; the `opp_ci` user needs `/nix`
  access. launchd has no `After=nix-daemon.service` ordering, but the
  daemon is up at boot. If a race ever appears, the wrapper can be
  extended to block on `/nix/var/nix/daemon-socket/socket` before exec.
- **Podman**: on macOS podman runs jobs inside a per-user `podman machine`
  (a Linux VM) that is normally started interactively. Running podman jobs
  from a headless service account is materially harder than Linux rootless
  podman and is **out of scope for the first cut** — do **not** give macOS
  workers a `podman` tag, so the dispatcher never sends them
  podman-isolation jobs.

Skipping a toolchain just means the worker isn't tagged for it and won't
be dispatched those jobs — there is no startup error (same as Linux).

## Hardening

macOS has **no equivalent** of systemd's
`NoNewPrivileges`/`ProtectSystem`/`ProtectHome`/`PrivateTmp`. The closest
mechanisms (`sandbox-exec`, App Sandbox entitlements) don't fit a
long-running CLI service, and `sandbox-exec` is deprecated. Isolation here
relies solely on the dedicated unprivileged `opp_ci` account and file
permissions. The Linux [hardening section](systemd.md#hardening) has no
counterpart on macOS.

## Log retention

There is no journald. Each worker logs to
`/usr/local/var/log/opp_ci/worker-<name>.log`. The installer ships a
[`newsyslog.d` drop-in](../packaging/launchd/newsyslog-opp_ci.conf) at
`/etc/newsyslog.d/opp_ci.conf` so macOS's built-in `newsyslog(8)` rotates
them (keep 7 generations, rotate at 5 MB). This is the macOS analogue of
`journalctl --vacuum-time`.

## Uninstall

```bash
sudo packaging/launchd/uninstall.sh
```

Boots out and removes the worker plists and the newsyslog drop-in. Leaves
`/opt/opp_ci`, `/etc/opp_ci`, `/usr/local/var/opp_ci`, the logs, and the
`opp_ci` account in place so a re-install doesn't lose tokens or caches.
