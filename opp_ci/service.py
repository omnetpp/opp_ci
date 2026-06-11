"""Service lifecycle management for opp_ci (serve + worker).

This module folds the old ``packaging/systemd`` and ``packaging/launchd``
shell installers into the CLI. It generates and applies the unit / plist
artifacts that run opp_ci straight from GitHub via ``uvx`` (re-fetching the
pinned ref on every restart), and drives the per-OS lifecycle commands.

It is structured in three layers:

* **Pure renderers** — functions that turn an :class:`InstallSpec` into the
  exact text of each artifact (systemd unit, worker template, target, launchd
  plist + wrapper, newsyslog drop-in, env-file bodies, TLS aux units, the
  NixOS module + flake, and the unprivileged "manual recipe" transcript). No
  side effects; everything here is unit-testable without root.
* **A thin side-effecting apply layer** — file writes, uv/uvx copy,
  user/dir/postgres/podman provisioning, and ``systemctl`` / ``launchctl``
  calls.
* **OS dispatch** — systemd (Linux) vs launchd (macOS, worker-only) vs NixOS
  (render-only), with a NixOS detector and the up-front privilege check.

See ``plan/pending/uvx-service-management.md`` for the full design.
"""

import os
import shlex
import shutil
import subprocess
import sys

from opp_ci import config as cfg


# ── Constants ─────────────────────────────────────────────────────────────

OPP_CI_GIT = "git+https://github.com/omnetpp/opp_ci.git"
OPP_REPL_GIT = "git+https://github.com/omnetpp/opp_repl.git"
OPP_REPL_REF = "opp_ci"  # opp_repl is pulled from this branch, not PyPI.

SERVE_EXTRAS = "web,postgres,client,podman"
WORKER_EXTRAS = "client,podman"

DEFAULT_USER = "opp_ci"
DEFAULT_REF = "main"

# Filesystem layout. State dir doubles as the service user's HOME (so the uv
# cache lands at $HOME/.cache/uv). macOS uses the Homebrew-style /usr/local
# tree; everything else follows the FHS /var/lib layout.
CONFIG_DIR = "/etc/opp_ci"
WORKER_CONFIG_DIR = CONFIG_DIR + "/workers"
TLS_DIR = CONFIG_DIR + "/tls"

LINUX_STATE_DIR = "/var/lib/opp_ci"
MACOS_STATE_DIR = "/usr/local/var/opp_ci"
MACOS_LOG_DIR = "/usr/local/var/log/opp_ci"

SYSTEMD_DIR = "/etc/systemd/system"
LAUNCHD_DIR = "/Library/LaunchDaemons"
LAUNCHD_LABEL_PREFIX = "org.omnetpp.opp_ci.worker"
NEWSYSLOG_DROPIN = "/etc/newsyslog.d/opp_ci.conf"

# Unit / label names. Kept in sync with config.SERVE_UNIT /
# WORKER_UNIT_TEMPLATE so the web UI log viewer keeps working.
SERVE_UNIT = "opp_ci-serve.service"
WORKER_UNIT_TEMPLATE = "opp_ci-worker@{name}.service"
TARGET_UNIT = "opp_ci.target"
CERT_PATH_UNIT = "opp_ci-serve-cert.path"
CERT_RELOAD_UNIT = "opp_ci-serve-cert-reload.service"

PEM_PACKAGE_FILE = os.path.join(os.path.dirname(__file__), "data",
                                "cloudflare-origin-ca.pem")


class ServiceError(Exception):
    """A service operation could not proceed (privilege, OS, missing arg)."""


# ── OS detection ──────────────────────────────────────────────────────────


def detect_os():
    """Return one of 'nixos', 'macos', 'linux' for the current host.

    NixOS takes precedence over the generic Linux path (it owns units
    declaratively, so the service commands go render-only there).
    """
    if is_nixos():
        return "nixos"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def is_nixos():
    """True on NixOS: /etc/NIXOS, /run/current-system/nixos-version, or
    ID=nixos in /etc/os-release (any one is sufficient)."""
    if os.path.exists("/etc/NIXOS"):
        return True
    if os.path.exists("/run/current-system/nixos-version"):
        return True
    try:
        with open("/etc/os-release") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "ID" and value.strip().strip('"') == "nixos":
                    return True
    except OSError:
        pass
    return False


def state_dir_for(os_kind):
    return MACOS_STATE_DIR if os_kind == "macos" else LINUX_STATE_DIR


# ── Install spec ──────────────────────────────────────────────────────────


