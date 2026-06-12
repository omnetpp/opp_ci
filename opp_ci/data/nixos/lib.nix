# opp_ci NixOS helper functions.
#
# Shared by coordinator.nix and worker.nix. The translation from a typed
# module option to the OPP_CI_* environment variable the app reads is purely
# mechanical, so it lives here once.
{ lib }:
rec {
  # Coerce a Nix scalar to the string systemd's `environment = {…}` wants.
  #   bool -> "1" / "0"   (matches config.py's `== "1"` checks)
  #   int  -> decimal string
  #   list -> comma-joined (matches config.py's split(",") parsing)
  #   str  -> itself
  toEnvValue = v:
    if builtins.isBool v then (if v then "1" else "0")
    else if builtins.isInt v then builtins.toString v
    else if builtins.isList v then lib.concatStringsSep "," v
    else builtins.toString v;

  # Build an attrset of { OPP_CI_FOO = "string"; … } from a freeform settings
  # attrset, dropping null values (so an unset key falls through to the app's
  # own default in config.py).
  renderSettings = settings:
    lib.mapAttrs (_: toEnvValue)
      (lib.filterAttrs (_: v: v != null) settings);

  # The freeform `settings` option type: OPP_CI_* -> str | int | bool |
  # listOf str (or null to leave unset). The RFC42-style escape hatch for the
  # long tail of OPP_CI_* vars not promoted to named options.
  settingsType = lib.types.attrsOf
    (lib.types.nullOr (lib.types.oneOf [
      lib.types.str
      lib.types.int
      lib.types.bool
      (lib.types.listOf lib.types.str)
    ]));
}
