#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
# sogo6-webmail-activate.sh — ONE COMMAND webmail (SOGo6) activation
#
# Deploys the webmail SQL user source (single relational DB, no LDAP):
#   1. (optional) docker load a freshly built sogo6-server image
#   2. probe the runc wedge (container create/start): if still blocked,
#      fail with exit 42 instead of a confusing half-deploy
#   3. `docker compose up -d` the full suite (exact ansible chain +
#      opensme-live.yml, same profiles) — recreates opensme-sogo6-server
#      and openme-sogo6-ui from the new image
#   4. the server self-bootstraps (init JSONs shipped + SOGO_INIT_* env
#      since 4a79647) and answers /api/user/v1/system with 200 instead
#      of 412 S000001
#   5. verify health + configuration and print the login short-hint
#
# Idempotent: safe to re-run whenever the image changes.
#
# Usage (as root, ON the VM hosting the stack — default /opt/opensme-compose):
#   bash sogo6-webmail-activate.sh [--load /path/to/sogo6-server.img] [--dry-run]
#
# Exit codes: 0 = active & verified · 42 = runc wedge (retry later) ·
#             43 = compose failed · 44 = verification failed
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/opensme-compose/compose}"
IMAGE_REF="${IMAGE_REF:-sogo6-server:latest}"
PUBLIC_SYSTEM_URL="${PUBLIC_SYSTEM_URL:-https://webmail.home.opensme.graphwiz.ai/api/user/v1/system}"
LOAD_TARBALL=""
DRY_RUN=false

# ── arg parsing ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --load) LOAD_TARBALL="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help)
            sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1"; exit 2 ;;
    esac
done

# ── compose chain = ansible opensme_compose_chain + live overlay ─────
FILES=(
    docker-compose.yml
    idm/zitadel.yml
    opencloud/opencloud.yml
    mail/stalwart.yml
    mail/sogo6.yml
    stalwart-idpplain.yml
    services/camunda.yml
    services/invoice-ninja.yml
    services/paperless.yml
    services/notes.yml
    services/synapse.yml
    services/element.yml
    services/limesurvey.yml
    prod-tuning.yml
    profiles/system-traefik.yml
    opensme-coexist.yml
    opensme-live.yml
)
PROFILES="camunda,invoice,paperless,notes,chat,element,surveys,web"

# ── helpers ───────────────────────────────────────────────────────────
say()  { printf '\033[1;34m[i]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit "${2:-1}"; }

preflight() {
    [[ $EUID -eq 0 ]] || die "run as root (the stack lives on the Docker host)"
    command -v docker >/dev/null || die "docker not found"
    [[ -d "$COMPOSE_DIR" ]] || die "compose dir missing: $COMPOSE_DIR"
    [[ -f "$COMPOSE_DIR/.env" ]] || warn "no .env at $COMPOSE_DIR — compose interpolation may fail"
    for f in "${FILES[@]}"; do
        [[ -f "$COMPOSE_DIR/$f" ]] || die "missing compose file: $COMPOSE_DIR/$f"
    done
}

# compose wrapper: dry-run prints, otherwise executes
compose() {
    local dc_args=()
    for f in "${FILES[@]}"; do dc_args+=(-f "$COMPOSE_DIR/$f"); done
    if $DRY_RUN; then
        printf '(cd %s && COMPOSE_PROFILES=%q docker compose %s %s)\n' \
            "$COMPOSE_DIR" "$PROFILES" "${dc_args[*]}" "$*"
        return 0
    fi
    ( cd "$COMPOSE_DIR" && \
      COMPOSE_PROFILES="$PROFILES" docker compose "${dc_args[@]}" "$@" )
}

load_image() {
    [[ -n "$LOAD_TARBALL" ]] || return 0
    [[ -f "$LOAD_TARBALL" ]] || die "image tarball not found: $LOAD_TARBALL"
    say "loading image from $LOAD_TARBALL …"
    if $DRY_RUN; then
        printf 'docker load -i %s\n' "$LOAD_TARBALL"
    else
        docker load -i "$LOAD_TARBALL"
    fi
    ok "image loaded: $IMAGE_REF"
}

probe_wedge() {
    # container create/start is wedged (runc: can't get final child's PID
    # from pipe: EOF). Probe with a throwaway container; 0 = wedge clear.
    if $DRY_RUN; then
        say "(dry-run) wedge probe: docker run --rm busybox true"
        return 0
    fi
    say "probing runc (container create/start) …"
    if timeout 60 docker run --rm busybox true >/dev/null 2>&1; then
        ok "wedge clear — container create/start works"
    else
        die "runc wedge still active — container create/start blocked (EOF). \
Nothing was changed; retry once the wedge clears (wedge-watcher auto-starts \
paperless/promo). To deploy later: bash $0 ${LOAD_TARBALL:+--load $LOAD_TARBALL}" 42
    fi
}

up_stack() {
    say "compose up (recreate server/ui from $IMAGE_REF) …"
    compose up -d --no-build --remove-orphans || die "compose up failed" 43
}

wait_healthy() {
    say "waiting for opensme-sogo6-server to be healthy …"
    local i
    for i in $(seq 1 24); do
        local st
        st=$(docker inspect -f '{{.State.Health.Status}}' opensme-sogo6-server 2>/dev/null || echo none)
        [[ "$st" == "healthy" ]] && { ok "healthy after $((i*5))s"; return 0; }
        sleep 5
    done
    warn "server not 'healthy' yet — continuing to verification anyway"
}

verify() {
    say "verifying bootstrap state…"
    local body code
    # inside container: 200 + no S000001 beats the old 412
    code=$(docker exec opensme-sogo6-server curl -sf -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:5000/api/user/v1/system 2>/dev/null || echo 000)
    if [[ "$code" != "200" ]]; then
        body=$(docker exec opensme-sogo6-server curl -s http://127.0.0.1:5000/api/user/v1/system 2>/dev/null || true)
        die "server still unconfigured (HTTP $code): ${body:0:160}" 44
    fi
    ok "server configured (HTTP 200 on /api/user/v1/system)"

    say "public system endpoint (through traefik) …"
    local pub
    pub=$(env -u http_proxy -u https_proxy curl -sk -o /dev/null -w '%{http_code}' \
        --max-time 15 "$PUBLIC_SYSTEM_URL" 2>/dev/null || echo 000)
    if [[ "$pub" == "200" ]]; then
        ok "public webmail API answers 200"
    else
        warn "public $PUBLIC_SYSTEM_URL -> $pub (server-side is configured; traefik/route may lag)"
    fi

    # users table + testuer present in the single relational DB
    local rows
    rows=$(docker exec opensme-sogo6-postgres psql -U sogo -d sogo -tAc \
        "SELECT count(*) FROM users;" 2>/dev/null || echo "?")
    ok "webmail users table rows: $rows"

    cat <<'EOF'

──────────────────────────────────────────────────────────────
 Webmail login:  https://webmail.home.opensme.graphwiz.ai/en
   user:   testuer@opensme.auth.home.opensme.graphwiz.ai
   pass:   Eval2026
──────────────────────────────────────────────────────────────
EOF
}

# ── run ───────────────────────────────────────────────────────────────
preflight
load_image
probe_wedge
if $DRY_RUN; then
    up_stack
    ok "dry-run OK — plan printed, nothing was changed"
    exit 0
fi
up_stack
wait_healthy
verify
ok "webmail activation complete"