class InstallSpec:
    """Resolved parameters for one serve/worker install, shared by all
    renderers and the apply layer. Constructed from CLI options."""

    def __init__(self, *, role, os_kind=None, user=DEFAULT_USER, ref=DEFAULT_REF,
                 # serve
                 host=None, port=None, cert=None, key=None,
                 postgres=True, tls=False,
                 # worker
                 name="default", coordinator=None, token=None,
                 poll_interval=None, heartbeat_interval=None, niceness=None,
                 # lifecycle / output
                 enable=True, start=True, dry_run=False, out_dir=None, purge=False):
        if role not in ("serve", "worker"):
            raise ValueError(f"unknown role {role!r}")
        self.role = role
        self.os_kind = os_kind or detect_os()
        self.user = user
        self.group = user
        self.ref = ref
        self.host = host
        self.port = port
        self.cert = cert
        self.key = key
        self.postgres = postgres
        self.tls = tls
        self.name = name
        self.coordinator = coordinator
        self.token = token
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.niceness = niceness
        self.enable = enable
        self.start = start
        self.dry_run = dry_run
        self.out_dir = out_dir
        self.purge = purge

    @property
    def state_dir(self):
        return state_dir_for(self.os_kind)

    @property
    def home(self):
        # The service user's HOME == its state dir (uv cache → $HOME/.cache/uv).
        return self.state_dir

    @property
    def bindir(self):
        return os.path.join(self.home, ".local", "bin")

    @property
    def extras(self):
        return SERVE_EXTRAS if self.role == "serve" else WORKER_EXTRAS

    def uvx_path(self):
        """Absolute uvx path baked into the unit (OPP_CI_UVX override wins)."""
        if cfg.UVX:
            return cfg.UVX
        return os.path.join(self.bindir, "uvx")

    @property
    def worker_unit(self):
        return WORKER_UNIT_TEMPLATE.format(name=self.name)

    @property
    def worker_env_path(self):
        return f"{WORKER_CONFIG_DIR}/{self.name}.env"

    @property
    def launchd_label(self):
        return f"{LAUNCHD_LABEL_PREFIX}.{self.name}"

    @property
    def launchd_plist_path(self):
        return f"{LAUNCHD_DIR}/{self.launchd_label}.plist"


# ── uvx command ───────────────────────────────────────────────────────────


def uvx_argv(spec, *, uvx=None):
    """The full uvx argv that the unit's ExecStart runs.

    Pins opp_ci to ``@<ref>`` with the role's extras, supplies opp_repl from
    its ``opp_ci`` branch via ``--with``, and forces a re-resolve of both on
    every start with ``--refresh-package`` (the "latest each restart"
    mechanism). The opp_ci subcommand carries no runtime options — all config
    comes from env files.
    """
    uvx = uvx or spec.uvx_path()
    from_spec = f"opp_ci[{spec.extras}] @ {OPP_CI_GIT}@{spec.ref}"
    with_spec = f"opp_repl[all] @ {OPP_REPL_GIT}@{OPP_REPL_REF}"
    subcommand = ["serve"] if spec.role == "serve" else ["worker", "start"]
    return [
        uvx,
        "--from", from_spec,
        "--with", with_spec,
        "--refresh-package", "opp_ci",
        "--refresh-package", "opp_repl",
        "opp_ci", *subcommand,
    ]


def uvx_command(spec, *, uvx=None):
    """uvx_argv rendered as a copy-pasteable shell command string."""
    return " ".join(shlex.quote(a) for a in uvx_argv(spec, uvx=uvx))


# ── Env-file bodies ───────────────────────────────────────────────────────


def _render_env(pairs, *, header):
    """Render ``KEY=value`` lines from an iterable of (key, value) pairs,
    skipping None values. Values are shell-quoted when they need it."""
    lines = [f"# {header}", ""]
    for key, value in pairs:
        if value is None:
            continue
        value = str(value)
        if value == "" or any(c in value for c in ' \t"\'#'):
            value = '"' + value.replace('"', '\\"') + '"'
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def render_shared_env(spec, *, database_url=None):
    """``/etc/opp_ci/opp_ci.env`` — shared by serve and workers."""
    return _render_env(
        [("OPP_CI_DATABASE_URL", database_url)],
        header="/etc/opp_ci/opp_ci.env — shared opp_ci environment (0640 root:opp_ci)",
    )


def render_serve_env(spec):
    """``/etc/opp_ci/serve.env`` — serve runtime options."""
    return _render_env(
        [
            ("OPP_CI_SERVE_HOST", spec.host),
            ("OPP_CI_SERVE_PORT", spec.port),
            ("OPP_CI_SERVE_TLS_CERT_FILE", spec.cert),
            ("OPP_CI_SERVE_TLS_KEY_FILE", spec.key),
        ],
        header="/etc/opp_ci/serve.env — opp_ci-serve options (0640 root:opp_ci)",
    )


def render_worker_env(spec):
    """``/etc/opp_ci/workers/<name>.env`` — one worker's runtime options.

    Includes ``OPP_CI_OPP_ENV_CMD`` so the host-nix opp_env path runs through
    its own uvx tool (isolated venv)."""
    return _render_env(
        [
            ("OPP_CI_COORDINATOR_URL", spec.coordinator),
            ("OPP_CI_WORKER_TOKEN", spec.token),
            ("OPP_CI_WORKER_POLL_INTERVAL", spec.poll_interval),
            ("OPP_CI_WORKER_HEARTBEAT_INTERVAL", spec.heartbeat_interval),
            ("OPP_CI_WORKER_NICENESS", spec.niceness),
            ("OPP_CI_OPP_ENV_CMD", "uvx --from opp-env opp_env"),
        ],
        header=f"/etc/opp_ci/workers/{spec.name}.env — opp_ci worker (0600 opp_ci:opp_ci)",
    )


# ── systemd renderers (Linux / NixOS share the ExecStart) ────────────────


def _path_env(spec):
    return (f"{spec.bindir}:/usr/local/sbin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin")


def render_serve_unit(spec, *, uvx=None):
    return f"""\
[Unit]
Description=opp_ci coordinator (web UI + API + scheduler)
Documentation=https://github.com/omnetpp/opp_ci
After=network-online.target postgresql.service
Wants=network-online.target
PartOf={TARGET_UNIT}

[Service]
Type=simple
User={spec.user}
Group={spec.group}
# Read access to the system journal so the web UI's Logs pages can tail the
# serve and worker units (`journalctl -u …`). Scoped to this process.
SupplementaryGroups=systemd-journal
WorkingDirectory={spec.state_dir}
Environment=HOME={spec.home}
EnvironmentFile={CONFIG_DIR}/opp_ci.env
EnvironmentFile=-{CONFIG_DIR}/serve.env
# {spec.bindir} carries the copied uvx/uv so the daemon resolves them.
Environment="PATH={_path_env(spec)}"
ExecStart={uvx_command(spec, uvx=uvx)}
Restart=on-failure
RestartSec=5s

# Hardening — safe for serve (no Nix, no podman).
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={spec.state_dir}

[Install]
WantedBy={TARGET_UNIT}
"""


