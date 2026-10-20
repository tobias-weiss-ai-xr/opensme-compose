#!/usr/bin/env bash
#
# scripts/provision-demo-users.sh — Provision 12 demo users across components
#
# Part of the bootstrap process: creates users for an imaginary SME
# company (OpenSME Muster GmbH) for demo and e2e testing purposes.
#
# Components (each with its OWN user store; OIDC-fed apps need no seeding):
#   zitadel    IdP — machine-PAT admin API (the classic target)
#   operaton   BPM engine — engine-rest identity + admin-webapp setup
#   synapse    Matrix homeserver — shared-secret admin register API
#   paperless  document vault — Django user model via manage.py shell
#   stalwart   mail server — Stalwart principals (admin REST API) + the
#              matching SOGo rows in Postgres (table mail_users)
#   invoice    Invoice Ninja — NOT wired (users are company-bound; the
#              demo ships a single admin seeded by its entrypoint)
#
# The same bootstrap/demo-users.json drives every component.
#
# Idempotent: running twice does NOT duplicate users.
#
# Usage:
#   ./scripts/provision-demo-users.sh                              # zitadel only (backwards compatible)
#   ./scripts/provision-demo-users.sh --component operaton          # single component
#   ./scripts/provision-demo-users.sh --component all               # every component, best effort
#   ./scripts/provision-demo-users.sh --component synapse --dry-run # show plan
#   ./scripts/provision-demo-users.sh --host ... --wait --clean ... # zitadel options (unchanged)
#
# Environment (component targets — auto-detected via docker when unset):
#   OPERATON_URL      engine base, e.g. http://opensme-camunda:8080
#   SYNAPSE_CONTAINER container name for the register API (default opensme-synapse)
#   PAPERLESS_CONTAINER container name (default opensme-paperless)
#   STALWART_CONTAINER  container name (default opensme-stalwart)
#   POSTGRES_CONTAINER  container name (default opensme-postgres)
#   ZITADEL_HOST / ZITADEL_EXTERNAL / ZITADEL_singleSignOnDomain / ZITADEL_PAT / E2E_ZITADEL_PAT
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
USERS_JSON="$REPO_ROOT/bootstrap/demo-users.json"

# ── defaults ──────────────────────────────────────────────────────────
HOST="${ZITADEL_HOST:-}"
EXTERNAL_HOST="${ZITADEL_EXTERNAL:-$HOST}"
DOMAIN="${ZITADEL_singleSignOnDomain:-opensme.local}"
WAIT=false
CLEAN=false
DRY_RUN=false
VERBOSE=false
IN_DOCKER=false
COMPONENT="zitadel"

ALL_COMPONENTS="zitadel operaton synapse paperless stalwart"

# ── argument parsing ──────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)       HOST="$2"; shift 2 ;;
        --external)   EXTERNAL_HOST="$2"; shift 2 ;;
        --domain)     DOMAIN="$2"; shift 2 ;;
        --wait)       WAIT=true; shift ;;
        --clean)      CLEAN=true; shift ;;
        --dry-run)    DRY_RUN=true; shift ;;
        --verbose|-v) VERBOSE=true; shift ;;
        --in-docker)  IN_DOCKER=true; shift ;;
        --component)  COMPONENT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [options]"
            echo "Options:"
            echo "  --component LIST   comma list: $ALL_COMPONENTS,all (default: zitadel)"
            echo "  --host HOST        Zitadel host:port (default: opensme-zitadel:8080 in docker)"
            echo "  --external HOST    External IdP hostname (default: same as --host)"
            echo "  --domain DOM       SSO domain (default: opensme.local)"
            echo "  --wait             Wait for Zitadel to be ready"
            echo "  --clean            Delete all demo users first (zitadel only)"
            echo "  --dry-run          Show plan/config and exit"
            echo "  --verbose, -v      Verbose output"
            echo "  --in-docker        Running inside docker (adjusts curl Host header)"
            echo "  --help, -h         This help"
            echo ""
            echo "Components:"
            echo "  zitadel    Zitadel IdP (machine PAT admin API)"
            echo "  operaton   Operaton/Camunda engine identity + admin-webapp setup"
            echo "  synapse    Matrix homeserver (shared-secret register API)"
            echo "  paperless  Paperless-ngx users (Django manage.py)"
            echo "  stalwart   Stalwart mail accounts + SOGo's mail_users rows"
            echo "  invoice    Invoice Ninja: not wired (company-bound users)"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Default: talk to the zitadel container via docker network
