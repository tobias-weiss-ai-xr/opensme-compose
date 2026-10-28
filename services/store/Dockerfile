# RaisFast — pinned release artifact (no upstream registry image as of v0.4.2).
# musl static binary on alpine: wget doubles as the healthcheck binary,
# ca-certificates lets reqwest do HTTPS (payments/OAuth callbacks).
# RAISFAST_ARCH: x86_64 (amd64) or aarch64 (arm64).
FROM debian:bookworm-slim AS fetch
RUN apt-get update >/dev/null && apt-get install -y --no-install-recommends xz-utils && rm -rf /var/lib/apt/lists/*
ARG RAISFAST_VERSION=0.4.2
ARG RAISFAST_ARCH=x86_64
ADD https://github.com/RaisFast/raisfast/releases/download/v${RAISFAST_VERSION}/raisfast-${RAISFAST_ARCH}-unknown-linux-musl.tar.xz /tmp/raisfast-${RAISFAST_ARCH}-unknown-linux-musl.tar.xz
ADD https://github.com/RaisFast/raisfast/releases/download/v${RAISFAST_VERSION}/raisfast-${RAISFAST_ARCH}-unknown-linux-musl.tar.xz.sha256 /tmp/raisfast-${RAISFAST_ARCH}-unknown-linux-musl.tar.xz.sha256
# both ADDs keep the URL basenames, which is exactly what the published
# .sha256 references — so plain `sha256sum -c` verifies.
RUN cd /tmp \
    && sha256sum -c raisfast-${RAISFAST_ARCH}-unknown-linux-musl.tar.xz.sha256 \
    && mkdir out && tar -xJf raisfast-${RAISFAST_ARCH}-unknown-linux-musl.tar.xz -C out \
    && BIN="$(find out -type f -name 'raisfast' | head -1)" \
    && test -n "$BIN" \
    && install -m 0755 "$BIN" /usr/local/bin/raisfast

FROM alpine:3.24
RUN apk add --no-cache ca-certificates wget
COPY --from=fetch /usr/local/bin/raisfast /raisfast
# SQLite database + uploads live under /data (single-dir contract)
VOLUME /data
EXPOSE 9898
ENTRYPOINT ["/raisfast"]
