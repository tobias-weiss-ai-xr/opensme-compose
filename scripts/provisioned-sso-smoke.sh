#!/usr/bin/env bash
#
# scripts/provisioned-sso-smoke.sh — full front-channel SSO smoke login
#
# Performs a REAL front-channel SSO login for one demo user against the
# three core components — portal (home), notes and webmail (sogo6):
#
#   component start hop → IdP authorize (authRequest + redirect_uri +
#   client_id) → v2 Session API login driven by the login-client PAT
#   (user check → password check → finalize auth request) → GET the
#   callback URL in the SAME http session (redirects DISABLED) → assert
#   the component actually established a login.
#
# This mirrors tests/05-e2e/run.py::api_login + section_sso_matrix — the
# check that catches "authorize works but the component's server-side
# callback broke". The IdP session is intentionally NOT terminated between
# finalizing the auth request and fetching the callback: the component
# would reject the code.
#
# Usage:
#   scripts/provisioned-sso-smoke.sh <username> [components]
#   components: comma list from portal,notes,webmail (default: all three)
# Example:
#   scripts/provisioned-sso-smoke.sh tweiss
#   scripts/provisioned-sso-smoke.sh testuser portal,webmail
#
# Output: one PASS/FAIL line per component; exit 0 only if all pass.
#
# Secrets: PATs and the demo password are read from env/files/JSON and are
# NEVER printed.
#
# Env (all optional):
#   ZITADEL_PAT                     machine PAT (else /tmp/opencode/machine-pat,
#                                   else docker cp opensme-zitadel:/machinekey/pat)
#   E2E_ZITADEL_LOGINCLIENT_PAT     login-client PAT (else /tmp/opencode/lc-pat,
#                                   else docker cp .../machinekey/login-client.pat)
#   ZITADEL_IDP_BASE                default https://auth.home.opensme.graphwiz.ai
#   PORTAL_BASE / NOTES_BASE / WEBMAIL_BASE   component bases (same defaults)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
USERS_JSON="$REPO_ROOT/bootstrap/demo-users.json"

USERNAME="${1:-}"
COMPONENTS="${2:-portal,notes,webmail}"

IDP_BASE="${ZITADEL_IDP_BASE:-https://auth.home.opensme.graphwiz.ai}"
PORTAL_BASE="${PORTAL_BASE:-https://home.opensme.graphwiz.ai}"
NOTES_BASE="${NOTES_BASE:-https://notes.home.opensme.graphwiz.ai}"
WEBMAIL_BASE="${WEBMAIL_BASE:-https://webmail.home.opensme.graphwiz.ai}"

ZT_CONTAINER="${ZITADEL_CONTAINER:-opensme-zitadel}"
MACHINE_PAT_FILE="${MACHINE_PAT_FILE:-/tmp/opencode/machine-pat}"
LC_PAT_FILE="${LC_PAT_FILE:-/tmp/opencode/lc-pat}"

err() { echo "[sso-smoke] ERROR: $*" >&2; }

usage() {
    echo "usage: $0 <username> [components]   (components: portal,notes,webmail — default all)"
}

# ── argument checks ───────────────────────────────────────────────────
[[ -z "$USERNAME" ]] && { usage >&2; exit 2; }
[[ -f "$USERS_JSON" ]] || { err "demo users config not found: $USERS_JSON"; exit 1; }
command -v jq >/dev/null || { err "jq is required"; exit 1; }
command -v curl >/dev/null || { err "curl is required"; exit 1; }

# password for the user (case-insensitive) — read via jq, never printed
PASSWORD="$(jq -r --arg u "$USERNAME" \
    '.users[] | select((.username // "") | ascii_downcase == ($u | ascii_downcase)) | .password' \
    "$USERS_JSON" | head -n1)"
[[ -n "$PASSWORD" && "$PASSWORD" != "null" ]] || {
    err "no password for '$USERNAME' in $USERS_JSON"; exit 1; }

# ── PAT resolution (same recipe as scripts/provision-admin.sh) ────────
read_pat_file() { [[ -s "$1" ]] && tr -d '\r\n' < "$1"; }

docker_cp_pat() { # $1 container path → stdout
    local tmp
    tmp="$(mktemp)"
    if docker cp "$ZT_CONTAINER:$1" "$tmp" >/dev/null 2>&1; then
        sudo chown "$(id -u):$(id -g)" "$tmp" 2>/dev/null || true
        tr -d '\r\n' < "$tmp"
    fi
    rm -f "$tmp"
}