#
# NOTE: the zitadel container runs with '--tlsMode external' (Traefik
# terminates TLS in front), so it speaks ONLY plain HTTP on :8080. All
# CURL_HOST/container-direct curls MUST therefore use http:// — exactly
# like the compose healthcheck (http://localhost:8080/debug/ready). The
# external BASE_URL below may keep https:// because it goes through
# Traefik's TLS termination.
[[ -z "$HOST" ]] && HOST="opensme-zitadel:8080"
[[ -z "$EXTERNAL_HOST" ]] && EXTERNAL_HOST="$HOST"

BASE_URL="https://$EXTERNAL_HOST"
# Inside docker network, use the container name directly
CURL_HOST="$HOST"

# ── resolution mode ───────────────────────────────────────────────────
# The Zitadel container is distroless (no shell, no cat) and its :8080 is
# not published — the API is reachable either from inside the compose
# network (container DNS, how CI's in-stack callers reach it) or through
# the Traefik edge (how tests/05-e2e and scripts/zitadel-client.sh reach
# it from a bare host: https + --resolve for *.local-style demo hosts).
if [[ "$IN_DOCKER" == "true" ]]; then
    ZT_MODE="in-docker"
elif getent hosts "${HOST%%:*}" >/dev/null 2>&1; then
    ZT_MODE="docker-net"
else
    ZT_MODE="edge"
fi
zt_scheme="http"; zt_base="$CURL_HOST"; zt_extra=()
if [[ "$ZT_MODE" == "edge" ]]; then
    # The container-internal host is unresolvable from here — go through the
    # Traefik edge instead, like tests/05-e2e and scripts/zitadel-client.sh.
    # EXTERNAL_HOST defaults to the container name, so fall back to the
    # ZITADEL_DOMAIN from .env (the same host demo.sh registers at the edge).
    edge_host="${EXTERNAL_HOST%%:*}"
    if ! getent hosts "$edge_host" >/dev/null 2>&1 && [[ -f .env ]] \
            && grep -q '^ZITADEL_DOMAIN=' .env; then
        edge_host="$(grep -m1 '^ZITADEL_DOMAIN=' .env | cut -d= -f2- | tr -d '\r')"
    fi
    zt_scheme="https"; zt_base="$edge_host"
    zt_extra=(--resolve "$edge_host:443:127.0.0.1" --resolve "$edge_host:80:127.0.0.1")
    BASE_URL="https://$edge_host"
fi

log() { $VERBOSE && echo "[provision] $*"; }
info() { echo "[provision] $*"; }
err() { echo "[provision] ERROR: $*" >&2; }

# ── helper: curl with optional Host header for docker ────────────────
ztcurl() {
    local extra=()
    [[ "$IN_DOCKER" == "true" ]] && extra+=("-H" "Host: $EXTERNAL_HOST")
    curl -s -f -S --retry 3 --max-time 30 "${zt_extra[@]}" "${extra[@]}" "$@"
}

# ── wait for Zitadel ──────────────────────────────────────────────────
wait_zitadel() {
    if ! $WAIT; then return 0; fi
    info "Waiting for Zitadel at $zt_scheme://$zt_base..."
    for _ in $(seq 1 60); do
        if ztcurl -k "$zt_scheme://$zt_base/healthz" >/dev/null 2>&1; then
            info "Zitadel is ready"
            return 0
        fi
        sleep 2
    done
    err "Zitadel not ready after 120s"
    return 1
}

