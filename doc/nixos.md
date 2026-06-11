# Running opp_ci on NixOS

NixOS owns systemd units, users, and packages **declaratively** from the
system configuration. Imperative writes to `/etc/systemd/system`,
`useradd`, copying binaries into a home dir, `systemctl enable`, and
imperative PostgreSQL/podman setup either don't survive `nixos-rebuild`
or fight the Nix model. So on NixOS the `service` commands split:

- **`install` / `uninstall` are render-only** — they make **no** system
  mutation (even as root) and emit a **NixOS module** + flake + env-file
  bodies + apply instructions. You import the module and apply it with
  `sudo nixos-rebuild switch`.
- **`start` / `stop` / `restart` / `status` work normally** via
  `systemctl` once the module is applied — the units
  `opp_ci-serve.service` / `opp_ci-worker@<name>.service` exist exactly
  as on a generic systemd host, so lifecycle commands and the web UI log
  viewer are unchanged.

NixOS is detected via `/etc/NIXOS`, `/run/current-system/nixos-version`,
or `ID=nixos` in `/etc/os-release` (any one). Detection takes precedence
over the generic systemd path, and the render-only path is taken
**regardless of privilege**.

## Render the module

```bash
opp_ci serve  service install --host 0.0.0.0 --port 8080 --out ./opp_ci-nix
opp_ci worker service install --name builder-1 \
    --coordinator https://ci.example.org --token <token> --out ./opp_ci-nix
```

With `--out DIR` the artifacts are written there; without it they print
to stdout. The bundle:

| File | Purpose |
|---|---|
| `opp_ci.nix` | standalone NixOS module (user/group, the systemd service with the uvx `ExecStart`, `systemd.tmpfiles` dirs, declarative PostgreSQL for serve, `pkgs.uv` on the unit `path`) |
| `flake.nix` | exposes `nixosModules.{opp_ci-serve, opp_ci-worker, default}` for flake-based configs |
| `opp_ci.env`, `serve.env` / `<name>.env` | env-file **bodies** to write imperatively at `/etc/opp_ci/…` |
| `APPLY.txt` | where to drop the files, the import line, the enable toggle, and the `nixos-rebuild` step |

`pkgs.uv` **replaces** the binary copy used on other distros — nixpkgs'
uv is patched for NixOS, so `uvx`/`uv` resolve from the Nix store and no
copy or `OPP_CI_UVX` override is needed.

## Apply

1. Drop `opp_ci.nix` next to your `configuration.nix` (or use the flake).
2. Write the env-file bodies imperatively (secrets stay **out** of the
   world-readable Nix store):
   - `/etc/opp_ci/opp_ci.env` (0640 root:opp_ci)
   - serve: `/etc/opp_ci/serve.env` (0640 root:opp_ci)
   - worker: `/etc/opp_ci/workers/<name>.env` (0600 opp_ci:opp_ci)
3. In `configuration.nix`:
   ```nix
   imports = [ ./opp_ci.nix ];
   services.opp_ci.serve.enable = true;     # or .worker.enable
   # services.opp_ci.serve.ref = "v1.2";    # override the pinned ref
   ```
4. `sudo nixos-rebuild switch`.

The module options stay minimal (parity with the env-file-only rule):
`enable`, `ref`, `user`, `uvPackage`, `postgres.enable`, `tls.enable`,
and the `environmentFile` path(s). All runtime config (`--host`,
`--coordinator`, …) lives in the rendered env-file bodies, not in Nix
options.

## Secrets (sops-nix / agenix)

The module only `EnvironmentFile=`-references the env files, so managed
secrets work without hand-writing them. Point the reference at a decrypted
secret path, e.g. with sops-nix:

```nix
sops.secrets."opp_ci/worker-token" = { owner = "opp_ci"; };
services.opp_ci.worker.environmentFile =
  config.sops.secrets."opp_ci/worker-token".path;
```

(or the agenix equivalent). Never inline a token into `opp_ci.nix` — the
Nix store is world-readable.

## Freshness still works

The uvx `ExecStart` (with `--refresh-package`) is baked into the store at
rebuild time, but it is the *same* refreshing command, so each service
(re)start still re-fetches the latest `@<ref>` code. Only changing
`--ref` / extras / `uvPackage` requires a `nixos-rebuild`.

## Uninstall

`uninstall` mutates nothing: it renders instructions to remove the module
import / flake reference and run `nixos-rebuild switch` (no
`systemctl disable` — boot enablement is owned by the module). With
`--purge` it also lists the imperative env/state paths to delete by hand.

## Caveats

- Wheels uv downloads with native deps are non-FHS binaries that may fail
  to load on NixOS without `programs.nix-ld.enable = true;` (or an FHS
  wrapper). Enable nix-ld if a job fails with a loader error.
- `--ref` / extras live in the store and change only on `nixos-rebuild`;
  per-restart freshness still works via `--refresh-package`.
