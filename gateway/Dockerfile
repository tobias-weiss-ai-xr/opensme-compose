FROM rust:1.98-slim-bookworm AS builder
WORKDIR /app
# Root build context: the gateway includes ../portal/src/events.rs as a module.
COPY portal/src/events.rs portal/src/events.rs
COPY gateway gateway
WORKDIR /app/gateway
RUN cargo build --release --bin opensme-events-gateway

FROM gcr.io/distroless/cc-debian12 AS runtime
COPY --from=builder /app/gateway/target/release/opensme-events-gateway /opensme-events-gateway
USER 1000:1000
EXPOSE 8081
CMD ["/opensme-events-gateway"]
