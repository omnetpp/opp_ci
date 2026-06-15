# Running opp_ci on NixOS

On NixOS, opp_ci is configured **fully declaratively** in your
`configuration.nix`. Every runtime parameter is a NixOS module option;
there are **no imperative config files** to hand-write. The only thing
that can't live in the Nix config is secret *values* (the Nix store is
world-readable) — those are delivered by file path via `environmentFiles`
and managed with sops-nix / agenix, so even they end up declared in your
config.

Like other platforms, the service runs from GitHub via `uvx`, re-fetching
the pinned ref on every restart (see
[systemd.md](systemd.md#how-it-runs-uvx)).

## The modules

opp_ci ships two NixOS modules:

- `services.opp_ci.coordinator` — the web UI + API + scheduler →
  `opp_ci-coordinator.service`.
- `services.opp_ci.worker` — one or more worker instances, each →
  `opp_ci-worker-<name>.service`.

Get the module files with the CLI (it renders nothing imperative on
NixOS — it just copies the modules out and prints an example):

```bash
opp_ci coordinator service install --host 0.0.0.0 --out /etc/nixos/opp_ci
opp_ci worker      service install --name builder-1 \
    --coordinator https://ci.example.org --out /etc/nixos/opp_ci
```

The `--out` dir gets `lib.nix`, `coordinator.nix`, `worker.nix`,
`flake.nix`, a `configuration-example.nix` built from your flags, and
`APPLY.txt`. Detection is automatic (`/etc/NIXOS`,
`/run/current-system/nixos-version`, or `ID=nixos`); the render-only path
is taken regardless of privilege.

## Coordinator

```nix
{ config, ... }:
{
  imports = [ ./opp_ci/coordinator.nix ./opp_ci/worker.nix ];

  services.opp_ci.coordinator = {
    enable = true;
    host = "0.0.0.0";
    port = 8080;
    ref = "main";                       # or a tag to bound upgrades
    publicUrl = "https://ci.example.org";
    github.org = "omnetpp";
    github.oauthClientId = "Iv1.abc123";
    # Secrets by path (see "Secrets" below):
    environmentFiles = [ config.age.secrets.opp_ci-coord.path ];
  };
}
```

Common options (each maps to an `OPP_CI_*` env var on the unit):
`enable`, `ref`, `user`, `uvPackage`, `host`, `port`, `publicUrl`,
`postgres.enable` (default true → declarative `services.postgresql`),
`tls.{enable,certFile,keyFile,keyPasswordFile}`,
`github.{org,oauthClientId,submitterTeams}`, and the raw-value secret
paths `github.{tokenFile,oauthClientSecretFile,actionsTokenFile}`.

Everything the named options don't cover is reachable through the
freeform `settings` attrset (the long tail of ~50 `OPP_CI_*` vars):

```nix
services.opp_ci.coordinator.settings = {
  OPP_CI_WORKER_HEARTBEAT_TIMEOUT = 180;
  OPP_CI_GITHUB_ALLOW_EXTERNAL = false;     # bool → "0"
  OPP_CI_GITHUB_ADMIN_USERS = "alice,bob";
};
```

`settings` is merged into the unit's `environment` **after** the typed
options, so it can override them. Put **non-secret** values only here —
anything in `settings` lands in the world-readable Nix store.

## Workers

Each worker is a named instance; the module creates one
`opp_ci-worker-<name>.service` per entry:

```nix
{ config, ... }:
{
  imports = [ ./opp_ci/coordinator.nix ./opp_ci/worker.nix ];

  services.opp_ci.worker = {
    ref = "main";
    instances.builder-1 = {
      coordinatorUrl = "https://ci.example.org";
      niceness = 5;
      environmentFiles = [ config.age.secrets.opp_ci-builder-1.path ];
    };
    instances.nix-1 = {
      coordinatorUrl = "https://ci.example.org";
      pollInterval = 5;
      environmentFiles = [ config.age.secrets.opp_ci-nix-1.path ];
    };
  };
}
```

Per-instance options: `enable` (default true), `coordinatorUrl`,
`pollInterval`, `heartbeatInterval`, `niceness`, `oppEnvCmd` (defaults to
`opp_env` — the bundled console script of the `opp_env` supplied to the uvx
env at its `opp_ci` branch), `settings`, and `environmentFiles` (carries
the worker token). The worker module enables rootless podman
(`virtualisation.podman.enable`).

**Log viewer coupling:** workers are named `opp_ci-worker-<name>.service`,
so the coordinator module sets
`OPP_CI_WORKER_UNIT_TEMPLATE = "opp_ci-worker-{instance}.service"` by
default, which is what the web UI's Logs pages use to find each worker's
journal. Override it via `coordinator.settings` only if you rename the
units.

## Secrets (sops-nix / agenix)

Secret *values* cannot be declarative — the Nix store is world-readable.
Each module takes an `environmentFiles` list of **paths** to KEY=value
files that systemd reads via `EnvironmentFile=`. Manage those files with
sops-nix or agenix so the *encrypted* secret is what lives in your repo.

Coordinator secret file (`OPP_CI_*` lines):

```
OPP_CI_DATABASE_URL=postgresql:///opp_ci?host=/run/postgresql
OPP_CI_SESSION_SECRET=<random>
OPP_CI_GITHUB_WEBHOOK_SECRET=<webhook secret>
```

Worker secret file:

```
OPP_CI_WORKER_TOKEN=<token from `opp_ci worker register`>
```

Wire it with agenix:

```nix
age.secrets.opp_ci-coord.file  = ../secrets/opp_ci-coord.age;
age.secrets.opp_ci-coord.owner = "opp_ci";

services.opp_ci.coordinator.environmentFiles =
  [ config.age.secrets.opp_ci-coord.path ];
```

or sops-nix (`config.sops.secrets."opp_ci-coord".path`) — the option takes
a path, so both backends drop in unchanged. The four GitHub/TLS secrets
the app reads from a *raw-value* file
(`github.tokenFile`, `github.oauthClientSecretFile`,
`github.actionsTokenFile`, `tls.keyPasswordFile`) take a path to a file
containing just the secret (no `KEY=` wrapping).

## Flake usage

`flake.nix` exposes `nixosModules.{coordinator, worker, default}`:

```nix
{
  inputs.opp_ci.url = "path:/etc/nixos/opp_ci";   # or a git URL
  outputs = { nixpkgs, opp_ci, ... }: {
    nixosConfigurations.ci = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [ opp_ci.nixosModules.default ./configuration.nix ];
    };
  };
}
```

## Apply

1. Put the `*.nix` module files under `/etc/nixos/opp_ci/`.
2. Merge the example into `configuration.nix` (imports + the option block).
3. Create the secret files with sops-nix / agenix and reference them via
   `environmentFiles`.
4. `sudo nixos-rebuild switch`.

Lifecycle afterwards is normal: `opp_ci coordinator service
{start,stop,restart,status}` and `opp_ci worker service … --name <name>`
drive `systemctl` against the units the module created, and the web UI log
viewer works unchanged.

## Uninstall

`opp_ci … service uninstall` on NixOS mutates nothing: remove the module
import / option block from `configuration.nix` and run
`nixos-rebuild switch` (boot enablement is owned by the module, so there is
no `systemctl disable` step). Delete the secret files separately if you
want them gone.

## Freshness & caveats

- The uvx `ExecStart` (with `--refresh-package`) is baked into the store at
  rebuild time, but it is the *same* refreshing command, so each
  (re)start re-fetches the latest `@<ref>` code. Only changing
  `ref`/`uvPackage` (or any option) needs a `nixos-rebuild`.
- Wheels uv downloads with native deps are non-FHS binaries that may fail
  to load without `programs.nix-ld.enable = true;` — enable nix-ld if a job
  fails with a loader error.
- Never put a secret *value* in a module option or `settings` — it would
  land in the world-readable Nix store. Use `environmentFiles` / the
  `*File` options.
