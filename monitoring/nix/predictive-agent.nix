# SPDX-License-Identifier: Apache-2.0
# predictive-agent — Predictive container health monitor (Docker, not K8s)
#
# Uses Kalman filters, Markov chains, and Bayesian risk scoring to predict
# container failures before they happen. Monitors Docker containers via
# `docker stats` / `docker inspect` instead of `kubectl top pods`.
#
# Build:
#   nix-build monitoring/nix/predictive-agent.nix -o result-predictive-agent
#   docker load < result-predictive-agent
#   docker tag predictive-agent:latest-nix ghcr.io/tobias-weiss-ai-xr/predictive-agent:latest

{ pkgs ? import <nixpkgs> { system = "x86_64-linux"; } }:

let
  entrypointSh = pkgs.writeText "entrypoint.sh" (builtins.readFile ./predictive-agent-files/entrypoint.sh);
  healthcheckSh = pkgs.writeText "healthcheck.sh" (builtins.readFile ./predictive-agent-files/healthcheck.sh);

  # The predictive_agent package (stdlib-only Python, copied locally)
  predictiveAgentPackage = pkgs.runCommand "predictive-agent-package" {} ''
    mkdir -p $out/opt/predictive-agent/predictive_agent
    cp ${./predictive-agent/predictive_agent}/*.py $out/opt/predictive-agent/predictive_agent/
    cp ${entrypointSh} $out/opt/predictive-agent/entrypoint.sh
    chmod +x $out/opt/predictive-agent/entrypoint.sh
    cp ${healthcheckSh} $out/opt/predictive-agent/healthcheck.sh
    chmod +x $out/opt/predictive-agent/healthcheck.sh
  '';

  etcFiles = pkgs.runCommand "predictive-agent-etc" {} ''
    mkdir -p $out/etc
    echo 'root:x:0:0:root:/root:/bin/bash' > $out/etc/passwd
    echo 'opensme:x:1000:1000:opensme:/home/opensme:/bin/bash' >> $out/etc/passwd
    echo 'nobody:x:65534:65534:nobody:/:/sbin/nologin' >> $out/etc/passwd
    echo 'root:x:0:' > $out/etc/group
    echo 'opensme:x:1000:' >> $out/etc/group
    echo 'nobody:x:65534:' >> $out/etc/group
  '';

in
pkgs.dockerTools.buildLayeredImage {
  name = "predictive-agent";
  tag = "latest-nix";

  contents = with pkgs; [
    python3
    curl
    bash
    coreutils
    gnugrep
    gnused
    procps
    cacert
    docker
    predictiveAgentPackage
    etcFiles
  ];

  config = {
    User = "0:0";
    WorkingDir = "/opt/predictive-agent";
    Entrypoint = [
      "${pkgs.bash}/bin/bash"
      "/opt/predictive-agent/entrypoint.sh"
    ];
    Cmd = [];
    Env = [
      "OPERATOR_VERSION=4.0.0-docker"
      "OPERATOR_NAME=opensme-predictive-agent"
      "OPERATOR_NAMESPACE=opensme"
      "OPERATOR_WATCH_NAMESPACES=opensme,default"
      "LLM_BACKEND=ollama"
      "OLLAMA_URL=http://ollama:11434"
      "OLLAMA_MODEL=qwen3-30b-a3b:latest"
      "RECONCILE_INTERVAL=60"
      "OPERATOR_METRICS_BIND_ADDRESS=0.0.0.0:8080"
      "OPERATOR_HEALTH_PROBE_BIND_ADDRESS=0.0.0.0:8081"
      "PYTHONPATH=/opt/predictive-agent"
      "PATH=${pkgs.python3}/bin:${pkgs.curl}/bin:${pkgs.bash}/bin:${pkgs.coreutils}/bin:${pkgs.gnugrep}/bin:${pkgs.gnused}/bin:${pkgs.procps}/bin:${pkgs.docker}/bin"
      "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
      "HOME=/home/opensme"
    ];
    ExposedPorts = {
      "8080/tcp" = {};
      "8081/tcp" = {};
    };
  };

  # Nix dockerTools symlinks /etc/{passwd,group} into /nix/store. Some
  # container runtimes (e.g. the openedusuite.org host) reject `docker exec`
  # and compose healthchecks with "openat etc/group: path escapes from parent".
  # Replace those symlinks with regular files so exec/healthchecks work.
  extraCommands = ''
    rm -f etc/group etc/passwd
    cp ${etcFiles}/etc/group etc/group
    cp ${etcFiles}/etc/passwd etc/passwd
  '';

  maxLayers = 50;
}