# ── fetch PAT from container or env ──────────────────────────────────
get_pat() {
    local pat
    pat="${ZITADEL_PAT:-${E2E_ZITADEL_PAT:-}}"
    if [[ -n "$pat" ]]; then
        echo "$pat"
        return 0
    fi
    
    if command -v docker &>/dev/null; then
        # The zitadel image is distroless: no shell, no cat — docker exec
        # cannot read the seeded PAT. `docker compose cp` streams the file
        # out (the same pattern tests/05-e2e/run.py::fetch_seeded and
        # scripts/zitadel-client.sh::locate_pat use); the compose file set
        # must define a `zitadel` service, so try the usual overlays.
        local -a sets args
        local files f
        sets=()
        if [[ -n "${COMPOSE_FILE:-}" ]]; then
            sets+=("$COMPOSE_FILE")
        fi
        sets+=("docker-compose.yml idm/zitadel.yml")
        for files in "${sets[@]}"; do
            args=()
            for f in $files; do
                [[ -f "$f" ]] && args+=(-f "$f")
            done
            [[ -f .env ]] && args+=(--env-file .env)
            if pat=$(docker compose "${args[@]}" cp zitadel:/machinekey/pat - 2>/dev/null \
                    | tar -xOf - 2>/dev/null | tr -d '\r\n') && [[ -n "$pat" ]]; then
                echo "$pat"
                return 0
            fi
        done
        # Fallback: shell+cat (only for images that have them)
        local cname="opensme-zitadel"
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^opensme-zitadel"; then
            pat=$(docker exec "$cname" cat /machinekey/pat 2>/dev/null || true)
            [[ -n "$pat" ]] && { echo "$pat"; return 0; }
        fi
    fi
    
    err "No machine PAT found."
    err "  - Set ZITADEL_PAT or E2E_ZITADEL_PAT environment variable, or"
    err "  - Ensure zitadel container has /machinekey/pat (from 'zitadel setup --steps /steps.yaml'), or"
    err "  - Run with --wait after stack is up"
    return 1
}

# ── admin API wrapper ─────────────────────────────────────────────────
api() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    shift 3
    
    local url="$zt_scheme://$zt_base$path"
    local token="$1"
    
    local extra=("-H" "Authorization: Bearer $token")
    [[ "$IN_DOCKER" == "true" ]] && extra+=("-H" "Host: $EXTERNAL_HOST")
    
    if [[ -n "$data" ]]; then
        extra+=("-H" "Content-Type: application/json" "-d" "$data")
    fi
    
    ztcurl -k -X "$method" "$url" "${extra[@]}" "${@:4}"
}

# ── check if user exists by username ──────────────────────────────────
# ── Zitadel identity API (v4) ─────────────────────────────────────────
# Endpoints verified against Zitadel v4.19 — the same calls tests/05-e2e
# uses: POST /v2/users/human to provision (the initial password rides
# along; the separate /password endpoint is self-service and needs
# verification), POST /management/v1/users/_search to resolve. Zitadel
# has NO groups — the demo groups in bootstrap/demo-users.json feed the
# operaton/synapse components; this component skips them.

user_exists() {
    [[ -n "$(get_user_id "$1" "$2")" ]]
}

# Human users in Zitadel carry org-domain-suffixed login names
# (tweiss@<org-domain>), so a suffix-free equals-query would miss them —
# compare on the '@'-stem, the same way tests/05-e2e/run.py does.
get_user_id() {
    local username="$1"
    local token="$2"
    local res
    res=$(api POST "/management/v1/users/_search" "{\"userName\":\"$username\"}" "$token" 2>/dev/null) || true
    printf '%s' "$res" | jq -r --arg u "$username" \
        '[.result[]? | select((.userName // "") | ascii_downcase | split("@")[0] == ($u | ascii_downcase))][0].id // empty' 2>/dev/null
}

delete_user() {
    local user_id="$1"
    local token="$2"
    api DELETE "/v2/users/$user_id" "" "$token" >/dev/null 2>&1
}

create_user() {
    local json="$1"
    local token="$2"
    api POST "/v2/users/human" "$json" "$token"
}

list_users() {
    local query="$1"
    local token="$2"
    api POST "/management/v1/users/_search" "{\"userName\":\"$query\"}" "$token"
}

# ── shared config loading ─────────────────────────────────────────────
load_config() {
    if [[ ! -f "$USERS_JSON" ]]; then
        err "Demo users config not found: $USERS_JSON"
        return 1
    fi
    if ! jq empty "$USERS_JSON" 2>/dev/null; then
        err "Invalid JSON in $USERS_JSON"
        return 1
    fi
    COMPANY=$(jq -r '.company' "$USERS_JSON")
}

# Normalized email: local part from JSON, domain from --domain
demo_email() {
    local email="$1"
    echo "${email%%@*}@${DOMAIN}"
}

# ══════════════════════════════════════════════════════════════════════
# OPERATON (Camunda 7 compatible engine-rest identity + admin-webapp)
# ══════════════════════════════════════════════════════════════════════
OPERATON_URL="${OPERATON_URL:-}"
OPERATON_CONTAINER="${OPERATON_CONTAINER:-opensme-camunda}"