get_machine_pat() {
    local pat="${ZITADEL_PAT:-}"
    [[ -z "$pat" ]] && pat="$(read_pat_file "$MACHINE_PAT_FILE")"
    [[ -z "$pat" ]] && command -v docker >/dev/null 2>&1 && pat="$(docker_cp_pat /machinekey/pat)"
    [[ -n "$pat" ]] && printf '%s' "$pat"
}

get_lc_pat() {
    local pat="${E2E_ZITADEL_LOGINCLIENT_PAT:-}"
    [[ -z "$pat" ]] && pat="$(read_pat_file "$LC_PAT_FILE")"
    [[ -z "$pat" ]] && command -v docker >/dev/null 2>&1 && pat="$(docker_cp_pat /machinekey/login-client.pat)"
    [[ -n "$pat" ]] && printf '%s' "$pat"
}

MACHINE_PAT="$(get_machine_pat)" || true
[[ -n "${MACHINE_PAT:-}" ]] || { err "no machine PAT (set ZITADEL_PAT, /tmp/opencode/machine-pat, or run the stack)"; exit 1; }
LC_PAT="$(get_lc_pat)" || true
[[ -n "${LC_PAT:-}" ]] || { err "no login-client PAT (set E2E_ZITADEL_LOGINCLIENT_PAT, /tmp/opencode/lc-pat, or run the stack)"; exit 1; }

# ── Management API: resolve user ids ──────────────────────────────────
# Empty-body _search (shaped queries return empty on this server) plus a
# local case-insensitive '@'-stem filter.
resolve_user_id() { # $1 login name → stdout: userId
    local want="$1" res
    res="$(curl -sk -X POST "$IDP_BASE/management/v1/users/_search" \
        -H "Authorization: Bearer $MACHINE_PAT" \
        -H "Content-Type: application/json" --data '{}')"
    printf '%s' "$res" | jq -r --arg w "$want" \
        '[.result[]? | select((.userName // "") | ascii_downcase | split("@")[0] == ($w | ascii_downcase))][0].id // empty'
}

USER_ID="$(resolve_user_id "$USERNAME")"
[[ -n "$USER_ID" ]] || { err "user '$USERNAME' not found at $IDP_BASE"; exit 1; }

# the login-client machine user rides the x-zitadel-login-client header
# (best effort — same resolution the e2e suite performs)
LC_USER_ID="$(resolve_user_id opensme-login-client || true)"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
JAR="$WORKDIR/cookies.jar"
: > "$JAR"

# ── login-client headers (bash array — unquoted expansion would word-split
# the "Authorization: Bearer <PAT>" header and corrupt the request) ────
LC_ARGS=()
init_lc_args() {
    LC_ARGS=(-s -k -H "Authorization: Bearer $LC_PAT")
    if [[ -n "$LC_USER_ID" ]]; then
        LC_ARGS+=(-H "x-zitadel-login-client: $LC_USER_ID")
    fi
}
init_lc_args

# ── start hops: component → authorize URL ─────────────────────────────
# Each returns the authorize URL on stdout, or fails with a message on
# stderr (caller turns that into a FAIL line).
start_portal() {
    local hdr loc
    hdr="$(curl -sk -b "$JAR" -c "$JAR" -D- -o /dev/null -w '\n%{http_code}' "$PORTAL_BASE/api/auth/login")"
    loc="$(printf '%s' "$hdr" | grep -im1 '^location:' | tr -d '\r' | cut -d' ' -f2-)"
    [[ "$(printf '%s\n' "$hdr" | tail -n1)" == "303" ]] || { echo "portal start hop answered $(printf '%s\n' "$hdr" | tail -n1), expected 303" >&2; return 1; }
    [[ "$loc" == *"/oauth/v2/authorize"* ]] || { echo "portal 303 Location is not the authorize endpoint: ${loc:0:60}" >&2; return 1; }
    printf '%s' "$loc"
}

