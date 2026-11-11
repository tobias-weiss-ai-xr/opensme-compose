ARG RUST_VERSION=1.98
FROM rust:${RUST_VERSION}-slim-bookworm AS chef
RUN cargo install cargo-chef --locked
# The build context is the REPO ROOT: portal/Cargo.toml depends on the
# shared ../services/rust-webdav-client crate, which a ./portal-only
# context cannot resolve (cargo chef fails on the missing path dep). The
# checkout lands at /app, the package lives at /app/portal.
WORKDIR /app/portal

FROM chef AS planner
COPY . /app
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
# data dir for the runtime stage (team-notes persistence); ownership is
# applied on COPY --chown in the runtime stage
RUN mkdir -p /data/attachments
# The recipe records the ../services/rust-webdav-client PATH dependency,
# so its manifest (and sources) must exist before `cargo chef cook` reads it.
COPY services/rust-webdav-client /app/services/rust-webdav-client
COPY --from=planner /app/portal/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json
COPY . /app
RUN cargo build --release --bin opensme-portal

FROM gcr.io/distroless/cc-debian12 AS runtime
# musl-static busybox — the default busybox image is glibc-dynamic and
# needs a newer glibc than debian12 ships
COPY --from=busybox:1.36-musl /bin/busybox /busybox
ENV PATH=/busybin:$PATH
COPY --from=builder /app/portal/target/release/opensme-portal /opensme-portal
# Pre-create the team-notes data dir owned by the runtime user: named
# volumes seed their content (incl. ownership) from the image on first
# mount, so /data stays writable without a root entrypoint.
COPY --from=builder --chown=1000:1000 /data /data
USER 1000:1000
EXPOSE 8080
CMD ["/opensme-portal"]