operaton_curl() {
    # $1 http-code-fmt (0/1)  $2 method  $3 path  $4 data (optional)
    local want_code="$1" method="$2" path="$3" data="${4:-}"
    local args=(-s -X "$method" "http://localhost:8080$path")
    [[ -n "$data" ]] && args+=(-H "Content-Type: application/json" -d "$data")
    if [[ "$want_code" == "1" ]]; then
        args+=(-o /dev/null -w '%{http_code}')
    fi
    if [[ -n "$OPERATON_URL" ]]; then
        local url="${OPERATON_URL}${path}"
        args[-1]="${url}"
        curl "${args[@]}"
    else
        docker exec "$OPERATON_CONTAINER" curl "${args[@]}"
    fi
}

main_operaton() {
    load_config || return 1
    info "[operaton] Provisioning engine users/groups for: $COMPANY"
    $DRY_RUN && { info "[operaton] would create 12 users + department groups + admin-webapp setup (tweiss)"; return 0; }

    local created=0 skipped=0
    # Groups: slug from JSON name (opensme-* already slug-like)
    while IFS= read -r gname; do
        [[ -z "$gname" ]] && continue
        local code
        code=$(operaton_curl 1 GET "/engine-rest/group/$gname")
        if [[ "$code" != "200" ]]; then
            operaton_curl 1 POST "/engine-rest/group/create" \
                "{\"id\":\"$gname\",\"name\":\"$gname\"}" >/dev/null
            info "  ✓ group $gname"
        else
            $VERBOSE && info "  - group $gname exists"
        fi
    done < <(jq -r '.groups[].name' "$USERS_JSON")

    # Users + memberships
    while IFS= read -r user; do
        local un fn ln em pw
        un=$(echo "$user" | jq -r '.username')
        fn=$(echo "$user" | jq -r '.firstName')
        ln=$(echo "$user" | jq -r '.lastName')
        em=$(demo_email "$(echo "$user" | jq -r '.email')")
        pw=$(echo "$user" | jq -r '.password')
        local code
        # engine-rest has no GET /user/{id}; the profile subresource exists.
        code=$(operaton_curl 1 GET "/engine-rest/user/$un/profile")
        if [[ "$code" != "200" ]]; then
            local payload
            payload=$(jq -n --arg id "$un" --arg fn "$fn" --arg ln "$ln" --arg em "$em" --arg pw "$pw" \
                '{profile:{id:$id,firstName:$fn,lastName:$ln,email:$em},credentials:{password:$pw}}')
            code=$(operaton_curl 1 POST "/engine-rest/user/create" "$payload")
            if [[ "$code" == "204" || "$code" == "200" ]]; then
                info "  ✓ user $un"
                created=$((created+1))
            elif [[ "$code" == "400" ]]; then
                # "The user already exists" (race / check-endpoint variance)
                skipped=$((skipped+1)); $VERBOSE && info "  - user $un exists (400)"
            else
                err "  user $un failed (HTTP $code)"; continue
            fi
        else
            skipped=$((skipped+1)); $VERBOSE && info "  - user $un exists"
        fi
        # memberships: department group + opensme-all
        while IFS= read -r gname; do
            [[ -z "$gname" ]] && continue
            operaton_curl 1 PUT "/engine-rest/group/$gname/members/$un" >/dev/null 2>&1 || true
        done < <(jq -r --arg un "$un" '
            .users[] | select(.username==$un)
            | "opensme-all", "opensme-" + (.department | ascii_downcase)' "$USERS_JSON")
    done < <(jq -c '.users[]' "$USERS_JSON")

    # Admin webapp setup: first-run bootstrap (silently skips if an admin exists)
    local admin_pw
    admin_pw=$(jq -r '.users[] | select(.username=="tweiss") | .password' "$USERS_JSON")
    local setup_code
    setup_code=$(operaton_curl 1 POST "/camunda/admin/auth/setup" \
        "{\"userId\":\"tweiss\",\"password\":\"$admin_pw\"}")
    if [[ "$setup_code" == "204" || "$setup_code" == "200" ]]; then
        info "  ✓ admin webapp: tweiss (setup)"
    else
        $VERBOSE && info "  - admin webapp already set up (HTTP $setup_code)"
    fi
    info "[operaton] done: $created created, $skipped skipped"
}

# ══════════════════════════════════════════════════════════════════════
# SYNAPSE (Matrix homeserver — shared-secret register API)
# ══════════════════════════════════════════════════════════════════════
SYNAPSE_CONTAINER="${SYNAPSE_CONTAINER:-opensme-synapse}"

main_synapse() {
    load_config || return 1
    info "[synapse] Registering Matrix users for: $COMPANY"
    $DRY_RUN && { info "[synapse] would register 12 users via $SYNAPSE_CONTAINER shared-secret API (tweiss=admin)"; return 0; }

    # The whole flow runs inside the synapse container: the registration
    # secret never leaves it, python3+urllib are already in the image.
    # Two stdin feeds: (1) user JSON to /tmp, (2) the python driver on stdin.
    jq -c '.' "$USERS_JSON" | docker exec -i "$SYNAPSE_CONTAINER" sh -c 'cat > /tmp/demo-users.json' || {
        err "[synapse] cannot reach container $SYNAPSE_CONTAINER"; return 1
    }
    local out rc
    out=$(cat <<'PY' | docker exec -i "$SYNAPSE_CONTAINER" python3 - 2>&1
import hashlib, hmac, json, urllib.request

def req(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request("http://127.0.0.1:8008" + path, data=data, method=method)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

users = json.load(open("/tmp/demo-users.json"))["users"]
secret = open("/data/registration_secret").read().strip().encode()
for u in users:
    un, pw = u["username"], u["password"]
    admin = "true" if "admin" in u.get("roles", []) else "false"
    status, nonce = req("GET", "/_synapse/admin/v1/register")
    if status != 200:
        print("  ! nonce failed (" + str(status) + ") — shared_secret registration disabled?")
        break
    # Current synapse MAC scheme (rest/admin/users.py): SHA-1 over
    # nonce \x00 username \x00 password \x00 "admin"|"notadmin".
    mac = hmac.new(secret, digestmod=hashlib.sha1)
    mac.update(nonce["nonce"].encode())
    mac.update(b"\x00"); mac.update(un.encode())
    mac.update(b"\x00"); mac.update(pw.encode())
    mac.update(b"\x00"); mac.update(b"admin" if admin == "true" else b"notadmin")
    payload = {"nonce": nonce["nonce"], "username": un, "password": pw,
               "admin": admin == "true", "mac": mac.hexdigest()}
    status, body = req("POST", "/_synapse/admin/v1/register", payload)
    if status == 200:
        print("  \u2713 " + un + (" (admin)" if admin == "true" else ""))
    elif body.get("errcode") in ("M_USER_EXISTS", "M_USER_IN_USE"):
        print("  - " + un + " (exists)")
    else:
        print("  ! " + un + " failed: " + str(body.get("errcode", status)))
PY
) && rc=0 || rc=$?
    docker exec "$SYNAPSE_CONTAINER" rm -f /tmp/demo-users.json >/dev/null 2>&1 || true
    echo "$out" | grep -E '✓|exists|failed|nonce' | while IFS= read -r line; do info "  $line"; done
    if [[ $rc -ne 0 ]]; then
        err "[synapse] driver failed: $out"
        return 1
    fi
    info "[synapse] done"
}

# ══════════════════════════════════════════════════════════════════════
# PAPERLESS (Django users via manage.py shell)
# ══════════════════════════════════════════════════════════════════════
PAPERLESS_CONTAINER="${PAPERLESS_CONTAINER:-opensme-paperless}"

main_paperless() {
    load_config || return 1
    info "[paperless] Creating users for: $COMPANY"
    $DRY_RUN && { info "[paperless] would create 12 Django users via $PAPERLESS_CONTAINER manage.py"; return 0; }

    # Both the driver and the user data travel base64-encoded inside one
    # `shell -c`: manage.py shell -c has no second stdin stream, and embedding
    # raw JSON in shell/python quoting is fragile.
    local users_b64 driver driver_b64 out rc
    users_b64=$(jq -c --arg dom "$DOMAIN" \
        '[.users[] | {username, firstName, lastName, password, email: ((.email | split("@"))[0] + "@" + $dom)}]' \
        "$USERS_JSON" | base64 -w0)
    driver=$(cat <<PY
import json, base64
from django.contrib.auth import get_user_model
users = json.loads(base64.b64decode("$users_b64").decode())
U = get_user_model()
for u in users:
    user, created = U.objects.get_or_create(username=u["username"], defaults={
        "first_name": u["firstName"], "last_name": u["lastName"], "email": u["email"],
    })
    if created:
        user.set_password(u["password"])
        user.save()
        print("OKCREATED " + u["username"])
    else:
        print("OKEXISTS " + u["username"])
PY
)
    driver_b64=$(printf '%s' "$driver" | base64 -w0)
    out=$(docker exec "$PAPERLESS_CONTAINER" python3 /usr/src/paperless/src/manage.py shell \
        -c "import base64; exec(base64.b64decode('$driver_b64').decode())" 2>&1) && rc=0 || rc=$?
    echo "$out" | grep -E '^OK' | sed 's/^OKCREATED /  \xe2\x9c\x93 /; s/^OKEXISTS /  - /; s/$/ (exists)/' | while IFS= read -r line; do info "$line"; done
    if [[ $rc -ne 0 ]]; then
        err "[paperless] manage.py shell failed: $out"
        return 1
    fi
    info "[paperless] done"
}

# ══════════════════════════════════════════════════════════════════════
# STALWART (mail accounts) — two halves, one call
#
# Stalwart authenticates IMAP/SMTP against its own store, SOGo authenticates
# webmail logins against Postgres (mail_users). A demo user therefore has to
# exist in BOTH, with the same password, or half of the stack answers
# "login failed" while the other half looks healthy:
#
#   1. a Stalwart principal (type=individual) with roles=["user"];
#      without that role Stalwart refuses the IMAP LOGIN and SOGo's account
#      list ends up empty even though the webmail login itself succeeded.
#   2. a mail_users row whose c_password is a {SHA512-CRYPT} hash of the same
#      password (sogo.conf.template: userPasswordAlgorithm = sha512-crypt,
#      prependPasswordScheme = YES).
# ══════════════════════════════════════════════════════════════════════
STALWART_CONTAINER="${STALWART_CONTAINER:-opensme-stalwart}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-opensme-postgres}"
STALWART_MAIL_DOMAIN="${STALWART_MAIL_DOMAIN:-$DOMAIN}"

# SOGo password hash: a fresh salt per call means two runs produce different
# (equally valid) hashes — the row is only rewritten when it is missing.
sogo_password_hash() {
    printf '{SHA512-CRYPT}%s' \
        "$(openssl passwd -6 -salt "$(openssl rand -hex 8)" "$1")"
}

main_stalwart() {
    load_config || return 1
    info "[stalwart] Provisioning mail accounts for: $COMPANY (@$STALWART_MAIL_DOMAIN)"
    $DRY_RUN && { info "[stalwart] would write 12 rows in $POSTGRES_CONTAINER (sogo.mail_users); Stalwart principals auto-provision on first SSO login"; return 0; }

    local pg_user
    # Fail loudly here rather than after 12 silent per-user errors: without the
    # table no webmail login can ever succeed (see postgres-init/02-mail-users.sql).
    pg_user=$(docker exec "$POSTGRES_CONTAINER" printenv POSTGRES_USER 2>/dev/null || true)
    pg_user="${pg_user:-opensme}"
    if ! docker exec "$POSTGRES_CONTAINER" psql -U "$pg_user" -d sogo -tAc \
            'select 1 from mail_users limit 1' >/dev/null 2>&1; then
        err "[stalwart] table sogo.mail_users is not readable — was postgres-init/02-mail-users.sql applied?"
        return 1
    fi

    # ── Stalwart half: NO eager principal writes ──
    # Since the webmail SSO (Epic C3) the default directory is type "oidc"
    # (mail/stalwart/config.toml.template [directory.oidc]): access tokens are
    # validated against Zitadel's userinfo endpoint and the principal is
    # AUTO-PROVISIONED on first login with the built-in role `user`
    # (upstream v0.15: OpenIdResponse::build_principal pushes PrincipalData::
    # Role(ROLE_USER)). The management API refuses INSERT/UPDATE against an
    # oidc directory ("Only internal directories support inserts and update
    # operations") — but DELETE passes, so the only provisioning duty left is
    # removing pre-SSO principals whose bare names ("tweiss") collide with
    # the auto-provision's unique email index (their emails already carry the
    # SSO uid). Without this the first SSO login dies with "Already exists".
    local admin_pw ip api
    admin_pw=$(docker exec "$STALWART_CONTAINER" printenv STALWART_ADMIN_PASSWORD 2>/dev/null || true)
    ip=$(docker inspect "$STALWART_CONTAINER" 2>/dev/null | jq -r '.[0].NetworkSettings.Networks[]?.IPAddress' | head -1)
    if [[ -n "$admin_pw" && -n "$ip" && "$ip" != "null" ]]; then
        api="http://$ip:8080"
    else
        api=""
        info "[stalwart] no admin API reachable — skipping legacy principal cleanup"
    fi

    local sogo_written=0 failures=0
    while IFS= read -r user; do
        local un pw cn email sso_uid
        un=$(echo "$user" | jq -r '.username')
        pw=$(echo "$user" | jq -r '.password')
        cn=$(echo "$user" | jq -r '.displayName')
        # Identity contract (webmail SSO, Epic C3): the SSO uid is
        #   <user>@$DOMAIN
        # — exactly Zitadel's email claim (main_zitadel normalizes to the
        # same shape), which SOGo matches against mail_users.c_uid
        # (SOGoOpenIdEmailParam="email") and presents as the IMAP/XOAUTH2
        # login. Stalwart names auto-provisioned principals after that same
        # claim, so all three identities are one string; the routing address
        # (mail column) is the same @<OPENSME_DOMAIN> shape.
        email="$un@$DOMAIN"
        sso_uid="$email"

        # Legacy principal from the pre-SSO era (bare name, emails already on
        # the SSO uid)? Delete it — see the note above. DELETE is the one
        # write the oidc-directory guard permits. Re-created automatically on
        # first login with the SSO uid as its name.
        if [[ -n "$api" ]] && ! curl -sf -u "admin:$admin_pw" "$api/api/principal/$sso_uid" 2>/dev/null | grep -q '"data"' \
                && curl -sf -u "admin:$admin_pw" "$api/api/principal/$un" 2>/dev/null | grep -q '"data"'; then
            if curl -sf -u "admin:$admin_pw" -X DELETE "$api/api/principal/$un" >/dev/null 2>&1; then
                info "  ✓ removed legacy principal '$un' (re-created as '$sso_uid' on first login)"
            else
                info "  ! legacy principal '$un' could not be deleted — first SSO login may fail"
            fi
        fi

        # SOGo's half: the password as a Dovecot-scheme hash, keyed by the
        # SSO uid. The hash is the DAV/Basic credential only — IMAP/SMTP
        # authenticate via XOAUTH2 tokens against the IdP. ON CONFLICT keeps
        # an existing password (a re-run is not a password reset); rows from
        # the pre-SSO identity form (bare-username c_uid) are removed so SOGo
        # has exactly one mapping per user.
        if docker exec "$POSTGRES_CONTAINER" psql -U "$pg_user" -d sogo -q -v ON_ERROR_STOP=1 \
                -c "DELETE FROM mail_users WHERE c_uid = '$un';" \
                -c "INSERT INTO mail_users (c_uid, c_name, c_cn, c_password, mail)
                    VALUES ('$sso_uid', '$un', '$cn', '$(sogo_password_hash "$pw")', '$email')
                    ON CONFLICT (c_uid) DO UPDATE
                       SET c_name = EXCLUDED.c_name,
                           c_cn   = EXCLUDED.c_cn,
                           mail   = EXCLUDED.mail" >/dev/null 2>&1; then
            sogo_written=$((sogo_written+1))
            $VERBOSE && info "  ✓ $sso_uid"
        else
            err "  $sso_uid: SOGo row (sogo.mail_users) could not be written"
            failures=$((failures+1))
        fi
    done < <(jq -c '.users[]' "$USERS_JSON")
    info "[stalwart] done: $sogo_written SOGo rows (Stalwart principals auto-provision on first login)"
    [[ $failures -eq 0 ]]
}

# ── component dispatcher ──────────────────────────────────────────────
run_components() {
    local list
    case "$COMPONENT" in
        all) list="$ALL_COMPONENTS" ;;
        *)   list="$(echo "$COMPONENT" | tr ',' ' ')" ;;
    esac
    local failures=0
    for c in $list; do
        case "$c" in
            zitadel)   main_zitadel   || failures=$((failures+1)) ;;
            operaton)  main_operaton  || failures=$((failures+1)) ;;
            synapse)   main_synapse   || failures=$((failures+1)) ;;
            paperless) main_paperless || failures=$((failures+1)) ;;
            stalwart)  main_stalwart  || failures=$((failures+1)) ;;
            invoice)   info "[invoice] not wired: Invoice Ninja users are company-bound; demo ships a single admin" ;;
            *)         err "Unknown component: $c (known: $ALL_COMPONENTS)"; failures=$((failures+1)) ;;
        esac
        echo
