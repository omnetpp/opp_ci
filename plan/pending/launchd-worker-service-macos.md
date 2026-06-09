# Plan: opp_ci worker as a launchd service on macOS

Goal: run `opp_ci worker start` as a managed background service on macOS,
so it boots with the machine, restarts on crash, and can be controlled
with `launchctl`. This is the macOS analogue of the Linux systemd setup
in [`packaging/systemd/`](../../packaging/systemd/) and
[`doc/systemd.md`](../../doc/systemd.md), but scoped to **workers only**
— no `serve`, no coordinator, no PostgreSQL provisioning, no TLS path
units. As on Linux, the service runs under a dedicated **`opp_ci`**
service account.

A macOS worker host polls a remote coordinator (typically the Linux
`ci.example.org`) for jobs. Workers need only outbound network access
(per [`doc/workers.md`](../../doc/workers.md)), so there is nothing to
expose and no inbound port.

## Why launchd, and how it differs from systemd

macOS has no systemd; the system service manager is **launchd**, driven
by `.plist` files and the `launchctl` CLI. The mapping is not 1:1, and
four differences drive the whole design:

| systemd concept | launchd equivalent | Consequence for this plan |
|---|---|---|
| `opp_ci-worker@.service` **template** | *none* — launchd has no instance templating | Generate **one plist per worker name** from a template at install time |
| `EnvironmentFile=/etc/opp_ci/workers/%i.env` | *none* — plists have a static `EnvironmentVariables` dict, can't read a file | Use a small **wrapper script** that sources the env file, then `exec`s the worker |
| `Restart=on-failure` + `RestartSec=10s` | `KeepAlive={SuccessfulExit:false}` + `ThrottleInterval` | Direct mapping (throttle min is 10s) |
| `TimeoutStopSec=60s` (drain on SIGTERM) | `ExitTimeOut=60` | Direct mapping; launchd sends SIGTERM then SIGKILL after the timeout |
| `journalctl -fu …` | unified logging + `StandardOutPath`/`StandardErrorPath` | Redirect stdout/stderr to a log file; rotate with `newsyslog` |
| `opp_ci.target` umbrella | *none* | A helper script that boots/boots-out all `org.omnetpp.opp_ci.worker.*` plists |
| `User=opp_ci` | `UserName` / `GroupName` keys + a `LaunchDaemon` | LaunchDaemon (not LaunchAgent) so it runs without an interactive login |
| `NoNewPrivileges`, `ProtectSystem=strict`, … | *no equivalent* | Hardening section is N/A on macOS (note it explicitly) |

**Daemon vs Agent.** Because the worker must run with no user logged in
and as a fixed service account, it is a **LaunchDaemon**
(`/Library/LaunchDaemons/`, loaded into the `system` domain), not a
per-user LaunchAgent. The `UserName`/`GroupName` keys make it run as
`opp_ci` rather than root.

## The env-injection problem (central design point)

On Linux, systemd's `EnvironmentFile=/etc/opp_ci/workers/%i.env`
injects `OPP_CI_COORDINATOR_URL` and `OPP_CI_WORKER_TOKEN` per instance.
launchd has no such mechanism. Two options:

1. **Bake env vars into each plist's `EnvironmentVariables` dict.**
   Simple, but puts the worker token inside a world-readable plist in
   `/Library/LaunchDaemons/` and means editing+reloading the plist to
   rotate a token. Rejected.
2. **Wrapper script sources the env file, then `exec`s the worker.**
   Chosen. The plist's `ProgramArguments` calls
   `/opt/opp_ci/bin/opp_ci-worker-run <name>`, which does roughly:

   ```bash
   #!/bin/bash
   set -euo pipefail
   name="$1"
   set -a
   [ -f /etc/opp_ci/opp_ci.env ]            && . /etc/opp_ci/opp_ci.env
   [ -f "/etc/opp_ci/workers/$name.env" ]   && . "/etc/opp_ci/workers/$name.env"
   set +a
   exec /opt/opp_ci/.venv/bin/opp_ci worker start
   ```

   This mirrors systemd's two `EnvironmentFile=` lines exactly, keeps the
   token in a 0600 file owned by `opp_ci` (not in the plist), and makes
   token rotation a "edit the env file + `launchctl kickstart -k`" with
   no plist change. The 0600 env files are byte-for-byte the same format
   as the Linux ones — [`worker.env.example`](../../packaging/systemd/worker.env.example)
   is reused verbatim.

