# SPDX-License-Identifier: Apache-2.0
# portal — Nix port of portal/Dockerfile (Rust axum gateway for openSME).
#
# portal/ is a self-contained Rust crate (own Cargo.lock; the repo-root
# go.mod is unrelated). The Dockerfile builds --release --bin opensme-portal
# and runs it from a distroless runtime: binary at /opensme-portal,
# USER 1000:1000, /data pre-created and owned by the runtime user (named
# volumes seed their ownership from the image on first mount), port 8080.
# The Dockerfile's busybox sidecar (for debugging on a shell-less runtime)
# is mirrored with busybox applets on the standard /bin PATH.
{
  pkgs,
  lib,
  srcRoot, # flake source root (repo root containing portal/)
  rustPkgs, # newer nixpkgs for the Rust toolchain (25.05 rustc is below MSRV)
}: let
  version = "0.1.0";

  portal = rustPkgs.rustPlatform.buildRustPackage {
    pname = "opensme-portal";
    inherit version;
    src = srcRoot + "/portal";
    # portal is its own crate with its own Cargo.lock; cargoLock-based
    # vendoring is deterministic from the committed lockfile — no cargoHash
    # fix cycle needed (crates.io deps only).
    cargoLock.lockFile = ../portal/Cargo.lock;
    # Image builds don't run the test suite (dev-deps like proptest would
    # add a lot of build time); `cargo test` inside portal/ stays the
    # test gate.
    doCheck = false;
  };

  # Mirror the Dockerfile layout: binary at /opensme-portal (0555).
  portalPkg = pkgs.runCommand "opensme-portal-img-${version}" {} ''
    mkdir -p $out
    install -m 0555 ${portal}/bin/opensme-portal $out/opensme-portal
  '';

  # busybox applets on /bin (the Dockerfile ships busybox next to the
  # distroless runtime for operational/debug access) PLUS the binary at
  # the Dockerfile's exact /busybox path — the compose healthcheck runs
  # ["/busybox","wget",…,"http://127.0.0.1:8080/health"].
  shkit = pkgs.runCommand "portal-shkit" {} ''
    mkdir -p $out/bin
    install -m 0555 ${pkgs.busybox}/bin/busybox $out/busybox
    for app in sh env ls cp mv rm mkdir chmod id uname sleep date head tail \
               grep find wc touch dirname basename readlink cat echo printf; do
      ln -s ${pkgs.busybox}/bin/busybox $out/bin/$app
    done
  '';
in
pkgs.dockerTools.streamLayeredImage {
  name = "portal";
  tag = version;

  contents = [
    portalPkg # /opensme-portal
    pkgs.cacert # CA bundle (distroless/cc ships one; OIDC/HTTPS callers need it)
    shkit # /bin applets
  ];

  config = {
    User = "1000:1000";
    Env = [
      "PATH=/bin"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
    ];
    ExposedPorts = {
      "8080/tcp" = {};
    };
    Cmd = ["/opensme-portal"];
  };

  # Pre-create the team-notes data dir owned by the runtime user, exactly
  # like the Dockerfile's COPY --chown=1000:1000 /data: named volumes seed
  # their content (incl. ownership) from the image on first mount.
  fakeRootCommands = ''
    mkdir -p data/attachments
    chown 1000:1000 data data/attachments
  '';
  enableFakechroot = true;

  maxLayers = 20;
}
