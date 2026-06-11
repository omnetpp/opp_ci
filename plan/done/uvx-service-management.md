# Plan: uvx-based service execution + in-CLI service management

Goal: stop shipping opp_ci to a host by cloning the repo and building a
venv under `/opt/opp_ci`. Instead, run opp_ci straight from GitHub with
`uvx`, re-fetching the latest code on **every (re)start**, and fold all
service lifecycle management into the CLI as
`opp_ci serve service …` and `opp_ci worker service …` subcommands that
do the right thing per OS (systemd on Linux, launchd on macOS, and a
generated NixOS module on NixOS — see §5.3).

This replaces the shell installers in
[`packaging/systemd/`](../../packaging/systemd/) and
[`packaging/launchd/`](../../packaging/launchd/) — the unit/plist files
become CLI-generated, and the `install.sh`/`uninstall.sh` scripts go
away.

Target operator UX:

```bash
# Coordinator host (Linux):
uvx opp_ci serve service install --host 0.0.0.0 --port 8080

# Worker host (Linux or macOS):
uvx opp_ci worker service install --name builder-1 \
    --coordinator https://ci.example.org --token <token>
```

---

## 1. uvx execution model

### 1.1 The command baked into each unit

The systemd unit / launchd wrapper runs opp_ci through `uvx`, pinned to a
GitHub ref and forced to re-resolve that ref on each start:

```
<uvx> --from "opp_ci[<extras>] @ git+https://github.com/omnetpp/opp_ci.git@<ref>" \
      --with "opp_repl[all] @ git+https://github.com/omnetpp/opp_repl.git@opp_ci" \
      --refresh-package opp_ci --refresh-package opp_repl \
      opp_ci <serve | worker start>
```

- `<extras>` is **role-determined**:
  - serve  → `opp_ci[web,postgres,client,podman]`
  - worker → `opp_ci[client,podman]`
- `<ref>` is the opp_ci GitHub ref, default `main`, set by `--ref` at
  install time.
- `opp_repl` is pulled from its **`opp_ci` branch** (not on PyPI). It is
  a hard dependency of opp_ci, so it must be supplied with `--with`; the
  `[all]` extra pulls every optional group the worker exercises.