Note: the shared `/etc/opp_ci/opp_ci.env` is *also* auto-loaded by
[`opp_ci/config.py`](../../opp_ci/config.py#L35) on import (it hardcodes
that path, which exists on macOS since `/etc` → `/private/etc`). The
wrapper sources it too so it wins for `exec`'d subprocesses (`opp_env`)
that don't import `config.py`; the precedence is identical to Linux
(explicit env wins, file fills the gap). **No opp_ci code change is
required** — this is purely packaging.

## Install layout on the host

Mirrors Linux where macOS conventions allow; deviates only where they
must. Keeping `/etc/opp_ci` and `/opt/opp_ci` identical to Linux means
the config files, the `config.py` autoload path, and operator muscle
memory all carry over.

```
/opt/opp_ci/                         repo checkout
  .venv/                             python venv with `pip install -e .[client,podman]`
  bin/opp_ci-worker-run              NEW: env-sourcing wrapper (this plan)

/etc/opp_ci/                         (same paths as Linux; /etc is /private/etc)
  opp_ci.env                         shared env (OPP_CI_PROJECT_DIR, OPP_CI_CACHE_DIR…)
  workers/
    default.env                      OPP_CI_COORDINATOR_URL, OPP_CI_WORKER_TOKEN  (0600 opp_ci)
    <name>.env                       one file per worker

/usr/local/var/opp_ci/               state dir (home for opp_ci; caches)         [macOS-idiomatic]
/usr/local/var/log/opp_ci/           worker logs (launchd has no journald)       [macOS-idiomatic]

/Library/LaunchDaemons/
  org.omnetpp.opp_ci.worker.default.plist     one plist per worker name
  org.omnetpp.opp_ci.worker.<name>.plist
```

Notes:
- **State/log dirs** use `/usr/local/var/...` rather than Linux's
  `/var/lib` and `/var/log`, which don't exist on macOS by default.
  The worker's `$HOME` points at the state dir (opp_env / Nix profile
  need a real home, same as Linux).
- A reverse-domain plist **label** (`org.omnetpp.opp_ci.worker.<name>`)
  is the macOS convention and what `launchctl` keys off. The `<name>`
  suffix replaces systemd's `@<name>` instance.

## System user (`opp_ci`) on macOS

There is no `useradd`. A **hidden service account** is created with
`dscl` (or `sysadminctl`). Requirements:

- A group `opp_ci` (`PrimaryGroupID`) and a user `opp_ci`.
- A UID/GID in the service range. macOS hides accounts with UID < 500;
  pick the next free UID below 500 (e.g. scan `dscl . -list /Users UniqueID`
  for the lowest free value in 200–499). Set
  `IsHidden=1` so it doesn't appear on the login screen.
- Home `= /usr/local/var/opp_ci`, shell `/bin/bash` (so `sudo -u opp_ci`
  and the wrapper work; opp_env needs a usable shell), password disabled.

Sketch of the `dscl` creation (the installer wraps this idempotently):

```bash
# group
sudo dscl . -create /Groups/opp_ci
sudo dscl . -create /Groups/opp_ci PrimaryGroupID 401
# user
sudo dscl . -create /Users/opp_ci
sudo dscl . -create /Users/opp_ci UniqueID 401
sudo dscl . -create /Users/opp_ci PrimaryGroupID 401
sudo dscl . -create /Users/opp_ci NFSHomeDirectory /usr/local/var/opp_ci
sudo dscl . -create /Users/opp_ci UserShell /bin/bash
sudo dscl . -create /Users/opp_ci RealName "opp_ci CI worker"
sudo dscl . -create /Users/opp_ci IsHidden 1
sudo dscl . -delete /Users/opp_ci PrimaryGroupID 20 2>/dev/null || true   # ensure clean
```

(`sysadminctl -addUser opp_ci -UID 401 -fullName … -home … -shell /bin/bash -roleAccount`
is the higher-level alternative on recent macOS; the installer can prefer
it when available and fall back to `dscl`.)

The Linux `subuid`/`subgid` + `loginctl enable-linger` provisioning for
rootless podman has **no macOS analogue** and is dropped. See worker
prerequisites below.

