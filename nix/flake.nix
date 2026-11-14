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
    # Rust toolchain input: the locked nixos-25.05 rustc is below the MSRV of locked portal/calrs deps.
    nixpkgs-rust.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs, nixpkgs-rust, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        rustPkgs = import nixpkgs-rust { inherit system; };
      in
      {
        packages = {
          # Pinned stable OpenCloud release as a Docker image (release-binary
          # based, hostPlatform-aware: amd64 asset on x86_64, arm64 on aarch64).
          opencloud = pkgs.callPackage ./opencloud.nix { };

          # Owned app images (self-contained in this flake — build with
          # `cd nix/ && nix build .#vc-room .#portal .#calrs`, the form the
          # campaign gate runs).
          vc-room = pkgs.callPackage ./vc-room.nix { };
          portal = pkgs.callPackage ./portal.nix { srcRoot = ./..; inherit rustPkgs; };
          calrs = pkgs.callPackage ./calrs.nix { inherit rustPkgs; };

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
