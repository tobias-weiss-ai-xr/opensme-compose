# SPDX-License-Identifier: Apache-2.0
# Nix flake for building the openSME opencloud Docker image.
#
# Build (run from THIS directory, or see nix/README.md):
#   . /home/weiss/.nix-profile/etc/profile.d/nix.sh
#   nix build .#opencloud
#   docker load < result
#   docker tag opencloud:9.1.0-nix-flake ghcr.io/tobias-weiss-ai-xr/opencloud:9.1.0-nix-flake
#
# Pattern follows monitoring/nix/flake.nix (nixpkgs nixos-25.05 + flake-utils).

{
  description = "openSME opencloud Docker image (Nix-built, reproducible, pinned release)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
      in
      {
        packages = {
          # Pinned stable OpenCloud release as a Docker image (release-binary
          # based, hostPlatform-aware: amd64 asset on x86_64, arm64 on aarch64).
          opencloud = pkgs.callPackage ./opencloud.nix { };

          default = self.packages.${system}.opencloud;
        };

        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            nix
            docker
            jq
            git
            curl
          ];
        };
      });
}
