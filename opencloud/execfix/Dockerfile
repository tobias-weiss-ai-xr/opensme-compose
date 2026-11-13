# Exec/healthcheck fix layer for the nix-built opencloud image.
#
# docker >= 29 reads /etc/passwd + /etc/group via openat2-in-root when it
# execs into a container (healthcheck, docker exec). The nix image symlinks
# both files into /nix/store, which openat2 rejects ("path escapes from
# parent") — every exec fails, so the healthcheck never runs and the
# container reports unhealthy while the app itself works fine.
#
# This layer materializes the SAME file contents as real files. COPY only —
# no RUN: RUN would itself need exec inside the broken base image.
#
# Rebuild when the base image is re-pushed:
#   cd opencloud/execfix && docker buildx build --network host \
#     -t opensme-opencloud:9.1.0-nix-execfix .
FROM registry.openedusuite.org/openedusuite-nix/opencloud:9.1.0-nix
COPY passwd group /etc/