def render_worker_unit(spec, *, uvx=None):
    """The ``opp_ci-worker@.service`` template (instance = worker name)."""
    return f"""\
[Unit]
Description=opp_ci worker (%i)
Documentation=https://github.com/omnetpp/opp_ci
After=network-online.target nix-daemon.service
Wants=network-online.target
PartOf={TARGET_UNIT}

[Service]
Type=simple
User={spec.user}
Group={spec.group}
WorkingDirectory={spec.state_dir}
Environment=HOME={spec.home}
EnvironmentFile={CONFIG_DIR}/opp_ci.env
EnvironmentFile={CONFIG_DIR}/workers/%i.env
Environment="PATH={_path_env(spec)}"
ExecStart={uvx_command(spec, uvx=uvx)}
Restart=on-failure
RestartSec=10s
# Worker handles SIGINT / SIGTERM cleanly per doc/workers.md.
KillSignal=SIGTERM
TimeoutStopSec=60s

# Looser hardening — the worker shells out to opp_env / nix / podman.
# Enable selectively once validated on your host:
#   NoNewPrivileges=true
#   ProtectSystem=strict
#   ReadWritePaths={spec.state_dir} /nix/var

[Install]
WantedBy={TARGET_UNIT}
"""


def render_target_unit():
    return f"""\
[Unit]
Description=opp_ci (coordinator and/or workers)
Documentation=https://github.com/omnetpp/opp_ci

[Install]
WantedBy=multi-user.target
"""


def render_cert_path_unit():
    return f"""\
[Unit]
Description=Watch opp_ci TLS cert for renewal
Documentation=https://github.com/omnetpp/opp_ci
PartOf={SERVE_UNIT}

[Path]
# Fires on close-after-write or atomic rename (acme.sh, certbot deploy hooks,
# `opp_ci tls-selfsign`, `install -m`). Write the key file before the cert so
# this watcher sees a consistent pair.
PathChanged={TLS_DIR}/fullchain.pem
Unit={CERT_RELOAD_UNIT}

[Install]
WantedBy={TARGET_UNIT}
"""


def render_cert_reload_unit():
    return f"""\
[Unit]
Description=Restart opp_ci-serve after TLS cert change
Documentation=https://github.com/omnetpp/opp_ci
After={SERVE_UNIT}
Requisite={SERVE_UNIT}

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart {SERVE_UNIT}
"""


# ── launchd renderers (macOS, worker-only) ───────────────────────────────


