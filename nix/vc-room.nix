# SPDX-License-Identifier: Apache-2.0
# vc-room — Nix port of images/vc-room/Dockerfile.
#
# Minimal LiveKit token mint + room UI: a Flask app (app.py + templates/)
# whose only runtime deps are flask>=3.0 and pyjwt>=2.8 (pure HMAC-SHA256
# token minting — no LiveKit API calls at runtime). The livekit-client SDK
# is vendored at BUILD time from the pinned version below and stored under
# the FIXED name the room template expects (livekit-client@2.x ships only
# the unminified .umd.js on unpkg — exactly the Dockerfile's fallback
# branch), so browsers get it from this app's /static, never from a CDN.
#
# Container posture mirrors compose (services: vc-room drops ALL caps and
# sets read_only + user "1000:1000"):
#   - image default user 1000:1000 (unprivileged)
#   - NO setuid/setgid binaries anywhere in the closure (the static suite
#     flags setuid binaries in an image that drops ALL caps)
#   - nothing written at runtime (read-only rootfs friendly)
{
  pkgs,
  lib,
}: let
  version = "1.0.0";
  livekitClientVersion = "2.23.0";

  # Python 3.12 (Dockerfile base python:3.12-slim) with the two runtime deps.
  pythonEnv = pkgs.python312.withPackages (ps: [
    ps.flask
    ps.pyjwt
  ]);

  # The pinned livekit-client SDK file from unpkg (same pinned file the
  # Dockerfile vendors; .umd.min.js 404s for 2.23.0, so this IS the file the
  # Dockerfile's fallback branch stores).
  livekitClient = pkgs.fetchurl {
    url = "https://unpkg.com/livekit-client@${livekitClientVersion}/dist/livekit-client.umd.js";
    hash = "sha256-+2RvImDYT5EjjQotwQcmxFZjHWomx8r8Q8/uWVm00x0=";
  };

  # Lay the app out exactly where the Dockerfile puts it: /app/app.py,
  # /app/templates/, /app/static/livekit-client.umd.min.js (fixed name).
  app = pkgs.runCommand "vc-room-app-${version}" {} ''
    mkdir -p $out/app/static
    install -m 0555 ${../images/vc-room/app.py} $out/app/app.py
    cp -r ${../images/vc-room/templates} $out/app/templates
    install -m 0444 ${livekitClient} $out/app/static/livekit-client.umd.min.js
  '';

  # /bin/sh + a small applet set (busybox multi-call binary — no setuid
  # bits; python:3.12-slim ships a shell, so the posture mirror includes
  # one for docker-exec operations) PLUS python on the standard PATH:
  # the compose healthcheck runs ["CMD","python","-c",… urllib /healthz].
  shkit = pkgs.runCommand "vc-room-shkit" {} ''
    mkdir -p $out/bin
    ln -s ${pythonEnv}/bin/python $out/bin/python
    for app in sh env ls mkdir id uname sleep date head tail grep cat echo \
               printf wc touch dirname basename readlink; do
      ln -s ${pkgs.busybox}/bin/busybox $out/bin/$app
    done
  '';
in
pkgs.dockerTools.streamLayeredImage {
  name = "vc-room";
  tag = version;

  contents = [
    app # /app (app.py + templates + vendored SDK)
    pythonEnv # flask + pyjwt, python3.12
    pkgs.cacert # /etc/ssl/certs/ca-bundle.crt
    shkit # /bin/sh + a few applets (busybox, non-setuid)
  ];

  config = {
    User = "1000:1000";
    Env = [
      "FLASK_APP=/app/app.py"
      "PATH=/bin"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
    ];
    WorkingDir = "/app";
    ExposedPorts = {
      "8090/tcp" = {};
    };
    # Dockerfile CMD verbatim.
    Cmd = [
      "${pythonEnv}/bin/python"
      "-c"
      "from app import app; app.run(host='0.0.0.0', port=8090)"
    ];
  };

  # Nothing written at runtime: compose runs this read_only with a /tmp
  # tmpfs and a /healthz probe (defined in the compose service, which
  # overrides whatever the image declares).

  maxLayers = 20;
}
