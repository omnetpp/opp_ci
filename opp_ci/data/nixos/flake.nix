# opp_ci NixOS modules, exposed as a flake for flake-based configs.
#
# In your flake:
#   inputs.opp_ci.url = "path:/etc/nixos/opp_ci";   # or a git URL
#   # then in your nixosSystem modules:
#   opp_ci.nixosModules.coordinator
#   opp_ci.nixosModules.worker
{
  description = "opp_ci NixOS modules (coordinator + worker)";

  outputs = { self, ... }: {
    nixosModules.coordinator = ./coordinator.nix;
    nixosModules.worker = ./worker.nix;
    nixosModules.default = { imports = [ ./coordinator.nix ./worker.nix ]; };
  };
}
