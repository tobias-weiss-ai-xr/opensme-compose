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

log() { $VERBOSE && echo "[provision] $*"; }
info() { echo "[provision] $*"; }
err() { echo "[provision] ERROR: $*" >&2; }

# ── helper: curl with optional Host header for docker ────────────────
ztcurl() {
    local extra=()
    [[ "$IN_DOCKER" == "true" ]] && extra+=("-H" "Host: $EXTERNAL_HOST")
    curl -s -f -S --retry 3 --max-time 30 "${extra[@]}" "$@"
}

# ── wait for Zitadel ──────────────────────────────────────────────────
wait_zitadel() {
    if ! $WAIT; then return 0; fi
    info "Waiting for Zitadel at http://$CURL_HOST..."
    for _ in $(seq 1 60); do
        if ztcurl -k "http://$CURL_HOST/healthz" >/dev/null 2>&1; then
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
    
    # Try docker exec: /machinekey/pat is where zitadel setup writes it
    if command -v docker &>/dev/null; then
        local cname="opensme-zitadel"
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^opensme-zitadel"; then
            pat=$(docker exec "$cname" cat /machinekey/pat 2>/dev/null || true)
            if [[ -n "$pat" ]]; then
                echo "$pat"
                return 0
            fi
            # Fallback: /run/secrets/... path
            pat=$(docker exec "$cname" cat /run/secrets/zitadel/pat/machine 2>/dev/null || true)
            if [[ -n "$pat" ]]; then
                echo "$pat"
                return 0
            fi
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
    
    local url="http://$CURL_HOST$path"
    local token="$1"
    
    local extra=("-H" "Authorization: Bearer $token")
    [[ "$IN_DOCKER" == "true" ]] && extra+=("-H" "Host: $EXTERNAL_HOST")
    
    if [[ -n "$data" ]]; then
        extra+=("-H" "Content-Type: application/json" "-d" "$data")
    fi
    
    ztcurl -k -X "$method" "$url" "${extra[@]}" "${@:4}"
}

# ── check if user exists by username ──────────────────────────────────
user_exists() {
    local username="$1"
    local token="$2"
    local res
    res=$(api GET "/admin/v1/users/by_query?query=$username" "$token" 2>/dev/null) || true
    [[ -n "$res" && "$res" != "null" && "$res" != "{}" ]]
}

# ── get user ID by username ──────────────────────────────────────────
get_user_id() {
    local username="$1"
    local token="$2"
    local res
    res=$(api GET "/admin/v1/users/by_query?query=$username" "$token" 2>/dev/null) || true
    echo "$res" | jq -r '.[0].id // empty' 2>/dev/null || true
}

# ── delete user by ID ────────────────────────────────────────────────
delete_user() {
    local user_id="$1"
    local token="$2"
    api DELETE "/admin/v1/users/$user_id" "" "$token" >/dev/null 2>&1
}

# ── create user ──────────────────────────────────────────────────────
create_user() {
    local json="$1"
    local token="$2"
    api POST "/admin/v1/users" "$json" "$token"
}

# ── list all users matching a query ──────────────────────────────────
list_users() {
    local query="$1"
    local token="$2"
    api GET "/admin/v1/users/by_query?query=$query" "" "$token"
}

# ── create group ─────────────────────────────────────────────────────
create_group() {
    local json="$1"
    local token="$2"
    api POST "/admin/v1/groups" "$json" "$token"
}

# ── get group ID by name ──────────────────────────────────────────────
get_group_id() {
    local name="$1"
    local token="$2"
    local res
    res=$(api GET "/admin/v1/groups/by_query?query=$name" "$token" 2>/dev/null) || true
    echo "$res" | jq -r '.[0].id // empty' 2>/dev/null || true
}

