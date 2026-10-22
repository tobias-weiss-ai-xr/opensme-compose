# dev-maintenance-bot — multi-stage build.
# Build context is the REPO ROOT (go.mod + opensme-knowledge live there):
#   docker build -f opensme-dev-agent/Dockerfile -t opensme-dev-maintenance-bot:latest .
FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
COPY internal ./internal
COPY opensme-knowledge ./opensme-knowledge
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/agent .

FROM alpine:3.20
# docker-cli: the checker/healer shell out to the docker CLI over the
# read-only socket — no SDK, works on any base image.
RUN apk add --no-cache docker-cli ca-certificates tzdata
COPY --from=build /out/agent /usr/local/bin/agent
# Runs as root: the docker socket is root:docker 0660 and the checker shells
# out to the docker CLI. The read-only allowlist in the code (checker/healer
# runners only ever invoke ps/inspect/logs/stats + consent-gated restart)
# is what enforces read-only behavior — a :ro socket mount alone cannot.
ENTRYPOINT ["/usr/local/bin/agent"]
# One-shot default: reconcile once, exit cleanly (compose restart: "no").
CMD ["-once"]