def render_worker_plist(spec):
    """One LaunchDaemon plist per worker name.

    Env injection is done by the wrapper (launchd can't source env files), so
    ProgramArguments points at the wrapper; the token stays in the 0600 env
    file out of this world-readable plist.
    """
    wrapper = os.path.join(spec.bindir, "opp_ci-worker-run")
    log = f"{MACOS_LOG_DIR}/worker-{spec.name}.log"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{spec.launchd_label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{wrapper}</string>
        <string>{spec.name}</string>
    </array>

    <key>UserName</key>   <string>{spec.user}</string>
    <key>GroupName</key>  <string>{spec.group}</string>

    <key>WorkingDirectory</key>
    <string>{spec.state_dir}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{spec.bindir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>{spec.home}</string>
    </dict>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key> <false/>
    </dict>
    <key>ThrottleInterval</key> <integer>10</integer>

    <key>RunAtLoad</key> <{'true' if spec.start else 'false'}/>

    <key>ExitTimeOut</key> <integer>60</integer>

    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""


def render_worker_wrapper(spec, *, uvx=None):
    """The env-sourcing wrapper launchd's ProgramArguments invokes.

    Sources the shared + per-worker env files (mirroring systemd's two
    EnvironmentFile= lines), then execs the uvx worker command. The token
    stays in the 0600 env file, never in the plist.
    """
    return f"""\
#!/bin/bash
# opp_ci-worker-run <name> — env-sourcing wrapper for a launchd worker.
#
# launchd has no equivalent of systemd's EnvironmentFile=, so this wrapper
# sources the shared env and the per-worker env (token included) before
# exec'ing the uvx worker command. CLI-generated by opp_ci serve/worker
# service install.
set -euo pipefail

name="${{1:?usage: opp_ci-worker-run <worker-name>}}"

set -a
[ -f {CONFIG_DIR}/opp_ci.env ]          && . {CONFIG_DIR}/opp_ci.env
[ -f "{CONFIG_DIR}/workers/$name.env" ] && . "{CONFIG_DIR}/workers/$name.env"
set +a

exec {uvx_command(spec, uvx=uvx)}
"""


def render_newsyslog():
    """newsyslog(8) drop-in for macOS worker-log rotation (no journald)."""
    return f"""\
# opp_ci worker log rotation for macOS's built-in newsyslog(8).
# CLI-generated to {NEWSYSLOG_DROPIN} by `worker service install`.
#
# logfilename                              [owner:group]  mode count size when flags
{MACOS_LOG_DIR}/worker-*.log     {DEFAULT_USER}:{DEFAULT_USER}  644  7     5000 *    GN
"""


# ── NixOS renderers ───────────────────────────────────────────────────────


def render_nixos_module(spec):
    """A standalone NixOS module declaring everything the imperative installer
    would have done. Secrets stay out of the store: the module only
    ``EnvironmentFile=``-references the rendered env files."""
    uvx = "${cfg.uvPackage}/bin/uvx"
    nix_spec = _nixos_exec_spec(spec, uvx=uvx)
    if spec.role == "serve":
        return _render_nixos_serve(spec, nix_spec)
    return _render_nixos_worker(spec, nix_spec)


def _nixos_exec_spec(spec, *, uvx):
    """The ExecStart string for the NixOS unit (pkgs.uv from the store)."""
    return uvx_command(spec, uvx=uvx)


def _render_nixos_serve(spec, exec_start):
    return f"""\
# opp_ci.nix — NixOS module for the opp_ci coordinator (serve).
# CLI-generated by `opp_ci serve service install` on NixOS. Import it and
# apply with `sudo nixos-rebuild switch`. Secrets live in the referenced
# EnvironmentFile, NOT in the Nix store.
{{ config, lib, pkgs, ... }}:

let
  cfg = config.services.opp_ci.serve;
in {{
  options.services.opp_ci.serve = {{
    enable = lib.mkEnableOption "opp_ci coordinator";
    ref = lib.mkOption {{ type = lib.types.str; default = "{spec.ref}"; }};
    user = lib.mkOption {{ type = lib.types.str; default = "{spec.user}"; }};
    uvPackage = lib.mkOption {{ type = lib.types.package; default = pkgs.uv; }};
    postgres.enable = lib.mkOption {{ type = lib.types.bool; default = {('true' if spec.postgres else 'false')}; }};
    tls.enable = lib.mkOption {{ type = lib.types.bool; default = {('true' if spec.tls else 'false')}; }};
    environmentFiles = lib.mkOption {{
      type = lib.types.listOf lib.types.path;
      default = [ "{CONFIG_DIR}/opp_ci.env" "{CONFIG_DIR}/serve.env" ];
    }};
  }};

  config = lib.mkIf cfg.enable {{
    users.groups.${{cfg.user}} = {{}};
    users.users.${{cfg.user}} = {{
      isSystemUser = true;
      group = cfg.user;
      home = "{spec.state_dir}";
      createHome = true;
    }};

    systemd.tmpfiles.rules = [
      "d {CONFIG_DIR} 0750 root ${{cfg.user}} -"
      "d {TLS_DIR} 0750 root ${{cfg.user}} -"
      "d {spec.state_dir} 0750 ${{cfg.user}} ${{cfg.user}} -"
    ];

    services.postgresql = lib.mkIf cfg.postgres.enable {{
      enable = true;
      ensureDatabases = [ "opp_ci" ];
      ensureUsers = [ {{ name = cfg.user; ensureDBOwnership = true; }} ];
    }};

    systemd.services."opp_ci-serve" = {{
      description = "opp_ci coordinator (web UI + API + scheduler)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" "postgresql.service" ];
      wants = [ "network-online.target" ];
      path = [ cfg.uvPackage ];
      environment.HOME = "{spec.state_dir}";
      serviceConfig = {{
        Type = "simple";
        User = cfg.user;
        Group = cfg.user;
        SupplementaryGroups = [ "systemd-journal" ];
        WorkingDirectory = "{spec.state_dir}";
        EnvironmentFile = cfg.environmentFiles;
        ExecStart = "{exec_start}";
        Restart = "on-failure";
        RestartSec = 5;
      }};
    }};
  }};
}}
"""


def _render_nixos_worker(spec, exec_start):
    return f"""\
# opp_ci.nix — NixOS module for an opp_ci worker.
# CLI-generated by `opp_ci worker service install` on NixOS. Import it and
# apply with `sudo nixos-rebuild switch`. The worker token lives in the
# referenced EnvironmentFile, NOT in the Nix store.
{{ config, lib, pkgs, ... }}:

let
  cfg = config.services.opp_ci.worker;
in {{
  options.services.opp_ci.worker = {{
    enable = lib.mkEnableOption "opp_ci worker";
    ref = lib.mkOption {{ type = lib.types.str; default = "{spec.ref}"; }};
    user = lib.mkOption {{ type = lib.types.str; default = "{spec.user}"; }};
    uvPackage = lib.mkOption {{ type = lib.types.package; default = pkgs.uv; }};
    instances = lib.mkOption {{
      type = lib.types.listOf lib.types.str;
      default = [ "{spec.name}" ];
      description = "Worker instance names (one EnvironmentFile per name).";
    }};
  }};

  config = lib.mkIf cfg.enable {{
    users.groups.${{cfg.user}} = {{}};
    users.users.${{cfg.user}} = {{
      isSystemUser = true;
      group = cfg.user;
      home = "{spec.state_dir}";
      createHome = true;
    }};

    virtualisation.podman.enable = true;

    systemd.tmpfiles.rules = [
      "d {CONFIG_DIR} 0750 root ${{cfg.user}} -"
      "d {WORKER_CONFIG_DIR} 0750 ${{cfg.user}} ${{cfg.user}} -"
      "d {spec.state_dir} 0750 ${{cfg.user}} ${{cfg.user}} -"
    ];

    systemd.services."opp_ci-worker@" = {{
      description = "opp_ci worker (%i)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [ cfg.uvPackage ];
      environment.HOME = "{spec.state_dir}";
      serviceConfig = {{
        Type = "simple";
        User = cfg.user;
        Group = cfg.user;
        WorkingDirectory = "{spec.state_dir}";
        EnvironmentFile = [ "{CONFIG_DIR}/opp_ci.env" "{CONFIG_DIR}/workers/%i.env" ];
        ExecStart = "{exec_start}";
        Restart = "on-failure";
        RestartSec = 10;
        KillSignal = "SIGTERM";
        TimeoutStopSec = 60;
      }};
    }};

    systemd.targets.opp_ci = {{
      wantedBy = [ "multi-user.target" ];
    }};
  }};
}}
"""


def render_nixos_flake(spec):
    """A flake exposing nixosModules for flake-based configs."""
    return """\
# flake.nix — opp_ci NixOS modules (CLI-generated).
# Use either the bare ./opp_ci.nix module or this flake's nixosModules.
{
  description = "opp_ci NixOS modules";

  outputs = { self }: {
    nixosModules.opp_ci-serve = import ./opp_ci.nix;
    nixosModules.opp_ci-worker = import ./opp_ci.nix;
    nixosModules.default = import ./opp_ci.nix;
  };
}
"""


def render_nixos_apply_instructions(spec):
    """Operator-facing apply block: where to drop the files, the import line,
    the enable toggle, then nixos-rebuild switch."""
    toggle = ("services.opp_ci.serve.enable = true;" if spec.role == "serve"
              else "services.opp_ci.worker.enable = true;")
    env_files = ([f"{CONFIG_DIR}/opp_ci.env (0640 root:{spec.user})",
                  f"{CONFIG_DIR}/serve.env (0640 root:{spec.user})"]
                 if spec.role == "serve"
                 else [f"{CONFIG_DIR}/opp_ci.env (0640 root:{spec.user})",
                       f"{spec.worker_env_path} (0600 {spec.user}:{spec.user})"])
    lines = [
        "# ── Apply on NixOS ───────────────────────────────────────────────",
        "# 1. Drop opp_ci.nix next to your configuration.nix (or use the flake).",
        "# 2. Write the env-file bodies below imperatively (secrets stay out of",
        "#    the Nix store):",
    ]
    for ef in env_files:
        lines.append(f"#      {ef}")
    lines += [
        "# 3. In configuration.nix:",
        "#      imports = [ ./opp_ci.nix ];",
        f"#      {toggle}",
        f'#      # services.opp_ci.{spec.role}.ref = "{spec.ref}";   # override the pinned ref',
        "# 4. sudo nixos-rebuild switch",
        "#",
        "# Managed secrets: have the module's EnvironmentFile reference a",
        "# sops-nix / agenix secret path, e.g.",
        '#   config.sops.secrets."opp_ci/worker-token".path',
        "# so you never hand-write the token file.",
    ]
    return "\n".join(lines) + "\n"


# ── No-sudo / dry-run transcript ──────────────────────────────────────────


def render_manual_transcript(plan):
    """Render an :class:`InstallPlan` as a copy-pasteable manual transcript.

    Powers both the unprivileged fallback (§7) and ``--dry-run`` (§2.3): lists
    every file to create (path, owner, mode, exact contents), the uv/uvx copy,
    user/dir creation, and the enable/start commands, in order.
    """
    out = []
    out.append("# ── Manual opp_ci service install ────────────────────────────────")
    out.append("# Run these as root (the CLI needs root to do them for you).")
    out.append("")
    if plan.user_cmds:
        out.append("# 1) Create the service user and directories:")
        for desc, argv in plan.user_cmds:
            out.append(f"#    {desc}")
            out.append("    " + " ".join(shlex.quote(a) for a in argv))
        out.append("")
    if plan.uv_copy:
        out.append("# 2) Copy uv/uvx to the service user:")
        for src, dst in plan.uv_copy:
            out.append(f"    install -D -m 0755 {shlex.quote(src)} {shlex.quote(dst)}")
            out.append(f"    chown {plan.spec.user}:{plan.spec.group} {shlex.quote(dst)}")
        out.append("")
    out.append("# 3) Write these files:")
    for art in plan.files:
        owner = f"{art.owner}:{art.group}"
        out.append(f"# ---- {art.path}  ({owner}, mode {art.mode:04o}) ----")
        out.append(f"install -d -m 0755 {shlex.quote(os.path.dirname(art.path))}")
        out.append(f"cat > {shlex.quote(art.path)} <<'OPP_CI_EOF'")
        out.append(art.content.rstrip("\n"))
        out.append("OPP_CI_EOF")
        out.append(f"chown {owner} {shlex.quote(art.path)} && "
                   f"chmod {art.mode:04o} {shlex.quote(art.path)}")
        out.append("")
    if plan.provision_cmds:
        out.append("# 4) Provisioning:")
        for desc, argv in plan.provision_cmds:
            out.append(f"#    {desc}")
            out.append("    " + " ".join(shlex.quote(a) for a in argv))
        out.append("")
    if plan.lifecycle_cmds:
        out.append("# 5) Enable / start:")
        for argv in plan.lifecycle_cmds:
            out.append("    " + " ".join(shlex.quote(a) for a in argv))
        out.append("")
    return "\n".join(out)


# ── Install plan (artifacts + commands) ───────────────────────────────────


class FileArtifact:
    def __init__(self, path, content, *, owner="root", group=None, mode=0o644,
                 secret=False, keep_existing=False):
        self.path = path
        self.content = content
        self.owner = owner
        self.group = group or owner
        self.mode = mode
        self.secret = secret
        # keep_existing → don't overwrite if present (env files / tokens).
        self.keep_existing = keep_existing


class InstallPlan:
    def __init__(self, spec):
        self.spec = spec
        self.files = []
        self.user_cmds = []       # (description, argv)
        self.uv_copy = []         # (src, dst)
        self.provision_cmds = []  # (description, argv)
        self.lifecycle_cmds = []  # argv


def build_install_plan(spec, *, uvx=None):
    """Build the full systemd/launchd :class:`InstallPlan` for *spec*.

    Pure: computes artifacts + commands without touching the system. Used by
    the transcript renderer, ``--dry-run``, and the apply layer.
    """
    plan = InstallPlan(spec)
    user, group = spec.user, spec.group

    # ── user + dirs ──────────────────────────────────────────────────────
    if spec.os_kind == "macos":
        plan.user_cmds.append(
            (f"create hidden service account '{user}' (use dscl; see docs)",
             ["dscl", ".", "-create", f"/Users/{user}"]))
    else:
        plan.user_cmds.append(
            (f"create system user '{user}'",
             ["useradd", "--system", "--gid", group, "--home-dir", spec.state_dir,
              "--shell", "/bin/bash", "--comment", "opp_ci CI service", user]))

    for d, mode in [(CONFIG_DIR, "0750"), (WORKER_CONFIG_DIR, "0750"),
                    (spec.state_dir, "0750"), (TLS_DIR, "0750"),
                    (spec.bindir, "0755")]:
        plan.user_cmds.append((f"create {d}",
                               ["install", "-d", "-o", user, "-g", group, "-m", mode, d]))
    if spec.os_kind == "macos":
        plan.user_cmds.append((f"create {MACOS_LOG_DIR}",
                               ["install", "-d", "-o", user, "-g", group, "-m", "0750", MACOS_LOG_DIR]))

    # ── uv/uvx copy (skipped on self-install / NixOS) ────────────────────
    _plan_uv_copy(plan, spec)

    # ── env files ────────────────────────────────────────────────────────
    plan.files.append(FileArtifact(
        f"{CONFIG_DIR}/opp_ci.env", render_shared_env(spec),
        owner="root", group=group, mode=0o640, keep_existing=True))
    if spec.role == "serve":
        plan.files.append(FileArtifact(
            f"{CONFIG_DIR}/serve.env", render_serve_env(spec),
            owner="root", group=group, mode=0o640, keep_existing=True))
    else:
        plan.files.append(FileArtifact(
            spec.worker_env_path, render_worker_env(spec),
            owner=user, group=group, mode=0o600, secret=True, keep_existing=True))

    # Cloudflare Origin CA bundle (shipped as package data).
    try:
        with open(PEM_PACKAGE_FILE) as f:
            pem = f.read()
        plan.files.append(FileArtifact(
            f"{TLS_DIR}/cloudflare-origin-ca.pem", pem,
            owner="root", group=group, mode=0o644))
    except OSError:
        pass

    # ── units / plists ───────────────────────────────────────────────────
    if spec.os_kind == "macos":
        _plan_launchd(plan, spec, uvx=uvx)
    else:
        _plan_systemd(plan, spec, uvx=uvx)

    return plan


def _plan_uv_copy(plan, spec):
    if cfg.UVX:
        return  # operator supplied an absolute uvx path; nothing to copy.
    invoking_user = _invoking_user()
    if spec.user == invoking_user:
        return  # self-install: the unit references the invoking user's uvx.
    for name in ("uv", "uvx"):
        src = shutil.which(name)
        dst = os.path.join(spec.bindir, name)
        if src:
            plan.uv_copy.append((src, dst))


def _plan_systemd(plan, spec, *, uvx=None):
    g = spec.group
    if spec.role == "serve":
        plan.files.append(FileArtifact(
            f"{SYSTEMD_DIR}/{SERVE_UNIT}", render_serve_unit(spec, uvx=uvx), mode=0o644))
        if spec.tls:
            plan.files.append(FileArtifact(
                f"{SYSTEMD_DIR}/{CERT_PATH_UNIT}", render_cert_path_unit(), mode=0o644))
            plan.files.append(FileArtifact(
                f"{SYSTEMD_DIR}/{CERT_RELOAD_UNIT}", render_cert_reload_unit(), mode=0o644))
    else:
        plan.files.append(FileArtifact(
            f"{SYSTEMD_DIR}/opp_ci-worker@.service",
            render_worker_unit(spec, uvx=uvx), mode=0o644))
    plan.files.append(FileArtifact(
        f"{SYSTEMD_DIR}/{TARGET_UNIT}", render_target_unit(), mode=0o644))

    # provisioning + lifecycle
    if spec.role == "serve" and spec.postgres:
        plan.provision_cmds.append(
            ("provision local PostgreSQL (role + db + grant)",
             ["sudo", "-u", "postgres", "createuser", spec.user]))
    if spec.role == "worker":
        plan.provision_cmds.append(
            ("enable lingering for rootless podman",
             ["loginctl", "enable-linger", spec.user]))

    plan.lifecycle_cmds.append(["systemctl", "daemon-reload"])
    unit = SERVE_UNIT if spec.role == "serve" else spec.worker_unit
    if spec.enable and spec.start:
        plan.lifecycle_cmds.append(["systemctl", "enable", "--now", unit])
    elif spec.enable:
        plan.lifecycle_cmds.append(["systemctl", "enable", unit])
    elif spec.start:
        plan.lifecycle_cmds.append(["systemctl", "start", unit])
    if spec.tls and spec.role == "serve":
        plan.lifecycle_cmds.append(["systemctl", "enable", "--now", CERT_PATH_UNIT])


def _plan_launchd(plan, spec, *, uvx=None):
    plan.files.append(FileArtifact(
        os.path.join(spec.bindir, "opp_ci-worker-run"),
        render_worker_wrapper(spec, uvx=uvx),
        owner=spec.user, group=spec.group, mode=0o755))
    plan.files.append(FileArtifact(
        spec.launchd_plist_path, render_worker_plist(spec),
        owner="root", group="wheel", mode=0o644))
    plan.files.append(FileArtifact(
        NEWSYSLOG_DROPIN, render_newsyslog(), owner="root", group="wheel", mode=0o644))
    if spec.start:
        plan.lifecycle_cmds.append(["launchctl", "bootstrap", "system", spec.launchd_plist_path])


# ── Privilege + invoking-user helpers ─────────────────────────────────────


def _invoking_user():
    """The real invoking user (SUDO_USER if present, else current)."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return sudo_user
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, ImportError):
        return os.environ.get("USER", "")


def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


# ── Apply layer (side effects) ────────────────────────────────────────────


def apply_plan(plan, *, echo=print):
    """Execute an :class:`InstallPlan` on the system (root required)."""
    spec = plan.spec
    for desc, argv in plan.user_cmds:
        _run(argv, desc, echo=echo, tolerate=True)
    for src, dst in plan.uv_copy:
        echo(f"  copy {src} → {dst}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        _chown(dst, spec.user, spec.group)
        os.chmod(dst, 0o755)
    for art in plan.files:
        if art.keep_existing and os.path.exists(art.path):
            echo(f"  keeping existing {art.path}")
            continue
        echo(f"  write {art.path} ({art.mode:04o})")
        os.makedirs(os.path.dirname(art.path), exist_ok=True)
        _write(art.path, art.content, mode=art.mode)
        _chown(art.path, art.owner, art.group)
    for desc, argv in plan.provision_cmds:
        _run(argv, desc, echo=echo, tolerate=True)
    for argv in plan.lifecycle_cmds:
        _run(argv, " ".join(argv), echo=echo, tolerate=False)


def _run(argv, desc, *, echo, tolerate):
    echo(f"  $ {' '.join(shlex.quote(a) for a in argv)}")
    try:
        subprocess.run(argv, check=not tolerate)
    except (subprocess.CalledProcessError, OSError) as e:
        if tolerate:
            echo(f"    (skipped: {e})")
        else:
            raise ServiceError(f"command failed: {desc}: {e}")


def _write(path, content, *, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(path, mode)


def _chown(path, user, group):
    try:
        import grp
        import pwd
        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid
        os.chown(path, uid, gid)
    except (KeyError, ImportError, OSError):
        pass


# ── Lifecycle (start/stop/restart/status) ─────────────────────────────────


def _systemctl(action, unit, *, echo):
    cmd = {"start": "start", "stop": "stop", "restart": "restart",
           "status": "status"}[action]
    _run(["systemctl", cmd, unit], f"systemctl {cmd} {unit}", echo=echo, tolerate=True)


def _launchctl_lifecycle(spec, action, *, echo):
    label = f"system/{spec.launchd_label}"
    if action == "start":
        _run(["launchctl", "bootstrap", "system", spec.launchd_plist_path],
             "bootstrap", echo=echo, tolerate=True)
    elif action == "stop":
        _run(["launchctl", "bootout", label], "bootout", echo=echo, tolerate=True)
    elif action == "restart":
        _run(["launchctl", "kickstart", "-k", label], "kickstart", echo=echo, tolerate=True)
    elif action == "status":
        _run(["launchctl", "print", label], "print", echo=echo, tolerate=True)


def lifecycle(spec, action, *, echo=print):
    """Drive start/stop/restart/status for *spec* per OS."""
    if spec.os_kind == "macos":
        _launchctl_lifecycle(spec, action, echo=echo)
    else:
        unit = SERVE_UNIT if spec.role == "serve" else spec.worker_unit
        _systemctl(action, unit, echo=echo)


# ── NixOS render bundle ───────────────────────────────────────────────────


def render_nixos_bundle(spec):
    """Render the full NixOS artifact set as an ordered list of
    (filename, content) pairs: module, flake, env-file bodies, apply block."""
    out = [
        ("opp_ci.nix", render_nixos_module(spec)),
        ("flake.nix", render_nixos_flake(spec)),
        ("opp_ci.env", render_shared_env(spec)),
    ]
    if spec.role == "serve":
        out.append(("serve.env", render_serve_env(spec)))
    else:
        out.append((f"{spec.name}.env", render_worker_env(spec)))
    out.append(("APPLY.txt", render_nixos_apply_instructions(spec)))
    return out


def _emit_nixos(spec, *, echo):
    """Render-only NixOS path: write to --out DIR or print to stdout."""
    bundle = render_nixos_bundle(spec)
    if spec.out_dir:
        os.makedirs(spec.out_dir, exist_ok=True)
        for name, content in bundle:
            path = os.path.join(spec.out_dir, name)
            with open(path, "w") as f:
                f.write(content)
            echo(f"  wrote {path}")
        echo("")
        echo(f"NixOS artifacts written to {spec.out_dir}. See APPLY.txt for the "
             "import + nixos-rebuild steps.")
    else:
        for name, content in bundle:
            echo(f"# ===== {name} =====")
            echo(content)


# ── Top-level dispatch (called by the CLI) ────────────────────────────────


def do_install(spec, *, echo=print):
    """Install the service per OS. NixOS → render-only; otherwise privilege-
    gated apply, with a manual transcript when unprivileged or --dry-run."""
    # Worker auto-start guard: no token now and none on disk → skip start.
    if spec.role == "worker" and spec.start and not spec.token:
        if not os.path.exists(spec.worker_env_path):
            echo(f"No --token given and {spec.worker_env_path} is absent: "
                 f"auto-start skipped. Set the token, then run "
                 f"`opp_ci worker service start --name {spec.name}`.")
            spec.start = False

    if spec.os_kind == "nixos":
        echo("NixOS detected: rendering a declarative module (no system "
             "mutation). Apply it with `sudo nixos-rebuild switch`.")
        echo("")
        _emit_nixos(spec, echo=echo)
        return

    uvx = spec.uvx_path()
    _warn_if_uv_missing(spec, echo=echo)
    plan = build_install_plan(spec, uvx=uvx)

    if spec.dry_run:
        echo(render_manual_transcript(plan))
        return
    if not _is_root():
        echo("Not running as root — no changes made. Manual recipe follows:")
        echo("")
        echo(render_manual_transcript(plan))
        raise ServiceError("root privileges required to apply (see transcript above)")

    apply_plan(plan, echo=echo)
    _print_migration_note(spec, echo=echo)


def _warn_if_uv_missing(spec, *, echo):
    if cfg.UVX:
        return
    if spec.user == _invoking_user():
        return
    for name in ("uv", "uvx"):
        if not shutil.which(name):
            echo(f"WARNING: {name} not found for the invoking user. The "
                 f"service will not start until uv/uvx are available to "
                 f"'{spec.user}' at {spec.bindir}/ (or set OPP_CI_UVX).")


def _print_migration_note(spec, *, echo):
    paths = ["/opt/opp_ci", "/opt/opp_env", "/opt/opp_repl",
             f"{spec.state_dir}/.profile"]
    existing = [p for p in paths if os.path.exists(p)]
    if existing:
        echo("")
        echo("Note: the following venv-based-install paths are now unused and "
             "can be removed manually once you confirm the uvx units work:")
        for p in existing:
            echo(f"  {p}")


def do_uninstall(spec, *, echo=print):
    """Uninstall (conservative). NixOS → render instructions only."""
    if spec.os_kind == "nixos":
        echo("NixOS detected: remove the module import / flake reference from "
             "your configuration and run `sudo nixos-rebuild switch`.")
        echo("Boot enablement is owned by the module, so there is no "
             "`systemctl disable` step.")
        if spec.purge:
            echo("")
            echo("--purge: also delete these imperative paths by hand:")
            echo(f"  {CONFIG_DIR}  {spec.state_dir}")
        return

    if spec.dry_run:
        echo(render_uninstall_transcript(spec))
        return
    if not _is_root():
        echo("Not running as root — no changes made. Manual recipe follows:")
        echo("")
        echo(render_uninstall_transcript(spec))
        raise ServiceError("root privileges required to apply (see transcript above)")

    _apply_uninstall(spec, echo=echo)


def render_uninstall_transcript(spec):
    out = ["# ── Manual opp_ci service uninstall ─────────────────────────────"]
    for argv in _uninstall_cmds(spec):
        out.append("    " + " ".join(shlex.quote(a) for a in argv))
    for path in _uninstall_files(spec):
        out.append(f"    rm -f {shlex.quote(path)}")
    if spec.purge:
        out.append("# --purge also removes config + state (after confirmation):")
        for path in _purge_paths(spec):
            out.append(f"    rm -rf {shlex.quote(path)}")
    return "\n".join(out) + "\n"


def _uninstall_cmds(spec):
    if spec.os_kind == "macos":
        return [["launchctl", "bootout", f"system/{spec.launchd_label}"]]
    unit = SERVE_UNIT if spec.role == "serve" else spec.worker_unit
    return [["systemctl", "disable", "--now", unit], ["systemctl", "daemon-reload"]]


def _uninstall_files(spec):
    if spec.os_kind == "macos":
        return [spec.launchd_plist_path]
    if spec.role == "serve":
        return [f"{SYSTEMD_DIR}/{SERVE_UNIT}",
                f"{SYSTEMD_DIR}/{CERT_PATH_UNIT}",
                f"{SYSTEMD_DIR}/{CERT_RELOAD_UNIT}"]
    # A worker uninstall removes only this instance's env file; the shared
    # opp_ci-worker@.service template + target are kept for other instances.
    return [spec.worker_env_path]


def _purge_paths(spec):
    if spec.role == "serve":
        return [CONFIG_DIR, spec.state_dir]
    return [spec.worker_env_path]


def _apply_uninstall(spec, *, echo):
    for argv in _uninstall_cmds(spec):
        _run(argv, " ".join(argv), echo=echo, tolerate=True)
    for path in _uninstall_files(spec):
        if os.path.exists(path):
            echo(f"  rm {path}")
            try:
                os.remove(path)
            except OSError as e:
                echo(f"    (skipped: {e})")
    if spec.os_kind != "macos":
        _run(["systemctl", "daemon-reload"], "daemon-reload", echo=echo, tolerate=True)
    if spec.purge:
        echo("--purge: removing config + state.")
        for path in _purge_paths(spec):
            _run(["rm", "-rf", path], f"rm -rf {path}", echo=echo, tolerate=True)


def do_lifecycle(spec, action, *, echo=print):
    """start/stop/restart/status. Works on NixOS too (units exist once the
    module is applied)."""
    if not _is_root() and spec.os_kind != "nixos":
        echo("Note: lifecycle commands usually need root (system units).")
    lifecycle(spec, action, echo=echo)