start_notes() {
    local hdr loc
    hdr="$(curl -sk -b "$JAR" -c "$JAR" -D- -o /dev/null -w '\n%{http_code}' "$NOTES_BASE/api/v1.0/authenticate/")"
    loc="$(printf '%s' "$hdr" | grep -im1 '^location:' | tr -d '\r' | cut -d' ' -f2-)"
    [[ "$(printf '%s\n' "$hdr" | tail -n1)" == "302" ]] || { echo "notes start hop answered $(printf '%s\n' "$hdr" | tail -n1), expected 302" >&2; return 1; }
    [[ "$loc" == *"/oauth/v2/authorize"* ]] || { echo "notes 302 Location is not the authorize endpoint: ${loc:0:60}" >&2; return 1; }
    printf '%s' "$loc"
}

start_webmail() {
    # sogo6 publishes the login mode as JSON; SSO users get the authorize
    # URL under .location inside .data. Tenant = parent domain of the
    # webmail host (webmail.home.x → home.x) — the user's SSO uid domain.
    local wm_host tenant body kind loc
    wm_host="$(printf '%s' "$WEBMAIL_BASE" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
    tenant="${wm_host#*.}"
    body="$(curl -sk -b "$JAR" -c "$JAR" \
        --get --data-urlencode "username=$USERNAME@$tenant" \
        "$WEBMAIL_BASE/api/user/v1/auth/mode")"
    kind="$(printf '%s' "$body" | jq -r '.data.kind // .kind // empty' 2>/dev/null)"
    loc="$(printf '%s' "$body" | jq -r '.data.location // .location // empty' 2>/dev/null)"
    [[ "$kind" == "sso" ]] || { echo "webmail auth/mode kind='$kind' — SSO not offered" >&2; return 1; }
    [[ -n "$loc" && "$loc" == *"/oauth/v2/authorize"* ]] || { echo "webmail auth/mode gave no authorize location" >&2; return 1; }
    printf '%s' "$loc"
}

# ── v2 Session API login + finalize (the proven e2e recipe) ───────────
# Single-call session create: echoes "sessionId<TAB>sessionToken"
session_create() {
    curl -sk "${LC_ARGS[@]}" -H 'Content-Type: application/json' \
        -X POST "$IDP_BASE/v2/sessions" \
        -d "{\"checks\":{\"user\":{\"userId\":\"$USER_ID\"}}}" \
        | jq -r '[.sessionId // "", .sessionToken // ""] | @tsv'
}

# Full chain: $1 = authRequest id from the authorize probe; $2 = password
finalize_login() {
    local req_id="$1" pw="$2" out sid stok cburl
    out="$(session_create)"
    sid="$(printf '%s' "$out" | cut -f1)"
    stok="$(printf '%s' "$out" | cut -f2)"
    [[ -n "$sid" && -n "$stok" ]] || { echo "session create returned no id/token" >&2; return 1; }
    # (c) password check — returns the CURRENT session token
    stok="$(curl -sk "${LC_ARGS[@]}" -H 'Content-Type: application/json' \
        -X PATCH "$IDP_BASE/v2/sessions/$sid" \
        -d "{\"checks\":{\"password\":{\"password\":\"$pw\"}}}" \
        | jq -r '.sessionToken // empty' 2>/dev/null)"
    [[ -n "$stok" ]] || { echo "password check failed (rejected or errored)" >&2; return 1; }
    # (d) finalize the auth request with the session → callbackUrl
    cburl="$(curl -sk "${LC_ARGS[@]}" -H 'Content-Type: application/json' \
        -X POST "$IDP_BASE/v2/oidc/auth_requests/$req_id" \
        -d "{\"session\":{\"sessionId\":\"$sid\",\"sessionToken\":\"$stok\"}}" \
        | jq -r '.callbackUrl // empty' 2>/dev/null)"
    [[ -n "$cburl" ]] || { echo "auth request finalize gave no callbackUrl" >&2; return 1; }
    # The IdP session is intentionally left alive — the callback MUST be
    # fetched while the session exists or the component rejects the code.
    printf '%s' "$cburl"
}

# ── success markers ───────────────────────────────────────────────────
check_portal() { # $1 callback http headers file, $2 callback code
    local hdrs="$1" code="$2" auth
    if ! grep -qi 'OPENSMEPORTALSESSION' "$JAR"; then
        echo "no OPENSMEPORTALSESSION cookie after callback"; return 1
    fi
    auth="$(curl -sk -b "$JAR" -c "$JAR" "$PORTAL_BASE/api/session")"
    if [[ "$(printf '%s' "$auth" | jq -r '.authenticated // false' 2>/dev/null)" != "true" ]]; then
        echo "/api/session not authenticated: $(printf '%s' "$auth" | head -c 60)"; return 1
    fi
    return 0
}

