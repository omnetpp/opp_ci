# NixOS module for the opp_ci coordinator (web UI + API + scheduler).
#
# Fully declarative: every runtime parameter is a module option rendered into
# the systemd unit's `environment`. Secret VALUES never appear here (the Nix
# store is world-readable) — they are delivered by path via `environmentFiles`
# (KEY=value files, e.g. from sops-nix / agenix) or the dedicated `*File`
# options for the secrets the app reads from a raw-value file.
#
# Import it and set `services.opp_ci.coordinator.enable = true;`.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.opp_ci.coordinator;
  helpers = import ./lib.nix { inherit lib; };
  inherit (helpers) renderSettings settingsType;

  stateDir = "/var/lib/opp_ci";

  # uvx ExecStart: pin opp_ci@ref with the coordinator extras, supply opp_repl
  # from its opp_ci branch, and force a re-resolve of both on every start
  # (--refresh-package) so a restart picks up the latest code on the ref.
  execStart = "${cfg.uvPackage}/bin/uvx"
    + " --from \"opp_ci[web,postgres,client,podman] @ git+https://github.com/omnetpp/opp_ci.git@${cfg.ref}\""
    + " --with \"opp_repl[all] @ git+https://github.com/omnetpp/opp_repl.git@opp_ci\""
    + " --refresh-package opp_ci --refresh-package opp_repl"
    + " opp_ci coordinator start";

  # Typed non-secret options projected onto their OPP_CI_* vars. Nulls are
  # dropped so an unset option falls through to the app default in config.py.
  # `*File` paths point at raw-value secret files the app reads natively.
  typedEnv = lib.filterAttrs (_: v: v != null) {
    OPP_CI_COORDINATOR_HOST                       = cfg.host;
    OPP_CI_COORDINATOR_PORT                       = toString cfg.port;
    OPP_CI_PUBLIC_URL                       = cfg.publicUrl;
    OPP_CI_GITHUB_ORG                       = cfg.github.org;
    OPP_CI_GITHUB_OAUTH_CLIENT_ID           = cfg.github.oauthClientId;
    OPP_CI_GITHUB_SUBMITTER_TEAMS           =
      if cfg.github.submitterTeams == null then null
      else lib.concatStringsSep "," cfg.github.submitterTeams;
    OPP_CI_COORDINATOR_TLS_CERT_FILE              = if cfg.tls.enable then cfg.tls.certFile else null;
    OPP_CI_COORDINATOR_TLS_KEY_FILE               = if cfg.tls.enable then cfg.tls.keyFile else null;
    OPP_CI_COORDINATOR_TLS_KEY_PASSWORD_FILE      = cfg.tls.keyPasswordFile;
    OPP_CI_GITHUB_TOKEN_FILE                = cfg.github.tokenFile;
    OPP_CI_GITHUB_OAUTH_CLIENT_SECRET_FILE  = cfg.github.oauthClientSecretFile;
    OPP_CI_GITHUB_ACTIONS_TOKEN_FILE        = cfg.github.actionsTokenFile;
    # Workers run as opp_ci-worker-<name>.service (see worker.nix), so tell
    # the log viewer how to resolve their journals. Overridable via settings.
    OPP_CI_WORKER_UNIT_TEMPLATE             = "opp_ci-worker-{instance}.service";
  };
