# NixOS module for opp_ci workers.
#
# Each named worker is `services.opp_ci.worker.instances.<name> = { … }` and
# becomes a concrete opp_ci-worker-<name>.service — fully declarative
# per-instance config, no shared template + per-name env file.
#
# Secret VALUES never appear here: the worker token is delivered by path via
# the instance's `environmentFiles` (a KEY=value file holding
# OPP_CI_WORKER_TOKEN=…, e.g. from sops-nix / agenix).
{ config, lib, pkgs, ... }:
let
  top = config.services.opp_ci.worker;
  helpers = import ./lib.nix { inherit lib; };
  inherit (helpers) renderSettings settingsType;

  stateDir = "/var/lib/opp_ci";

  execStart = "${top.uvPackage}/bin/uvx"
    + " --from \"opp_ci[client,podman] @ git+https://github.com/omnetpp/opp_ci.git@${top.ref}\""
    + " --with \"opp_repl[all] @ git+https://github.com/omnetpp/opp_repl.git@opp_ci\""
    + " --refresh-package opp_ci --refresh-package opp_repl"
    + " opp_ci worker start";

  instanceModule = { name, ... }: {
    options = {
      enable = lib.mkOption {
        type = lib.types.bool; default = true;
        description = "Whether this worker instance's unit is created.";
      };
      coordinatorUrl = lib.mkOption {
        type = lib.types.str;
        description = "Coordinator URL the worker polls → OPP_CI_COORDINATOR_URL.";
      };
      pollInterval = lib.mkOption { type = lib.types.int; default = 10; };
      heartbeatInterval = lib.mkOption { type = lib.types.int; default = 30; };
      niceness = lib.mkOption { type = lib.types.int; default = 10; };
      oppEnvCmd = lib.mkOption {
        type = lib.types.str; default = "uvx --from opp-env opp_env";
        description = "opp_env launcher for the host-nix path → OPP_CI_OPP_ENV_CMD.";
      };
      settings = lib.mkOption {
        type = settingsType; default = {};
        description = "Freeform OPP_CI_* environment for this worker (non-secret).";
      };
      environmentFiles = lib.mkOption {
        type = lib.types.listOf lib.types.path; default = [];
        example = lib.literalExpression ''[ config.age.secrets."opp_ci-w1".path ]'';
        description = ''
          KEY=value secret files (systemd EnvironmentFile=) for this worker —
          notably OPP_CI_WORKER_TOKEN=…. Keeps the token out of the Nix store.
        '';
      };
    };
  };

  mkUnit = name: w: lib.nameValuePair "opp_ci-worker-${name}" {
    description = "opp_ci worker (${name})";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" "nix-daemon.service" ];
    wants = [ "network-online.target" ];
    path = [ top.uvPackage ];
    environment = (lib.filterAttrs (_: v: v != null) {
      OPP_CI_COORDINATOR_URL           = w.coordinatorUrl;
      OPP_CI_WORKER_POLL_INTERVAL      = toString w.pollInterval;
      OPP_CI_WORKER_HEARTBEAT_INTERVAL = toString w.heartbeatInterval;
      OPP_CI_WORKER_NICENESS           = toString w.niceness;
      OPP_CI_OPP_ENV_CMD               = w.oppEnvCmd;
    }) // (renderSettings w.settings) // { HOME = stateDir; };
    serviceConfig = {
      Type = "simple";
      User = top.user;
      Group = top.user;
      WorkingDirectory = stateDir;
      EnvironmentFile = w.environmentFiles;
      ExecStart = execStart;
      Restart = "on-failure";
      RestartSec = 10;
      KillSignal = "SIGTERM";
      TimeoutStopSec = 60;
    };
  };

  enabledWorkers = lib.filterAttrs (_: w: w.enable) top.instances;
in {
  options.services.opp_ci.worker = {
    user = lib.mkOption {
      type = lib.types.str; default = "opp_ci";
      description = "System user/group the workers run as.";
    };
    ref = lib.mkOption {
      type = lib.types.str; default = "main";
      description = "opp_ci GitHub ref baked into the uvx ExecStart.";
    };
    uvPackage = lib.mkOption {
      type = lib.types.package; default = pkgs.uv;
      description = "uv package providing uvx/uv.";
    };
    instances = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule instanceModule);
      default = {};
      example = lib.literalExpression ''
        {
          builder-1 = { coordinatorUrl = "https://ci.example.org";
                        environmentFiles = [ config.age.secrets."opp_ci-w1".path ]; };
        }
      '';
      description = "Named worker instances; each becomes opp_ci-worker-<name>.service.";
    };
  };

  config = lib.mkIf (enabledWorkers != {}) {
    users.groups.${top.user} = {};
    users.users.${top.user} = {
      isSystemUser = true;
      group = top.user;
      home = stateDir;
      createHome = true;
    };

    virtualisation.podman.enable = true;

    systemd.tmpfiles.rules = [
      "d /etc/opp_ci 0750 root        ${top.user} -"
      "d ${stateDir} 0750 ${top.user} ${top.user} -"
    ];

    systemd.services = lib.mapAttrs' mkUnit enabledWorkers;
  };
}