## The plist (template the installer fills per worker)

`/Library/LaunchDaemons/org.omnetpp.opp_ci.worker.<name>.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.omnetpp.opp_ci.worker.__NAME__</string>

    <key>ProgramArguments</key>
    <array>
        <string>/opt/opp_ci/bin/opp_ci-worker-run</string>
        <string>__NAME__</string>
    </array>

    <key>UserName</key>   <string>opp_ci</string>
    <key>GroupName</key>  <string>opp_ci</string>

    <key>WorkingDirectory</key>
    <string>/usr/local/var/opp_ci</string>

    <!-- PATH for opp_env/nix/podman lookups; mirrors the Linux unit's
         Environment=PATH. Includes the venv bin and Homebrew paths. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/opp_ci/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/usr/local/var/opp_ci</string>
    </dict>

    <!-- Restart=on-failure: relaunch only on non-zero/abnormal exit. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key> <false/>
    </dict>
    <!-- RestartSec≈10s. launchd's floor is 10s anyway. -->
    <key>ThrottleInterval</key> <integer>10</integer>

    <!-- Start at boot, no login required (this is why it's a Daemon). -->
    <key>RunAtLoad</key> <true/>

    <!-- TimeoutStopSec=60s: drain in-flight jobs before SIGKILL. Worker
         handles SIGTERM cleanly per doc/workers.md. -->
    <key>ExitTimeOut</key> <integer>60</integer>

    <!-- launchd has no journald; capture stdout/stderr to files. -->
    <key>StandardOutPath</key>
    <string>/usr/local/var/log/opp_ci/worker-__NAME__.log</string>
    <key>StandardErrorPath</key>
    <string>/usr/local/var/log/opp_ci/worker-__NAME__.log</string>

    <!-- Optional resource cap (RLIMIT-based; coarser than systemd's
         MemoryMax/CPUQuota). Commented out by default.
    <key>SoftResourceLimits</key>
    <dict><key>NumberOfFiles</key><integer>8192</integer></dict>
    -->
</dict>
</plist>
```