- `--refresh-package opp_ci opp_repl` is **the mechanism that delivers
  "latest each restart"**. Plain `uvx --from "… @ git+…@main"` caches the
  first resolved commit of a branch and reuses it indefinitely (see the
  [uv tools docs](https://docs.astral.sh/uv/concepts/tools/): the cached
  environment is reused "unless a different version is requested, the
  cache is pruned, or the cache is refreshed"). Scoping the refresh to
  these two packages re-resolves/re-fetches them from GitHub on every
  start without re-downloading every unrelated wheel.

The `opp_ci serve` / `opp_ci worker start` command line carries **no
runtime options** — all runtime config comes from env files (§4).

### 1.2 opp_env as a self-contained uvx tool

`opp_env` is a separate command-line tool the worker shells out to on the
host-nix path. Rather than bundling it into the opp_ci environment, the
worker invokes it through **its own** uvx tool so it gets an isolated
venv:

```
uvx --from opp-env opp_env <install|run> …
```

(`opp-env` is published on PyPI, so no git ref is needed.) This is
plumbed via a new config knob `OPP_CI_OPP_ENV_CMD` (default `opp_env`):

- `opp_ci/config.py` adds `OPP_CI_OPP_ENV_CMD`.
- `opp_ci/executor.py` builds the two host-nix invocations from it
  (`shlex.split(OPP_CI_OPP_ENV_CMD) + [...]`):
  - the `opp_env install --init <project>` call
    (currently `["opp_env", "install", "--init", effective_project]`),
  - the `opp_env run <project> -c <inner>` call
    (currently `["opp_env", "run", effective_project, "-c", inner]`),
  - and the matching `recorder.begin(..., command=...)` strings.
- `worker service install` writes
  `OPP_CI_OPP_ENV_CMD="uvx --from opp-env opp_env"` into the worker env
  file.

No per-call `--refresh` for opp_env: it is invoked many times per job, so
the goal here is isolation/own-venv, not freshness-per-call. The first
call resolves the latest opp-env and caches it.

The **podman** path bakes opp_env into the runner image (via a git pin
resolved at image-build time) and is unchanged by this plan.

---

## 2. CLI surface

Two parallel `service` subgroups, one under `serve` and one under
`worker`, each with the same six lifecycle commands:

```
opp_ci serve  service {install|uninstall|start|stop|restart|status}
opp_ci worker service {install|uninstall|start|stop|restart|status}
```

### 2.1 `serve` becomes a group

`serve` is currently a plain command. It becomes a
`@click.group(invoke_without_command=True)` so that:

- `opp_ci serve` (no subcommand) still starts the web server, exactly as
  today — the existing `serve` body moves into the group callback, gated
  on `ctx.invoked_subcommand is None`, and keeps its `--host/--port/
  --cert/--key` options and the `_refuse_remote("serve")` behaviour.
- `opp_ci serve service …` dispatches to the new subgroup.

`worker` is already a group, so `opp_ci worker service …` slots in
alongside `worker start`, `worker register`, etc.

`serve service …` is **Linux-only** (this includes NixOS, which is
Linux); on macOS it exits with a message that the coordinator runs on
Linux (macOS packaging is worker-only).

### 2.2 `install` options

`install` mirrors the options of the underlying run command, plus
service-specific flags. Runtime options are persisted to env files
(§4); the rest shape the unit.

`serve service install`:

| Option | Effect |
|---|---|
| `--host`, `--port`, `--cert`, `--key` | → `serve.env` (`OPP_CI_SERVE_HOST`/`PORT`/`TLS_CERT_FILE`/`TLS_KEY_FILE`) |
| `--user` (default `opp_ci`) | run-as user; `User=` in the unit |
| `--ref` (default `main`) | opp_ci GitHub ref in the uvx command |
| `--no-postgres` | skip local PostgreSQL provisioning (remote DB) |
| `--tls` | also install the cert-watch auto-reload units (off by default) |
| `--no-enable`, `--no-start` | don't enable-on-boot / don't start now |
| `--dry-run` | render + print all artifacts and commands, change nothing |

`worker service install`:

| Option | Effect |
|---|---|
| `--name` (default `default`) | worker instance → `opp_ci-worker@<name>.service` + `workers/<name>.env` |
| `--coordinator`, `--token`, `--poll-interval`, `--heartbeat-interval`, `--niceness` | → `workers/<name>.env` (see §4) |
| `--user` (default `opp_ci`) | run-as user |
| `--ref` (default `main`) | opp_ci GitHub ref |
| `--no-enable`, `--no-start`, `--dry-run` | as above |

### 2.3 install lifecycle behaviour

- By default `install` **enables-on-boot and starts** the service
  (systemd `enable --now`; launchd `bootstrap` + `RunAtLoad=true`).
  `--no-enable` / `--no-start` opt out.
- **Worker guard:** if no token is provided *and* none is already in the
  env file, auto-start is skipped with a message telling the operator to
  set the token then run `worker service start --name <name>`.
- **NixOS:** `install` / `uninstall` are **render-only** (never mutate
  the system, even as root) — they emit a NixOS module + instructions
  instead of enabling/starting units. `--no-enable` / `--no-start` /
  auto-start are no-ops there (boot enablement is declared in the
  module). See §5.3.

### 2.4 `uninstall` semantics (conservative)

- Default: stop + disable, remove the unit/plist files (plus the
  launchd wrapper + newsyslog drop-in), `daemon-reload`. **Preserves**
  `/etc/opp_ci/` config + env files (tokens), `/var/lib/opp_ci/` state,
  the database, and the service user.
- `--purge`: additionally removes config + env files + state, **after a
  confirmation prompt**. Never drops the PostgreSQL database or deletes
  the user unless explicitly confirmed again.
- `worker service uninstall --name X` removes only that instance (its
  plist/env on macOS, its enablement + env file on Linux). The shared
  `opp_ci-worker@.service` template and `opp_ci.target` are kept while
  any other worker remains.
- **On NixOS** `uninstall` mutates nothing: it renders instructions to
  drop the module import / flake reference and `nixos-rebuild switch`
  (and, with `--purge`, the imperative env/state paths to delete by
  hand). See §5.3.

---

## 3. uv / uvx provisioning (copy from the invoking user)

`service install` does **not** install uv. It requires uv/uvx to be
present for the **invoking** user and copies them to the service user so
the same version is available to the daemon:

- Locate `uv` and `uvx` via `shutil.which` for the invoking user.
- Copy **both** binaries into the target user's conventional location
  `~<user>/.local/bin/` (e.g. `/var/lib/opp_ci/.local/bin/` for the
  default `opp_ci`), `chown <user>:<group>`, `chmod 0755`.
- Skip the copy when `--user` equals the invoking user (self-install).
- If `uv` or `uvx` is **not found**, print a **warning** (the service
  will not start until uv/uvx are available to the service user) and
  continue the rest of the install.
- The generated unit references the **absolute copied path**
  (`ExecStart=/var/lib/opp_ci/.local/bin/uvx …`), and
  `Environment=PATH` includes that directory so `uvx` can find `uv`.
  Override via `OPP_CI_UVX` if needed.
- uv cache lives in the target user's `~/.cache/uv`
  (HOME = `/var/lib/opp_ci`); provisioning ensures HOME is writable.

**NixOS exception:** the binary copy does **not** apply — copied
dynamically-linked `uv`/`uvx` binaries won't run on a non-FHS system.
There the generated module puts `pkgs.uv` (nixpkgs' uv is patched for
NixOS) on the service `path`, so `uvx`/`uv` resolve from the Nix store
and no copy/`OPP_CI_UVX` override is needed. See §5.3.

---

## 4. Configuration: env-file-only

Runtime options are written to env files; the command line stays
generic. The config layer already overlays `/etc/opp_ci/opp_ci.env`
(see `opp_ci/config.py`), and systemd `EnvironmentFile=` / the launchd
wrapper source these files.

| File | Read by | Mode | Keys written by install |
|---|---|---|---|
| `/etc/opp_ci/opp_ci.env` | both | 0640 root:opp_ci | `OPP_CI_DATABASE_URL` (postgres provisioning) |
| `/etc/opp_ci/serve.env` | serve | 0640 root:opp_ci | `OPP_CI_SERVE_HOST`, `OPP_CI_SERVE_PORT`, `OPP_CI_SERVE_TLS_CERT_FILE`, `OPP_CI_SERVE_TLS_KEY_FILE` |
| `/etc/opp_ci/workers/<name>.env` | one worker | 0600 opp_ci:opp_ci | `OPP_CI_COORDINATOR_URL`, `OPP_CI_WORKER_TOKEN`, `OPP_CI_WORKER_POLL_INTERVAL`, `OPP_CI_WORKER_HEARTBEAT_INTERVAL`, `OPP_CI_WORKER_NICENESS`, `OPP_CI_OPP_ENV_CMD` |

Option → env var mapping:

- serve: `--host→OPP_CI_SERVE_HOST`, `--port→OPP_CI_SERVE_PORT`,
  `--cert→OPP_CI_SERVE_TLS_CERT_FILE`, `--key→OPP_CI_SERVE_TLS_KEY_FILE`
- worker: `--coordinator→OPP_CI_COORDINATOR_URL`,
  `--token→OPP_CI_WORKER_TOKEN`,
  `--poll-interval→OPP_CI_WORKER_POLL_INTERVAL`,
  `--heartbeat-interval→OPP_CI_WORKER_HEARTBEAT_INTERVAL`,
  `--niceness→OPP_CI_WORKER_NICENESS`

New config vars in `opp_ci/config.py`:

- `OPP_CI_WORKER_NICENESS` (default `10`) — `worker start --niceness`
  default now reads this so it is expressible via the env file.
- `OPP_CI_OPP_ENV_CMD` (default `opp_env`) — see §1.2.
- `OPP_CI_UVX` (optional) — override the absolute uvx path in the unit.

Baked into the unit's `ExecStart` (not env files): `--ref` (the
`@<ref>`), `--user` (`User=`), and the role-determined extras.

Worker registration is **not** done by `install` — it consumes a token
only. `opp_ci worker register` stays a separate step on/against the
coordinator; docs show the two-step recipe.

---

## 5. Unit / plist model

### 5.1 systemd (Linux)

Keep the existing names so the web UI log viewer
(`OPP_CI_SERVE_UNIT` = `opp_ci-serve.service`,
`OPP_CI_WORKER_UNIT_TEMPLATE` = `opp_ci-worker@{instance}.service` in
`config.py`) keeps working unchanged:

- `opp_ci-serve.service` — singleton; `ExecStart` = the uvx serve
  command; `EnvironmentFile=/etc/opp_ci/opp_ci.env` +
  `-/etc/opp_ci/serve.env`; `Environment=PATH=…:<user>/.local/bin:…`.
- `opp_ci-worker@.service` — template; one instance per worker name;
  `EnvironmentFile=/etc/opp_ci/opp_ci.env` +
  `/etc/opp_ci/workers/%i.env`.
- `opp_ci.target` — umbrella; generated once.
- Optional (`serve service install --tls`): `opp_ci-serve-cert.path` +
  `opp_ci-serve-cert-reload.service` for cert-renewal auto-reload.

`SupplementaryGroups=systemd-journal` stays on serve for the log viewer.

### 5.2 launchd (macOS, worker-only)

- One plist per worker name:
  `/Library/LaunchDaemons/org.omnetpp.opp_ci.worker.<name>.plist`.
- A CLI-generated env-sourcing **wrapper** (launchd can't source env
  files): it does `set -a; . /etc/opp_ci/opp_ci.env;
  . /etc/opp_ci/workers/<name>.env; set +a; exec <abs-uvx> --from … opp_ci
  worker start`. The token stays in the 0600 env file, out of the
  world-readable plist.
- A CLI-generated newsyslog drop-in for log rotation (launchd has no
  journald; stdout/stderr go to `/usr/local/var/log/opp_ci/`).
- `worker service {start,stop,restart,status}` drive `launchctl`
  per-name directly. The old `opp_ci-workers` umbrella helper is dropped
  (the CLI subsumes it).

### 5.3 NixOS (declarative module)

NixOS owns systemd units, users, and packages declaratively from the
system configuration. Imperative writes to `/etc/systemd/system`,
`useradd`, copying binaries into a home dir, `systemctl enable`, and
imperative PostgreSQL/podman setup either don't survive `nixos-rebuild`
or fight the Nix model. So on NixOS the `service` commands split:

- **Detection** (precedence over the generic systemd-Linux path):
  `/etc/NIXOS` exists, or `/run/current-system/nixos-version` exists, or
  `ID=nixos` in `/etc/os-release`.
- **`install` / `uninstall` are render-only** — they make **no** system
  mutation, even as root (same discipline as the §7 no-sudo path), and
  emit a **NixOS module** + apply instructions. The operator imports the
  module and applies it with `sudo nixos-rebuild switch`.
- **`start` / `stop` / `restart` / `status` work normally** via
  `systemctl`: once the module is applied, the units
  `opp_ci-serve.service` / `opp_ci-worker@<name>.service` exist exactly
  as on a generic systemd host, so these lifecycle commands and the web
  UI log viewer (`OPP_CI_SERVE_UNIT` / `OPP_CI_WORKER_UNIT_TEMPLATE`) are
  unchanged.

What `install` emits on NixOS (to stdout; written to `--out DIR` when
given):

1. **A standalone module file** `opp_ci.nix` declaring everything the
   imperative installer would have done:
   - `users.users.<user>` / `users.groups.<user>` — system account,
     home `/var/lib/opp_ci`.
   - `systemd.services."opp_ci-serve"` (serve) or
     `systemd.services."opp_ci-worker@"` (worker template) with the same
     uvx `ExecStart` (§1.1), `serviceConfig.User`, the
     `EnvironmentFile=` references (§4), and
     `path = [ cfg.uvPackage ]` so `uvx`/`uv` resolve from the store.
   - `systemd.tmpfiles.rules` for `/etc/opp_ci`, `/etc/opp_ci/workers`,
     the TLS dir, and a writable HOME for the uv cache.
   - serve only: a `postgres.enable` option (default true, the
     `--no-postgres` analogue) that turns on declarative PostgreSQL
     (`services.postgresql.{enable,ensureDatabases,ensureUsers}`).
   - `uvPackage` option, default `pkgs.uv` — **replaces the §3 binary
     copy** (which can't run on a non-FHS host).
   - module options stay minimal and parity with §4's *env-file-only*
     rule: `enable`, `ref`, `user`, `uvPackage`, `postgres.enable`,
     `tls.enable`, and the `environmentFile` path(s). All runtime config
     (`--host`, `--coordinator`, …) lands in the rendered env-file bodies
     (item 3), not in Nix options.
2. **A flake** `flake.nix` exposing
   `nixosModules.{opp_ci-serve, opp_ci-worker, default}`, for
   flake-based configs (module delivery = both: bare module *and* flake).
3. **The env-file bodies** (§4) rendered separately for the operator to
   write **imperatively** at `/etc/opp_ci/…` (0640/0600). Secrets
   (`OPP_CI_WORKER_TOKEN`, `OPP_CI_DATABASE_URL`) must **not** enter the
   Nix store, which is world-readable; the module only
   `EnvironmentFile=`-references those paths.
4. **An apply block**: where to drop `opp_ci.nix`, the
   `imports = [ ./opp_ci.nix ];` line (or the flake input +
   `nixosModules.…`), the `services.opp_ci.serve.enable = true;` toggle,
   any `--ref` / extras overrides, then `sudo nixos-rebuild switch`.

**Secrets** — document both the plain imperative env file *and*
**sops-nix / agenix**: show the module consuming
`config.sops.secrets."opp_ci/worker-token".path` (or the agenix
equivalent) as its `EnvironmentFile`, so managed-secret users never
hand-write env files.

**Freshness still works:** the uvx `ExecStart` (with
`--refresh-package`) is baked into the store at rebuild time, but it is
the *same* refreshing command, so each service (re)start still re-fetches
the latest `@<ref>` code. Only changing `--ref`/extras/`uvPackage`
requires a `nixos-rebuild`.

`uninstall` on NixOS renders instructions to remove the module import /
flake reference and `nixos-rebuild switch`; with `--purge` it also lists
the imperative env/state paths to delete. No `systemctl disable` (boot
enablement is owned by the module).

---

## 6. Provisioning (role-scoped, full)

Ported from the current `install.sh` scripts, split by role.

Shared (both roles):

- Create `--user`/group (default `opp_ci`): `useradd --system` on Linux,
  `dscl` hidden role account on macOS.
- Create dirs: `/etc/opp_ci`, `/etc/opp_ci/workers`, `/var/lib/opp_ci`
  (+ `/usr/local/var/opp_ci`, `/usr/local/var/log/opp_ci` on macOS), TLS
  dir; correct owners/modes.
- Seed `/etc/opp_ci/opp_ci.env` (only if missing).
- Ensure the target user's HOME exists and is writable (uv cache).
- Copy uv/uvx (§3).

`serve service install` (Linux) adds:

- PostgreSQL provisioning (install if missing, create role + db, grant
  `ALL ON SCHEMA public`, detect port, append `OPP_CI_DATABASE_URL` to
  `opp_ci.env` if unset) unless `--no-postgres`.
- `serve.env` seed; TLS aux when `--tls`.

`worker service install` adds:

- Rootless podman: subuid/subgid allocation, `loginctl enable-linger`,
  `podman system migrate` (Linux).
- `workers/<name>.env` seed.

**On NixOS** none of these imperative steps run. The equivalents become
**declarative** in the generated module (§5.3): the user/group, dirs
(`systemd.tmpfiles`), PostgreSQL (`services.postgresql.*`), and
rootless-podman enablement (`virtualisation.podman` + linger /
subuid-subgid) are expressed as Nix options and applied by
`nixos-rebuild`. The installer only *renders* them; it does not run
`useradd`/`podman`/`pg_*`.

---

## 7. No-sudo fallback

System units (and copying into another user's home) need root. Each
`service` operation does a single **up-front** privilege check
(`os.geteuid() == 0`):

- If privileged: perform the operation.
- If **not** privileged: make **no** mutations. Render a complete,
  copy-pasteable "manual install/uninstall" transcript and exit
  non-zero. The transcript lists, in order:
  - every file to create — full path, owner, mode, and **exact
    contents**;
  - the uv/uvx copy (source → dest, `chown`/`chmod`);
  - user/dir creation commands;
  - the `systemctl` / `launchctl` commands to enable/start.

The same renderer powers `--dry-run`, which prints the transcript even
when run as root (so operators can review before applying).

**NixOS reuses this render-only discipline** but with a different
artifact: instead of a shell transcript it emits the NixOS module +
flake + env-file bodies + apply instructions (§5.3). On NixOS the
render-only path is taken **regardless of privilege** (declarative
config, not euid, is the gate).

---

## 8. Packaging: embed everything in the wheel

Because the installer runs from a `uvx`-installed wheel, it has **no
source checkout** to copy from (`packaging/` is not part of the Python
package). All artifacts the installer writes must be embedded in the
package:

- Unit text, `opp_ci.target`, the worker template, the launchd plist,
  the launchd wrapper, the newsyslog drop-in, the env-file seed
  contents, **and the NixOS `opp_ci.nix` module + `flake.nix`** → string
  templates in `opp_ci/service.py`.
- `tls.conf` example content embedded; `opp_ci-serve-cert.path` +
  `opp_ci-serve-cert-reload.service` generated from templates.
- `cloudflare-origin-ca.pem` shipped as `package-data`.

Then:

- Delete `packaging/systemd/` and `packaging/launchd/` entirely.
- Update `pyproject.toml` `[tool.setuptools.package-data]` to include
  the embedded data files (e.g. the Cloudflare CA pem).

---

## 9. Migration from the venv-based install

A re-install transparently migrates an existing host:

- `service install` overwrites the same-named unit files (now pointing
  at uvx) and `daemon-reload`s; the next restart runs the uvx model.
- It does **not** delete `/opt/opp_ci`, `/opt/opp_env`, `/opt/opp_repl`,
  or the `opp_ci` user's `.profile`/`setenv` shim (operator-owned /
  possibly in use). Instead it prints a note listing those now-unused
  paths for manual removal.
- Docs get a short "Migrating from the venv-based install" section.

---

## 10. Implementation steps

1. **`opp_ci/config.py`** — add `OPP_CI_WORKER_NICENESS`,
   `OPP_CI_OPP_ENV_CMD`, `OPP_CI_UVX`.
2. **`opp_ci/executor.py`** — route the two host-nix `opp_env`
   invocations and their recorder `command=` strings through
   `OPP_CI_OPP_ENV_CMD` via `shlex.split`.
3. **`opp_ci/service.py`** (new) —
   - pure renderers: serve unit, worker template, target, plist,
     launchd wrapper, newsyslog, env-file bodies, TLS aux units, the
     unprivileged "manual recipe" transcript, **and the NixOS module +
     flake + apply instructions** (§5.3);
   - thin side-effecting apply layer: file writes, uv/uvx copy,
     user/dir/postgres/podman provisioning, `systemctl`/`launchctl`
     calls;
   - OS dispatch (systemd vs launchd vs **NixOS render-only**) with a
     NixOS detector, and the up-front privilege check (skipped on NixOS,
     which is render-only regardless of euid).
4. **`opp_ci/cli.py`** — convert `serve` to
   `group(invoke_without_command=True)` preserving current behaviour;
   add `serve service` and `worker service` subgroups with
   `install/uninstall/start/stop/restart/status`; wire `--niceness`
   default to `OPP_CI_WORKER_NICENESS`.
5. **`pyproject.toml`** — add the embedded data (Cloudflare CA pem) to
   `package-data`.
6. **Delete** `packaging/systemd/` and `packaging/launchd/`.
7. **Docs** — rewrite `doc/systemd.md` and `doc/launchd.md` around the
   uvx + `service` model; add `doc/nixos.md` (module + flake usage,
   sops-nix/agenix secrets, `nixos-rebuild` apply flow); update
   `doc/workers.md` cross-references; add the migration section.
8. **`tests/test_service.py`** (new) — renderer assertions, including
   the NixOS module/flake render and the NixOS detector branch.

---

## 11. Verification

- `python -m unittest tests.test_service tests.test_log_pages`
  - `test_service`: the embedded uvx command carries the right extras,
    `@<ref>`, and `--refresh-package opp_ci opp_repl`; option→env-var
    mapping is correct; serve unit / worker template / target / plist /
    wrapper / newsyslog / env files render exactly; the unprivileged
    manual recipe lists files+contents+commands; the opp_env launcher is
    written into the worker env; **the NixOS module/flake render exactly
    (right `ExecStart`, `EnvironmentFile=` references, `pkgs.uv` on
    `path`, no secrets in the module) and the NixOS detector picks the
    render-only branch even as root.**
  - `test_log_pages`: stays green (unit names unchanged).
- Manual `--dry-run` smoke on a Linux (systemd) host and a macOS
  (launchd) host: review the rendered artifacts, confirm
  `serve service` refuses on macOS, confirm the no-sudo transcript when
  run unprivileged.
- Manual smoke on a **NixOS** host: `install` emits module + flake +
  env-file bodies + apply instructions and mutates nothing; after
  `nixos-rebuild switch`, `start/stop/status` drive the units via
  `systemctl` and the log viewer works.

---

## 12. Risks / considerations

- **Network on every restart** — `--refresh-package` re-hits GitHub at
  each (re)start; first start is slow (resolve + build). This is the
  explicit intent; a pinned `--ref` (e.g. a tag) bounds surprise
  upgrades.
- **uv must reach the service user** — if the copy step warned (uv/uvx
  missing for the invoking user), the daemon won't start until uv/uvx
  are present at the configured path.
- **`opp_repl` branch coupling** — pulled from the `opp_ci` branch; if
  that branch is renamed/removed the units stop resolving.
- **Build dependencies at runtime** — building opp_ci/opp_repl from a
  git source on a minimal host may need build tooling; uv handles wheels
  where available.
- **NixOS + uv-fetched binaries** — `pkgs.uv` runs fine, but wheels uv
  downloads (or builds) with native deps are non-FHS binaries that may
  fail to load on NixOS without `programs.nix-ld` (or an FHS wrapper).
  The module should document enabling `nix-ld`; worst case a fully
  Nix-packaged opp_ci would be needed (out of scope here).
- **NixOS upgrade cadence** — `--ref`/extras live in the store and only
  change on `nixos-rebuild`; per-restart freshness still works via
  `--refresh-package`, but operators expecting a pure imperative
  `install` to flip the ref must instead re-render + rebuild.
- **Secrets out of the store** — the module must only *reference* env
  files / sops-agenix secrets; a regression that inlined a token into
  `opp_ci.nix` would leak it world-readable via the Nix store.