done
    return $failures
}

main_zitadel() {
    wait_zitadel
    
    # Get PAT for admin operations
    PAT=$(get_pat)
    if [[ -z "$PAT" ]]; then
        err "Cannot proceed without PAT"
        exit 1
    fi
    log "Retrieved machine PAT"
    
    # Validate JSON config
    if [[ ! -f "$USERS_JSON" ]]; then
        err "Demo users config not found: $USERS_JSON"
        exit 1
    fi
    
    local config
    config=$(jq . "$USERS_JSON") || { err "Invalid JSON in $USERS_JSON"; exit 1; }
    
    if $DRY_RUN; then
        echo "$config"
        exit 0
    fi
    
    # Zitadel has no groups; only the human accounts are provisioned.
    local company users
    company=$(echo "$config" | jq -r '.company')
    users=$(echo "$config" | jq -c '.users[]')
    
    info "Provisioning demo users for: $company"
    info "Domain: $DOMAIN"
    info "Host: $BASE_URL"
    
    # ── Groups: not a Zitadel concept (see the API notes above) ────────
    info "Skipping demo groups — Zitadel has no group concept (the operaton/synapse components own them)"

    # ── Clean existing demo users if requested ─────────────────────────
    if $CLEAN; then
        info "Cleaning existing demo users..."
        for user_json in $users; do
            local username
            username=$(echo "$user_json" | jq -r '.username')
            local uid
            uid=$(get_user_id "$username" "$PAT") || true
            if [[ -n "$uid" ]]; then
                delete_user "$uid" "$PAT"
                info "  ✗ Deleted: $username ($uid)"
            fi
        done
    fi
    
    # ── Create users ──────────────────────────────────────────────────
    info "Creating/updating users..."
    local created=0 updated=0 skipped=0
    
    while IFS= read -r user_json; do
        [[ -z "$user_json" ]] && continue
        local username first last display email pwd
        username=$(echo "$user_json" | jq -r '.username')
        first=$(echo "$user_json" | jq -r '.firstName')
        last=$(echo "$user_json" | jq -r '.lastName')
        display=$(echo "$user_json" | jq -r '.displayName')
        email=$(echo "$user_json" | jq -r '.email')
        pwd=$(echo "$user_json" | jq -r '.password')
        
        # Normalize email to use configured domain
        local email_local
        email_local=${email%%@*}
        email="${email_local}@${DOMAIN}"
        
        # Check if user exists
        local uid
        uid=$(get_user_id "$username" "$PAT") || true
        
        if [[ -n "$uid" ]]; then
            log "  - User $username already exists ($uid)"
            skipped=$((skipped + 1))
        else
            # Zitadel v2 human-user shape (verified on v4.19): profile +
            # email nested, password wrapped, isVerified so no invite mail
            # is generated (the stack ships no SMTP relay for the IdP).
            local user_payload
            user_payload=$(jq -n \
                --arg un "$username" \
                --arg fn "$first" \
                --arg ln "$last" \
                --arg dn "$display" \
                --arg em "$email" \
                --arg pw "$pwd" \
                '{
                    userName: $un,
                    profile: { givenName: $fn, familyName: $ln, displayName: $dn },
                    email:   { email: $em, isVerified: true },
                    password: { password: $pw, changeRequired: false }
                }')
            
            local res
            res=$(create_user "$user_payload" "$PAT") || { err "Failed to create user $username"; continue; }
            uid=$(echo "$res" | jq -r '.id')
            info "  ✓ Created user: $display ($username, $uid)"
            created=$((created + 1))
        fi
        
    done < <(printf '%s\n' "$users")  # one compact object per line; a 2nd '.[]' would iterate values
    
    info ""
    info "Provisioning complete: $created created, $updated updated, $skipped skipped"
    
    # ── Summary ────────────────────────────────────────────────────────
    info "Users provisioned:"
    local count=0
    while IFS= read -r user_json; do
        [[ -z "$user_json" ]] && continue
        local un dn
        un=$(echo "$user_json" | jq -r '.username')
        dn=$(echo "$user_json" | jq -r '.displayName')
        local uid
        uid=$(get_user_id "$un" "$PAT") || true
        if [[ -n "$uid" ]]; then
            echo "  ✓ $dn (@$un)"
            count=$((count + 1))
        fi
    done < <(printf '%s\n' "$users")
    
    info ""
    info "Total active demo users: $count / 12"

    if [[ "$count" -lt 12 ]]; then
        err "Some users were not provisioned. Check logs above."
        return 1
    fi
}

run_components
