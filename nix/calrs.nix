# SPDX-License-Identifier: Apache-2.0
# calrs — Nix build of the openSME calendar image (upstream olivierlambert/calrs).
#
# Unlike images/calrs/Dockerfile (a documented pinned reference plus our
# runtime delta — the source tree is NOT vendored in this repo), this
# expression builds from UPSTREAM source: github.com/olivierlambert/calrs at
# the v1.6.0 tag, with ALL repo patches images/calrs/patches/0001-*.patch
# through 0005-*.patch applied in order (same recipe as /root/build/calrs
# and docs/calendar.md).
#
# Runtime layout mirrors the Dockerfile:
#   - binary at /usr/local/bin/calrs (0555)
#   - templates at /opt/calrs/templates/ (WorkingDir /opt/calrs)
#   - unprivileged user, CALRS_DATA_DIR=/var/lib/calrs (writable state dir,
#     compose mounts the calrs-data named volume over it)
#   - curl on PATH for the compose healthcheck (:3000 probe, calrs has no
#     /health route), ca-certificates for OIDC/CalDAV/SMTP TLS
#   - Entrypoint ["calrs"], Cmd ["serve","--host","0.0.0.0","--port","3000"]
{
  pkgs,
  lib,
  rustPkgs, # newer nixpkgs for the Rust toolchain (25.05 rustc is below MSRV)
}: let
  version = "1.6.0";

  # Upstream source at the pinned tag with our patch series applied in
  # order (0001 -> 0005, see docs/calendar.md). applyPatches output is also
  # the templates/ source for the image layer (patch 0005 changes
  # templates/base.html — the branding footer).
  upstream = pkgs.applyPatches {
    src = pkgs.fetchFromGitHub {
      owner = "olivierlambert";
      repo = "calrs";
      rev = "v${version}";
      hash = "sha256-4Ua3sjXy42Rd0fBDJUVJx0xE4ogbrQ1fhvqamdURPXg=";
    };
    patches = [
      ../images/calrs/patches/0001-oidc-zitadel-compat.patch
      ../images/calrs/patches/0002-caldav-radicale-resourcetype.patch
      ../images/calrs/patches/0003-livekit-room-url.patch
      ../images/calrs/patches/0004-smtp-selfsigned-relay.patch
      ../images/calrs/patches/0005-branding-footer.patch
    ];
  };

  calrs = rustPkgs.rustPlatform.buildRustPackage {
    pname = "calrs";
    inherit version;
    src = upstream;
    # Upstream ships Cargo.lock at v1.6.0 and none of the patches touch
    # Cargo files: cargoLock-based vendoring is deterministic from that
    # lockfile — no cargoHash fix cycle needed (crates.io deps only).
    cargoLock.lockFile = "${upstream}/Cargo.lock";
    doCheck = false; # image build only; upstream tests are not the gate
    # sqlx/sqlite resolves the SYSTEM libsqlite3 (lock pins libsqlite3-sys
    # without "bundled"): give pkg-config the nixpkgs sqlite so
    # `-lsqlite3` links against the store library.
    nativeBuildInputs = [ rustPkgs.pkg-config ];
    buildInputs = [ rustPkgs.sqlite ];
  };

  # Mirror the Dockerfile runtime layout.
  calrsPkg = pkgs.runCommand "calrs-img-${version}" {} ''
    mkdir -p $out/usr/local/bin $out/opt/calrs
    install -m 0555 ${calrs}/bin/calrs $out/usr/local/bin/calrs
    cp -r ${upstream}/templates $out/opt/calrs/templates
  '';

  # /bin/sh + applets (debian-slim runtime ships a shell; we ship busybox).
  shkit = pkgs.runCommand "calrs-shkit" {} ''
    mkdir -p $out/bin
    for app in sh env ls cp mv rm mkdir chmod chown id uname sleep date head \
               tail grep egrep find xargs wc touch dirname basename readlink \
               cat echo printf sed tr; do
      ln -s ${pkgs.busybox}/bin/busybox $out/bin/$app
    done
  '';

  # Real /etc/passwd + /etc/group with a dedicated non-root user
  # (Dockerfile: useradd -r -s /bin/false -m -d /var/lib/calrs calrs).
  # NOTE uid 1000: the legacy docker-built image runs `calrs` as a
  # system-range uid — switching a live volume from the legacy image to
  # this one needs a one-time chown of the calrs-data volume (see
  # nix/README.md).
  etcFiles = pkgs.runCommand "calrs-etc" {} ''
    mkdir -p $out/etc
    cat > $out/etc/passwd <<'EOF'
root:x:0:0:root:/root:/bin/false
calrs:x:1000:1000:calrs:/var/lib/calrs:/bin/false
nobody:x:65534:65534:nobody:/:/bin/false
EOF
    cat > $out/etc/group <<'EOF'
root:x:0:
calrs:x:1000:
nobody:x:65534:
EOF
  '';
in
pkgs.dockerTools.streamLayeredImage {
  name = "calrs";
  tag = "${version}-openSME";

  contents = [
    calrsPkg # /usr/local/bin/calrs + /opt/calrs/templates
    pkgs.cacert # CA bundle (OIDC discovery, CalDAV, SMTP TLS)
    pkgs.curl # compose healthcheck probe
    shkit # /bin/sh + applets
    etcFiles # /etc/passwd + /etc/group
  ];

  config = {
    User = "1000:1000";
    Entrypoint = ["/usr/local/bin/calrs"];
    Cmd = ["serve" "--host" "0.0.0.0" "--port" "3000"];
    Env = [
      "PATH=/usr/local/bin:/bin"
      "CALRS_DATA_DIR=/var/lib/calrs"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "SSL_CERT_DIR=/etc/ssl/certs"
    ];
    WorkingDir = "/opt/calrs";
    ExposedPorts = {
      "3000/tcp" = {};
    };
  };

  # Writable state dir owned by the runtime user (compose mounts the
  # calrs-data named volume over it). NOTE: docker >= 29 (exec AND
  # healthcheck) walks /etc/passwd + /etc/group with openat2 RESOLVE_BENEATH
  # and fails across the layer boundary when they are symlinks into
  # /nix/store ("openat etc/group: path escapes from parent") — materialize
  # them as REAL files (enableFakechroot makes the etcFiles content
  # readable here).
  fakeRootCommands = ''
    mkdir -p var/lib/calrs
    chown 1000:1000 var/lib/calrs
    cat etc/passwd > etc/passwd.tmp && rm -f etc/passwd \
      && mv etc/passwd.tmp etc/passwd && chmod 0444 etc/passwd
    cat etc/group > etc/group.tmp && rm -f etc/group \
      && mv etc/group.tmp etc/group && chmod 0444 etc/group
  '';
  enableFakechroot = true;

  maxLayers = 30;
}