# ─ add user to group ─────────────────────────────────────────────────
add_user_to_group() {
    local group_id="$1"
    local user_id="$2"
    local token="$3"
    api POST "/admin/v1/groups/$group_id/members" \
        "{\"userIds\": [\"$user_id\"]}" "$token" >/dev/null 2>&1
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

# Permissions of the role every mail user needs: Stalwart refuses IMAP LOGIN
# (and sending) for a principal without them.
STALWART_USER_PERMISSIONS='["authenticate","email-receive","email-send","imap-append","imap-authenticate","imap-copy","imap-create","imap-delete","imap-enable","imap-expunge","imap-fetch","imap-id","imap-idle","imap-list","imap-lsub","imap-move","imap-select","imap-status","imap-store"]'

# SOGo password hash: a fresh salt per call means two runs produce different
# (equally valid) hashes — the row is only rewritten when it is missing.
sogo_password_hash() {
    printf '{SHA512-CRYPT}%s' \
        "$(openssl passwd -6 -salt "$(openssl rand -hex 8)" "$1")"
}

main_stalwart() {
    load_config || return 1
    info "[stalwart] Creating mail accounts for: $COMPANY (@$STALWART_MAIL_DOMAIN)"
    $DRY_RUN && { info "[stalwart] would create 12 principals + role 'user' via $STALWART_CONTAINER and 12 rows in $POSTGRES_CONTAINER (sogo.mail_users)"; return 0; }

    local admin_pw ip api pg_user
    admin_pw=$(docker exec "$STALWART_CONTAINER" printenv STALWART_ADMIN_PASSWORD 2>/dev/null || true)
    if [[ -z "$admin_pw" ]]; then
        err "[stalwart] STALWART_ADMIN_PASSWORD not found in container env"
        return 1
    fi
    ip=$(docker inspect "$STALWART_CONTAINER" 2>/dev/null | jq -r '.[0].NetworkSettings.Networks[]?.IPAddress' | head -1)
    if [[ -z "$ip" || "$ip" == "null" ]]; then
        err "[stalwart] cannot determine container IP of $STALWART_CONTAINER"
        return 1
    fi
    api="http://$ip:8080"
    local call=(curl -sf -u "admin:$admin_pw")

    local pg_user
    pg_user=$(docker exec "$POSTGRES_CONTAINER" printenv POSTGRES_USER 2>/dev/null || true)
    pg_user="${pg_user:-opensme}"
    # Fail loudly here rather than after 12 silent per-user errors: without the
    # table no webmail login can ever succeed (see postgres-init/02-mail-users.sql).
    if ! docker exec "$POSTGRES_CONTAINER" psql -U "$pg_user" -d sogo -tAc \
            'select 1 from mail_users limit 1' >/dev/null 2>&1; then
        err "[stalwart] table sogo.mail_users is not readable — was postgres-init/02-mail-users.sql applied?"
        return 1
    fi

    # Domain principal must exist before accounts can reference it in emails.
    if "${call[@]}" -X POST -H 'Content-Type: application/json' \
            -d "{\"type\":\"domain\",\"name\":\"$STALWART_MAIL_DOMAIN\"}" "$api/api/principal" 2>/dev/null | grep -q fieldAlreadyExists; then
        $VERBOSE && info "  - domain $STALWART_MAIL_DOMAIN exists"
    else
        info "  ✓ domain $STALWART_MAIL_DOMAIN"
    fi

    # Role principal: every mail user carries it (see above).
    if "${call[@]}" "$api/api/principal?types=role" 2>/dev/null | grep -q '"name":"user"'; then
        $VERBOSE && info "  - role 'user' exists"
    elif "${call[@]}" -X POST -H 'Content-Type: application/json' \
            -d "{\"type\":\"role\",\"name\":\"user\",\"enabledPermissions\":$STALWART_USER_PERMISSIONS}" \
            "$api/api/principal" 2>/dev/null | grep -q '"data"'; then
        info "  ✓ role 'user'"
    else
        err "[stalwart] could not create role 'user' — IMAP logins will fail"
        return 1
    fi

    local created=0 skipped=0 roleless=0 sogo_written=0
    while IFS= read -r user; do
        local un pw cn email
        un=$(echo "$user" | jq -r '.username')
        pw=$(echo "$user" | jq -r '.password')
        cn=$(echo "$user" | jq -r '.displayName')
        # One address in both stores — Stalwart's principal and SOGo's row must
        # agree, otherwise the webmail shows an account the server does not own.
        email="$un@$STALWART_MAIL_DOMAIN"

        if "${call[@]}" "$api/api/principal/$un" 2>/dev/null | grep -q '"error"'; then
            if "${call[@]}" -X POST -H 'Content-Type: application/json' \
                    -d "{\"type\":\"individual\",\"name\":\"$un\",\"emails\":[\"$email\"],\"secrets\":[\"$pw\"],\"roles\":[\"user\"]}" \
                    "$api/api/principal" 2>/dev/null | grep -q '"data"'; then
                info "  ✓ $email"
                created=$((created+1))
            else
                err "  $email failed"
            fi
        else
            skipped=$((skipped+1))
            $VERBOSE && info "  - $email exists"
            # Principals created before the role was part of the payload have
            # no roles at all; the API cannot patch them (PATCH → notFound),
            # so report instead of silently leaving a half-working account.
            if ! "${call[@]}" "$api/api/principal/$un" 2>/dev/null | grep -q '"roles":\["user"\]'; then
                roleless=$((roleless+1))
                err "  $email exists WITHOUT roles:[\"user\"] — delete and re-provision it (IMAP login will fail)"
            fi
        fi

        # SOGo's half: the same password as a Dovecot-scheme hash. ON CONFLICT
        # keeps an existing password (a re-run is not a password reset).
        if docker exec "$POSTGRES_CONTAINER" psql -U "$pg_user" -d sogo -q -v ON_ERROR_STOP=1 \
                -c "INSERT INTO mail_users (c_uid, c_name, c_cn, c_password, mail)
                    VALUES ('$un', '$un', '$cn', '$(sogo_password_hash "$pw")', '$email')
                    ON CONFLICT (c_uid) DO UPDATE
                       SET c_name = EXCLUDED.c_name,
                           c_cn   = EXCLUDED.c_cn,
                           mail   = EXCLUDED.mail" >/dev/null 2>&1; then
            sogo_written=$((sogo_written+1))
        else
            err "  $email: SOGo row (sogo.mail_users) could not be written"
        fi
    done < <(jq -c '.users[]' "$USERS_JSON")
    info "[stalwart] done: $created created, $skipped existing, $sogo_written SOGo rows"
    if [[ $roleless -gt 0 ]]; then
        err "[stalwart] $roleless account(s) lack roles:[\"user\"] — IMAP/webmail is broken for them"
        return 1
    fi
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
    
    local company groups users
    company=$(echo "$config" | jq -r '.company')
    users=$(echo "$config" | jq -c '.users[]')
    groups=$(echo "$config" | jq -c '.groups[]')
    
    info "Provisioning demo users for: $company"
    info "Domain: $DOMAIN"
    info "Host: $BASE_URL"
    
    # ── Create groups first ────────────────────────────────────────────
    info "Creating groups..."
    while IFS= read -r group_json; do
        [[ -z "$group_json" ]] && continue
        local gname group_id
        gname=$(echo "$group_json" | jq -r '.name')
        
        # Check if group exists
        group_id=$(get_group_id "$gname" "$PAT")
        if [[ -z "$group_id" ]]; then
            # Create group
            local res
            res=$(create_group "$group_json" "$PAT") || { err "Failed to create group $gname"; continue; }
            group_id=$(echo "$res" | jq -r '.id')
            info "  ✓ Created group: $gname ($group_id)"
        else
            log "  - Group $gname already exists ($group_id)"
        fi
        
        # Store group ID for later
        declare "group_${gname}=$group_id"
    done < <(echo "$groups" | jq -c '.[]')
    
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
            # Create user
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
                    firstName: $fn,
                    lastName: $ln,
                    displayName: $dn,
                    email: $em,
                    password: $pw,
                    passwordChangeRequired: false
                }')
            
            local res
            res=$(create_user "$user_payload" "$PAT") || { err "Failed to create user $username"; continue; }
            uid=$(echo "$res" | jq -r '.id')
            info "  ✓ Created user: $display ($username, $uid)"
            created=$((created + 1))
        fi
        
        # ── Add user to groups ────────────────────────────────────────
        local member_of
        member_of=$(echo "$user_json" | jq -r '.memberOf // .groups // [] | .[]')
        if [[ -z "$member_of" ]]; then
            # Use department-based groups from the JSON structure
            member_of=$(echo "$user_json" | jq -r '[.department, (.roles[])] | flatten | unique | .[]')
        fi
        
        while IFS= read -r gname; do
            [[ -z "$gname" ]] && continue
            local gid
            gid=$(get_group_id "$gname" "$PAT") || true
            if [[ -n "$gid" ]]; then
                add_user_to_group "$gid" "$uid" "$PAT"
                log "    + Added to group: $gname"
            else
                log "    ! Group not found: $gname"
            fi
        done < <(echo "$member_of" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$')
        
    done < <(echo "$users" | jq -c '.[]')
    
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
    done < <(echo "$users" | jq -c '.[]')
    
    info ""
    info "Total active demo users: $count / 12"

    if [[ "$count" -lt 12 ]]; then
        err "Some users were not provisioned. Check logs above."
        return 1
    fi
}

run_components
