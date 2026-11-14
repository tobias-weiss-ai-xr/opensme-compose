# SPDX-License-Identifier: Apache-2.0
# opencloud — Nix-built Docker image of OpenCloud (opencloud-eu), pinned to a
# STABLE upstream release (never a rolling tag, never an rc prerelease).
#
# The image mirrors what opencloud/opencloud.yml expects from the upstream
# image (the compose file overrides the entrypoint with the host-mounted
# opencloud/opencloud-entrypoint.sh, which needs a POSIX shell + sed/tr/cat
# and calls the bare `opencloud` binary):
#   - binary at /usr/bin/opencloud (mode 0555; upstream image ENTRYPOINT)
#   - curl in PATH (compose healthcheck: curl -f http://localhost:9200/health/ready)
#   - /bin/sh + sed/tr/cat for the mounted /entrypoint.sh
#   - CA certs at the standard path (ocis validates the Zitadel IdP over HTTPS)
#   - writable /etc/opencloud (config) and /var/lib/opencloud (state)
#
# Release assets are single static Go binaries published per architecture
# with .sha256 sidecars. This expression is hostPlatform-aware: it fetches
# the amd64 asset on x86_64-linux and the arm64 asset on aarch64-linux, so
# `nix build .#opencloud` natively builds each architecture (an arm CI task
# builds the same flake on an arm runner).
#
# Hashes are the upstream v9.1.0 release .sha256 sidecars, converted to SRI:
#   opencloud-9.1.0-linux-amd64  183d95c3a77ccb520dabaa106e08d1037afcaa8af6403c22eb6ac9a565de777e
#   opencloud-9.1.0-linux-arm64  dd31554a1e092e352a229570ac8d6bc25ce70f641cf522fc5c3eb3d3b1381f10
{
  pkgs,
  lib,
}: let
  version = "9.1.0";
  baseUrl = "https://github.com/opencloud-eu/opencloud/releases/download/v${version}";

  asset =
    if pkgs.stdenv.hostPlatform.isx86_64
    then {
      url = "${baseUrl}/opencloud-${version}-linux-amd64";
      hash = "sha256-GD2Vw6d8y1INq6oQbgjRA3r8qor2QDwi62rJpWXed34=";
    }
    else if pkgs.stdenv.hostPlatform.isAarch64
    then {
      url = "${baseUrl}/opencloud-${version}-linux-arm64";
      hash = "sha256-3TFVSh4JLjUqIpVwrI1rwlznD2Qc9SL8XD6z07E4HxA=";
    }
    else throw "opencloud: unsupported hostPlatform ${pkgs.stdenv.hostPlatform.system}";

  opencloudBin = pkgs.fetchurl {
    inherit (asset) url hash;
  };

  # Lay the release binary out at /usr/bin/opencloud (mode 0555: readable +
  # executable, not writable). dockerTools strips the store prefix, so this
  # surfaces in the image exactly where the upstream image puts it.
  opencloudPkg = pkgs.runCommand "opencloud-${version}" {} ''
    mkdir -p $out/usr/bin
    install -m 0555 ${opencloudBin} $out/usr/bin/opencloud
  '';

  # pkgs.busybox ships a single multi-call binary without applet symlinks;
  # provide /bin/sh plus the applets the mounted entrypoint.sh and general
  # container operation rely on (busybox resolves applets via argv[0]).
  shkit = pkgs.runCommand "opencloud-shkit" {} ''
    mkdir -p $out/bin
    for app in sh sed tr cat echo printf ls cp mv rm mkdir rmdir chmod \
               chown id uname env sleep date head tail grep egrep \
               find xargs wc touch dirname basename readlink; do
      ln -s ${pkgs.busybox}/bin/busybox $out/bin/$app
    done
  '';

  # Real /etc/passwd + /etc/group with a dedicated non-root user. The compose
  # stack overrides this via `user: "0:0"`; a standalone `docker run` of the
  # image gets the non-root default.
  etcFiles = pkgs.runCommand "opencloud-etc" {} ''
    mkdir -p $out/etc
    cat > $out/etc/passwd <<'EOF'
root:x:0:0:root:/root:/bin/false
opencloud:x:1000:1000:opencloud:/var/lib/opencloud:/bin/false
nobody:x:65534:65534:nobody:/:/bin/false
EOF
    cat > $out/etc/group <<'EOF'
root:x:0:
opencloud:x:1000:
nobody:x:65534:
EOF
  '';
in
pkgs.dockerTools.streamLayeredImage {
  name = "opencloud";
  tag = "${version}-nix-flake";

  contents = [
    opencloudPkg     # /usr/bin/opencloud
    pkgs.cacert      # /etc/ssl/certs/ca-bundle.crt (standard CA path)
    pkgs.curl        # compose healthcheck
    shkit            # /bin/sh + core applets for /entrypoint.sh
    etcFiles         # /etc/passwd + /etc/group
  ];

  config = {
    User = "1000:1000";
    Entrypoint = ["/usr/bin/opencloud"];
    Cmd = ["server"];
    Env = [
      "PATH=/usr/bin:/bin:/usr/sbin:/sbin"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "SSL_CERT_DIR=/etc/ssl/certs"
    ];
    WorkingDir = "/";
    ExposedPorts = {
      "9200/tcp" = {};
    };
  };

  # Writable config/state dirs (compose mounts named volumes over them:
  # opencloud-config -> /etc/opencloud, opencloud-lib -> /var/lib/opencloud,
  # opencloud-data -> /var/lib/opencloud/data), owned by the non-root user.
  # NOTE: dockerTools lays contents out as SYMLINKS into /nix/store — docker
  # >= 29 `exec` walks /etc/passwd + /etc/group with openat2 RESOLVE_BENEATH
  # and fails across the layer boundary ("openat etc/group: path escapes
  # from parent"). Materialize them as REAL files (enableFakechroot makes
  # the symlinks readable here).
  fakeRootCommands = ''
    mkdir -p etc/opencloud var/lib/opencloud/data
    chown 1000:1000 etc/opencloud var/lib/opencloud var/lib/opencloud/data
    cat etc/passwd > etc/passwd.tmp && rm -f etc/passwd \
      && mv etc/passwd.tmp etc/passwd && chmod 0444 etc/passwd
    cat etc/group > etc/group.tmp && rm -f etc/group \
      && mv etc/group.tmp etc/group && chmod 0444 etc/group
  '';
  enableFakechroot = true;

  maxLayers = 30;
}