`__NAME__` is substituted at install time (the installer's `sed`).
There is no shipped `@`-style file as on Linux; instead a
`worker.plist.template` lives in the packaging dir and the installer
renders it per name.

## Packaging artefacts (new directory)

Under a new `packaging/launchd/` directory, parallel to
`packaging/systemd/`:

- `worker.plist.template` — the plist above with `__NAME__` placeholders.
- `opp_ci-worker-run` — the env-sourcing wrapper script (installed to
  `/opt/opp_ci/bin/`).
- `install.sh` — idempotent installer (macOS). Steps:
  1. Refuse to run unless `uname` is `Darwin` and `EUID==0`.
  2. Create the `opp_ci` group + hidden user via `sysadminctl`/`dscl`
     (skip if present); pick a free UID < 500.
  3. Create `/opt/opp_ci`, `/etc/opp_ci/workers`, `/usr/local/var/opp_ci`,
     `/usr/local/var/log/opp_ci` with correct owners/modes (workers dir
     0750 opp_ci; token files 0600 opp_ci; log dir 0750 opp_ci).
  4. Sync the repo into `/opt/opp_ci` (rsync/cp, same excludes as Linux),
     create `.venv`, `pip install -e .[client,podman]` (no `web`/`postgres`
     extras — worker-only). Sync sibling `opp_env`/`opp_repl` exactly as
     the Linux installer does.
  5. Install the wrapper to `/opt/opp_ci/bin/opp_ci-worker-run` (0755).
  6. Seed `/etc/opp_ci/opp_ci.env` and `/etc/opp_ci/workers/default.env`
     from the reused `.example` files (only if missing).
  7. Render `worker.plist.template` for each requested worker name into
     `/Library/LaunchDaemons/` (accept names as args, default `default`).
  8. `chown root:wheel` + `chmod 644` the plists (launchd requires the
     plist be owned by root and not group/world-writable, or it refuses
     to load it — call this out, it's a common gotcha).
  9. Print next steps (register on coordinator, paste token, bootstrap).
- `uninstall.sh` — `launchctl bootout` each worker plist, remove the
  plists, leave `/opt/opp_ci`, `/etc/opp_ci`, `/usr/local/var/opp_ci`,
  and the `opp_ci` account in place (token/cache preservation, same
  philosophy as the Linux uninstaller).
- `opp_ci-workers` — optional umbrella helper replacing `opp_ci.target`:
  `opp_ci-workers {start|stop|restart|status}` iterates over all
  `/Library/LaunchDaemons/org.omnetpp.opp_ci.worker.*.plist`.

The reused env example files (`opp_ci.env.example`, `worker.env.example`)
are symlinked or copied from `packaging/systemd/` so the two platforms
never drift — or, cleaner, moved to a shared `packaging/common/` that
both installers read. Decide in step 1 of implementation (see open
questions).

## Operator UX

```bash
# Install (renders a plist for the named workers; default: "default")
sudo packaging/launchd/install.sh                       # → worker "default"
sudo packaging/launchd/install.sh builder-1 nix-builder  # → two workers

# Register the worker on the (remote) coordinator, copy the token:
opp_ci worker register --remote https://ci.example.org \
    --name builder-1 --auto-tags
#   (note: --auto-tags reads /etc/os-release, which doesn't exist on
#    macOS — pass explicit --tags os:macos,os:macos-15,arch:arm64 instead;
#    see open questions / doc/workers.md tag scheme)

# Paste token into the per-worker env file (0600 opp_ci):
sudo vi /etc/opp_ci/workers/builder-1.env
sudo chown opp_ci:opp_ci /etc/opp_ci/workers/builder-1.env
sudo chmod 600           /etc/opp_ci/workers/builder-1.env

# Load + start the daemon (RunAtLoad makes bootstrap also start it):
sudo launchctl bootstrap system /Library/LaunchDaemons/org.omnetpp.opp_ci.worker.builder-1.plist

# Day-to-day
sudo launchctl kickstart -k system/org.omnetpp.opp_ci.worker.builder-1   # restart
sudo launchctl bootout   system/org.omnetpp.opp_ci.worker.builder-1      # stop+unload
launchctl print          system/org.omnetpp.opp_ci.worker.builder-1      # status
tail -f /usr/local/var/log/opp_ci/worker-builder-1.log                   # logs
log stream --predicate 'process == "opp_ci"' --info                      # unified logging

# Token rotation — no plist change needed:
sudo vi /etc/opp_ci/workers/builder-1.env
sudo launchctl kickstart -k system/org.omnetpp.opp_ci.worker.builder-1
```

(`launchctl bootstrap`/`bootout`/`kickstart`/`print` are the modern
domain-based subcommands; the legacy `load`/`unload`/`list` still work
but are deprecated — the doc should use the modern form.)

## Worker prerequisites on macOS

Same toolchain shape as Linux, different provisioning:

- **opp_env + toolchain** must be runnable as the `opp_ci` user, with a
  real `$HOME` (`/usr/local/var/opp_ci`) for `~/.nix-profile`.
- **Nix**: the macOS multi-user install runs `org.nixos.nix-daemon`
  (a launchd daemon) and a `/nix` volume. The `opp_ci` user needs `/nix`
  access. launchd has no `After=nix-daemon.service` ordering, but the
  daemon is already up at boot; if races appear, the wrapper can block on
  `/nix/var/nix/daemon-socket/socket` before exec.
- **Podman**: on macOS podman runs jobs inside a `podman machine` (a Linux
  VM). That VM is per-user and is normally started interactively
  (`podman machine start`). Running podman jobs from a headless service
  account is materially harder than Linux rootless podman and is **out of
  scope for the first cut** — tag macOS workers without `podman` so the
  dispatcher never sends them podman-isolation jobs. (Document this
  limitation; revisit if needed.)
- No subuid/subgid, no `enable-linger`, no rootless-podman migrate.

Skipping a toolchain just means the worker won't be tagged for it and
won't be dispatched those jobs — no startup error (same as Linux).

## Hardening

macOS has no `NoNewPrivileges`/`ProtectSystem`/`ProtectHome`/`PrivateTmp`.
The closest mechanisms (`sandbox-exec`, App Sandbox entitlements) don't
fit a long-running CLI service and `sandbox-exec` is deprecated. The doc
should state plainly that the Linux hardening section has no equivalent;
isolation relies on the dedicated unprivileged `opp_ci` account and file
permissions only.

## Log retention

No journald. Logs go to `/usr/local/var/log/opp_ci/worker-<name>.log`.
Ship a `newsyslog.d` drop-in
(`/etc/newsyslog.d/opp_ci.conf`) so macOS's built-in `newsyslog` rotates
them, e.g.:

```
# logfilename                              [owner:group]  mode count size when flags
/usr/local/var/log/opp_ci/worker-*.log     opp_ci:opp_ci  644  7     5000 *    GN
```

Document this as the macOS analogue of `journalctl --vacuum-time`.

## Documentation

- New `doc/launchd.md` — the macOS counterpart to `doc/systemd.md`,
  worker-only: install, the wrapper/env model, per-worker plists,
  `launchctl` day-to-day, the `os:macos*`/`arch:arm64` tagging caveat,
  log files + `newsyslog`, podman-on-macOS limitation, the
  no-hardening note.
- Cross-link from `doc/systemd.md` ("on macOS, see launchd.md") and from
  `doc/workers.md`'s "run workers under a process supervisor" bullet.

## Implementation steps

1. Decide the shared-vs-duplicated layout for the `.env.example` files
   (open question 1), then create `packaging/launchd/`.
2. Write `worker.plist.template` and `opp_ci-worker-run` wrapper.
3. Write `install.sh` (macOS) and `uninstall.sh`, plus the
   `opp_ci-workers` umbrella helper.
4. Write the `newsyslog.d` drop-in.
5. Write `doc/launchd.md`; add cross-links.
6. Verify on a real macOS host (checklist below). **No `opp_ci/*.py`
   changes are expected** — confirm this holds (the wrapper + existing
   `config.py` autoload should cover env injection).

## Verification checklist (manual, on a macOS host)

- Run `install.sh`; confirm the hidden `opp_ci` account exists
  (`dscl . -read /Users/opp_ci`), venv is populated, plist rendered.
- `launchctl bootstrap system …`; confirm `launchctl print system/…`
  shows the job running as `opp_ci`, and the log file fills.
- Register the worker on a coordinator with explicit macOS tags; confirm
  it shows `online` in the web UI within the heartbeat interval.
- `launchctl kickstart -k …` restarts cleanly; `bootout` drains within
  `ExitTimeOut` and the in-flight job returns to `queued`.
- Reboot the Mac; confirm `RunAtLoad` brings the worker back with no
  login.
- `kill -9` the worker PID; confirm `KeepAlive{SuccessfulExit:false}`
  relaunches it after `ThrottleInterval`.
- Edit the token env file + `kickstart -k`; confirm the new token takes
  effect with no plist edit.

## Open questions

- **Shared vs duplicated env examples.** `opp_ci.env.example` and
  `worker.env.example` are identical across platforms. Symlink from
  `packaging/launchd/` into `packaging/systemd/`, or hoist both to
  `packaging/common/` and point both installers there? Hoisting is
  cleaner but touches the (already shipped) Linux installer paths. Lean:
  hoist to `packaging/common/`, update both installers.
- **`--auto-tags` on macOS.** The coordinator-side `--auto-tags` reads
  `/etc/os-release` (Linux-only). For macOS workers, either (a) document
  that operators must pass explicit `--tags os:macos,os:macos-<ver>,
  arch:<arm64|amd64>`, or (b) extend `worker register --auto-tags` to
  detect Darwin via `sw_vers` + `uname -m`. (b) is a small, separable
  opp_ci enhancement; out of scope for the packaging plan but worth a
  follow-up ticket.
- **Podman on macOS.** Deferred (needs a per-account `podman machine`,
  awkward for a headless service). First cut: macOS workers are Nix-only
  and untagged for `podman`. Revisit if a podman-on-mac use case appears.
- **State/log dir convention.** `/usr/local/var/...` vs
  `/Library/Application Support/opp_ci` vs mirroring Linux's
  `/var/lib`+`/var/log` (which work via `/private/var` but are
  non-idiomatic). Plan picks `/usr/local/var/...`; confirm it suits the
  fleet's other macOS tooling.
- **UID allocation.** Plan scans for a free UID < 500 and sets
  `IsHidden=1`. Confirm there's no fleet-wide convention for service
  account UIDs to align with.