in {
  options.services.opp_ci.coordinator = {
    enable = lib.mkEnableOption "opp_ci coordinator (web UI + API + scheduler)";

    ref = lib.mkOption {
      type = lib.types.str; default = "main";
      description = "opp_ci GitHub ref baked into the uvx ExecStart.";
    };
    user = lib.mkOption {
      type = lib.types.str; default = "opp_ci";
      description = "System user/group the service runs as.";
    };
    uvPackage = lib.mkOption {
      type = lib.types.package; default = pkgs.uv;
      description = "uv package providing uvx/uv (nixpkgs' uv is FHS-patched).";
    };

    host = lib.mkOption {
      type = lib.types.str; default = "127.0.0.1";
      description = "Bind host → OPP_CI_COORDINATOR_HOST.";
    };
    port = lib.mkOption {
      type = lib.types.port; default = 8080;
      description = "Bind port → OPP_CI_COORDINATOR_PORT.";
    };
    publicUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str; default = null;
      description = "Public origin for OAuth callbacks → OPP_CI_PUBLIC_URL.";
    };

    postgres.enable = lib.mkOption {
      type = lib.types.bool; default = true;
      description = "Provision a local PostgreSQL (services.postgresql) for opp_ci.";
    };

    tls = {
      enable = lib.mkOption { type = lib.types.bool; default = false; };
      certFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path; default = null;
        description = "TLS cert path → OPP_CI_COORDINATOR_TLS_CERT_FILE.";
      };
      keyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path; default = null;
        description = "TLS key path → OPP_CI_COORDINATOR_TLS_KEY_FILE.";
      };
      keyPasswordFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path; default = null;
        description = "Path to the TLS key passphrase (secret) → OPP_CI_COORDINATOR_TLS_KEY_PASSWORD_FILE.";
      };
    };

    github = {
      org = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null; };
      oauthClientId = lib.mkOption {
        type = lib.types.nullOr lib.types.str; default = null;
        description = "GitHub OAuth App client id (public) → OPP_CI_GITHUB_OAUTH_CLIENT_ID.";
      };
      submitterTeams = lib.mkOption {
        type = lib.types.nullOr (lib.types.listOf lib.types.str); default = null;
        description = "GitHub team slugs granted submitter → OPP_CI_GITHUB_SUBMITTER_TEAMS.";
      };
      tokenFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path; default = null;
        description = "Path to the GitHub API token (secret) → OPP_CI_GITHUB_TOKEN_FILE.";
      };
      oauthClientSecretFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path; default = null;
        description = "Path to the OAuth App client secret (secret) → OPP_CI_GITHUB_OAUTH_CLIENT_SECRET_FILE.";
      };
      actionsTokenFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path; default = null;
        description = "Path to the Actions PAT (secret) → OPP_CI_GITHUB_ACTIONS_TOKEN_FILE.";
      };
    };

    settings = lib.mkOption {
      type = settingsType; default = {};
      example = { OPP_CI_WORKER_HEARTBEAT_TIMEOUT = 180; OPP_CI_GITHUB_ALLOW_EXTERNAL = false; };
      description = ''
        Freeform OPP_CI_* environment, merged into the unit's environment AFTER
        the typed options (so it overrides them). The escape hatch for the long
        tail of OPP_CI_* vars not promoted to named options. Do NOT put secret
        VALUES here — they would land in the world-readable Nix store; use
        environmentFiles instead.
      '';
    };

    environmentFiles = lib.mkOption {
      type = lib.types.listOf lib.types.path; default = [];
      example = lib.literalExpression ''[ config.age.secrets."opp_ci-coord".path ]'';
      description = ''
        KEY=value secret files passed to systemd EnvironmentFile=. Keeps secret
        values out of the Nix store. Use for OPP_CI_DATABASE_URL,
        OPP_CI_SESSION_SECRET, OPP_CI_API_TOKEN, OPP_CI_GITHUB_WEBHOOK_SECRET.
        Manage the files with sops-nix or agenix.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [{
      assertion = !cfg.tls.enable || (cfg.tls.certFile != null && cfg.tls.keyFile != null);
      message = "services.opp_ci.coordinator.tls.enable requires tls.certFile and tls.keyFile.";
    }];

    users.groups.${cfg.user} = {};
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.user;
      home = stateDir;
      createHome = true;
    };

    systemd.tmpfiles.rules = [
      "d /etc/opp_ci     0750 root        ${cfg.user} -"
      "d /etc/opp_ci/tls 0750 root        ${cfg.user} -"
      "d ${stateDir}     0750 ${cfg.user} ${cfg.user} -"
    ];

    services.postgresql = lib.mkIf cfg.postgres.enable {
      enable = true;
      ensureDatabases = [ "opp_ci" ];
      ensureUsers = [{ name = cfg.user; ensureDBOwnership = true; }];
    };

    systemd.services."opp_ci-coordinator" = {
      description = "opp_ci coordinator (web UI + API + scheduler)";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" "postgresql.service" ];
      wants = [ "network-online.target" ];
      path = [ cfg.uvPackage ];
      environment = typedEnv // (renderSettings cfg.settings) // { HOME = stateDir; };
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.user;
        SupplementaryGroups = [ "systemd-journal" ];
        WorkingDirectory = stateDir;
        EnvironmentFile = cfg.environmentFiles;
        ExecStart = execStart;
        Restart = "on-failure";
        RestartSec = 5;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ReadWritePaths = [ stateDir ];
      };
    };
  };
}