check_notes() { # $1 callback http headers file, $2 callback code
    local hdrs="$1" code="$2"
    if [[ "$code" -ge 500 ]]; then
        echo "callback HTTP $code (token exchange/JWKS/sync broke)"; return 1
    fi
    if ! grep -qiE '^set-cookie:.*(docs_sessionid|sessionid|csrftoken)' "$hdrs"; then
        echo "no session cookie set after callback"; return 1
    fi
    return 0
}

check_webmail() { # $1 callback http headers file, $2 callback code
    local hdrs="$1" code="$2" loc path
    if [[ "$code" -ge 500 ]]; then
        echo "callback HTTP $code (SOGo token exchange broke)"; return 1
    fi
    loc="$(grep -im1 '^location:' "$hdrs" | tr -d '\r' | cut -d' ' -f2-)"
    # 302 to a path starting /auth/callback — Location may be absolute or relative
    path="$(printf '%s' "$loc" | sed -E 's#^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+##')"
    if [[ "$code" != "302" || "$path" != "/auth/callback"* ]]; then
        echo "callback HTTP $code Location=${loc:0:60} — expected 302 to /auth/callback*"; return 1
    fi
    return 0
}

# ── per-component drive ───────────────────────────────────────────────
run_component() { # $1 name
    local name="$1" start_fn check_fn authz cburl hdrs code problem req_id loc hdr
    case "$name" in
        portal)  start_fn=start_portal;  check_fn=check_portal ;;
        notes)   start_fn=start_notes;   check_fn=check_notes ;;
        webmail) start_fn=start_webmail; check_fn=check_webmail ;;
        *) echo "FAIL $name: unknown component (portal, notes, webmail)"; return 1 ;;
    esac
    if ! authz="$($start_fn)"; then
        echo "FAIL $name: $authz"; return 1
    fi
    # authorize URL sanity: targets the IdP authorize endpoint and carries
    # authRequest-resolvable redirect (redirect_uri + client_id params)
    case "$authz" in
        *"/oauth/v2/authorize"*) ;;
        *) echo "FAIL $name: start hop does not target the IdP authorize endpoint: ${authz:0:70}"; return 1 ;;
    esac
    case "$authz" in
        *redirect_uri=*client_id=*|*"client_id="*"redirect_uri="*) ;;
        *) echo "FAIL $name: authorize URL lacks redirect_uri/client_id: ${authz:0:70}"; return 1 ;;
    esac
    # login chain: authorize probe → authRequest id → session+password → finalize
    hdr="$(curl -sk -D- -o /dev/null "${LC_ARGS[@]}" "$authz")"
    loc="$(printf '%s' "$hdr" | grep -im1 '^location:' | tr -d '\r' | cut -d' ' -f2-)"
    req_id="$(printf '%s' "$loc" | grep -oE 'authRequest=[A-Za-z0-9_:-]+' | head -n1 | cut -d= -f2)"
    if [[ -z "$req_id" ]]; then
        echo "FAIL $name: authorize handed out no authRequest id"; return 1
    fi
    if ! cburl="$(finalize_login "$req_id" "$PASSWORD")"; then
        echo "FAIL $name: $cburl"; return 1
    fi
    # callback in the SAME http session, redirects disabled
    hdrs="$WORKDIR/$name.headers"
    code="$(curl -sk -b "$JAR" -c "$JAR" -D"$hdrs" -o /dev/null -w '%{http_code}' "$cburl")"
    if problem="$($check_fn "$hdrs" "$code")"; then
        echo "PASS $name: full SSO login — session established"
        return 0
    fi
    echo "FAIL $name: $problem"
    return 1
}

# ── main ──────────────────────────────────────────────────────────────
failures=0
selected="$(printf '%s' "$COMPONENTS" | tr ',' ' ')"
for c in $selected; do
    run_component "$c" || failures=$((failures + 1))
done

if [[ "$failures" -eq 0 ]]; then
    echo "SSO-SMOKE-OK ($USERNAME)"
    exit 0
fi
err "$failures component(s) failed for $USERNAME"
exit 1
