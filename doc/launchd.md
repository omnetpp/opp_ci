# Running an opp_ci worker as a launchd service (macOS)

macOS has no systemd, so a macOS worker runs as a launchd **LaunchDaemon**
managed from the CLI. This is the macOS analogue of the systemd worker
([systemd.md](systemd.md)) and is **worker-only** — the coordinator
runs on Linux. `opp_ci coordinator service …` refuses on macOS.

Like the Linux path, the worker runs from GitHub via `uvx`, re-fetching
the pinned ref on every (re)start. See [systemd.md](systemd.md#how-it-runs-uvx)
for the uvx model.

## Prerequisites

`uv`/`uvx` must be installed for the **invoking** user; `service install`
copies them into the service user's `~/.local/bin/`. The worker also needs
network access to the coordinator and a working `opp_env`/Nix toolchain for
host-nix jobs.

## Install

```bash
# 1. Register the worker on the coordinator (mint a token). --auto-tags
#    reads /etc/os-release, which is Linux-only, so pass explicit tags:
opp_ci worker register --remote https://ci.example.org --name mac-1 \
    --tags os:macos,os:macos-15,arch:arm64
# 2. Install + start the worker LaunchDaemon with that token:
sudo uvx opp_ci worker service install --name mac-1 \
    --coordinator https://ci.example.org --token <token>
```

`install` creates the hidden `opp_ci` role account, the config/state/log
dirs, the per-worker plist, the env-sourcing wrapper, and a newsyslog
drop-in for log rotation; then (unless `--no-start`) bootstraps the
daemon (`RunAtLoad=true`).

Options match the Linux worker (`--name`, `--coordinator`, `--token`,
`--poll-interval`, `--heartbeat-interval`, `--niceness`, `--user`,
`--ref`, `--no-enable`, `--no-start`, `--dry-run`).

## Artifacts

| Path | Purpose |
|---|---|
| `/Library/LaunchDaemons/org.omnetpp.opp_ci.worker.<name>.plist` | the LaunchDaemon (one per worker name) |
| `<home>/.local/bin/opp_ci-worker-run` | env-sourcing wrapper invoked by the plist |
| `/etc/opp_ci/opp_ci.env`, `/etc/opp_ci/workers/<name>.env` | config (token in the 0600 per-worker file) |
| `/etc/newsyslog.d/opp_ci.conf` | log rotation for `/usr/local/var/log/opp_ci/worker-*.log` |

launchd can't source env files, so the plist runs a CLI-generated
**wrapper** that does `set -a; . opp_ci.env; . workers/<name>.env; set +a;
exec <uvx> … opp_ci worker start`. The token stays in the 0600 env file,
out of the world-readable plist.

## Lifecycle

```bash
sudo opp_ci worker service {start|stop|restart|status} --name <name>
```

These drive `launchctl` (`bootstrap` / `bootout` / `kickstart -k` /
`print`) per worker. Logs:
`tail -f /usr/local/var/log/opp_ci/worker-<name>.log`.

## Uninstall

```bash
sudo opp_ci worker service uninstall --name <name>
```

Removes that worker's plist and `bootout`s it; preserves config + state.
`--purge` also removes the per-worker env file (the token) after
confirmation.

## No-sudo / dry-run

As on Linux, run unprivileged or pass `--dry-run` and the CLI mutates
nothing — it prints the full manual transcript (files + contents +
`launchctl` commands). See [systemd.md](systemd.md#no-sudo--dry-run).

## Notes

- Podman on macOS runs in a per-user VM and is out of scope for headless
  workers; omit a `podman` tag and run host-nix jobs only.
- `HOME` must be a real writable dir (the plist sets it to the service
  user's state dir) so `opp_env`/`~/.nix-profile` and the uv cache work.
