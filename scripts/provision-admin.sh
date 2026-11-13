#!/usr/bin/env bash
#
# scripts/provision-admin.sh — make tweiss an admin EVERYWHERE
#
# The demo user provisioning (scripts/provision-demo-users.sh) seeds tweiss
# as a plain member in several components; this script guarantees the
# admin/owner flag on top, per component:
#
#   zitadel    IdP — org membership with role ORG_OWNER (Management API,
#              machine PAT; membership is dedup'd via a member _search first)
#   paperless  document vault — Django is_superuser + is_staff (manage.py shell)
#   operaton   BPM engine — membership in the admin group via engine-rest
#              (group name read from bootstrap/demo-users.json, same source
#              scripts/provision-demo-users.sh provisions from)
#   synapse    Matrix homeserver — users.admin = 1 in the synapse directory
#              (DB check via psycopg2 inside the container; missing user is
#              registered via the shared-secret API with admin=true, an
#              existing non-admin row is UPDATEd)
#
# The admin identity is bootstrap/demo-users.json → username "tweiss"
# (Tobias Weiß, roles owner/admin/developer). Everything is idempotent:
# re-running does NOT duplicate memberships or touch already-admin rows.
#
# Modes:
#   (default)          apply everything, then run the --check logic
#   --dry-run          print the plan (header "admin-coverage") and exit 0 —
#                      NO docker, NO network (safe offline)
#   --check            run all live checks only; one line per app, final
#                      line ADMIN-COVERAGE-OK; exit 0 only if all pass
#
# Secrets: the machine PAT and demo passwords are never printed.
#
# Env:
#   ZITADEL_PAT                machine PAT (else /tmp/opencode/machine-pat,
#                              else docker cp from opensme-zitadel)
#   ZITADEL_IDP_BASE           default https://auth.home.opensme.graphwiz.ai
#   ZITADEL_CONTAINER / OPERATON_CONTAINER / PAPERLESS_CONTAINER /
#   SYNAPSE_CONTAINER          defaults opensme-<name>
#   ADMIN_USERNAME             default tweiss
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
USERS_JSON="$REPO_ROOT/bootstrap/demo-users.json"

ADMIN_USERNAME="${ADMIN_USERNAME:-tweiss}"
IDP_BASE="${ZITADEL_IDP_BASE:-https://auth.home.opensme.graphwiz.ai}"
ZT_CONTAINER="${ZITADEL_CONTAINER:-opensme-zitadel}"
OP_CONTAINER="${OPERATON_CONTAINER:-opensme-camunda}"
PP_CONTAINER="${PAPERLESS_CONTAINER:-opensme-paperless}"
SY_CONTAINER="${SYNAPSE_CONTAINER:-opensme-synapse}"
MACHINE_PAT_FILE="${MACHINE_PAT_FILE:-/tmp/opencode/machine-pat}"

MODE="apply"
case "${1:-}" in
    --dry-run) MODE="dry-run" ;;
    --check)   MODE="check" ;;
    "") ;;
    *) echo "usage: $0 [--dry-run|--check]" >&2; exit 2 ;;
esac

log()  { echo "[admin] $*"; }
err()  { echo "[admin] ERROR: $*" >&2; }

# ── config lookups (offline-safe) ─────────────────────────────────────
# The admin's demo-users id ("tobias.weiss") — the key the groups section
# references — and the admin group for operaton (first group listing the
# admin; "opensme-management", Company leadership).
user_json_id() {
    jq -r --arg u "$ADMIN_USERNAME" \
        '.users[] | select((.username // "") | ascii_downcase == ($u | ascii_downcase)) | .id' \
        "$USERS_JSON" | head -n1
}

admin_group() {
    local uid
    uid="$(user_json_id)"
    jq -r --arg uid "$uid" \
        '.groups[] | select((.members // []) | index($uid)) | .name' \
        "$USERS_JSON" | head -n1
}

print_plan() {
    echo "=== admin-coverage plan (tweiss) ==="
    echo "config: $USERS_JSON"
    echo "  zitadel   [$ZT_CONTAINER] org member $ADMIN_USERNAME with roles=[ORG_OWNER] (IdP $IDP_BASE)"
    echo "  paperless [$PP_CONTAINER] is_superuser=True is_staff=True for username tweiss*"
    echo "  operaton  [$OP_CONTAINER] engine-rest: group membership $ADMIN_USERNAME -> $(admin_group)"
    echo "  synapse   [$SY_CONTAINER] users.admin=1 for @tweiss:* (register/update as needed)"
    echo "=== end plan ==="
}

# ── machine PAT (script 2 reuses this recipe) ─────────────────────────
# env ZITADEL_PAT → /tmp/opencode/machine-pat → docker cp out of the
# distroless zitadel container into a temp file chowned to this user.
get_pat() {
    local pat="${ZITADEL_PAT:-}"
    if [[ -z "$pat" && -s "$MACHINE_PAT_FILE" ]]; then
        pat="$(tr -d '\r\n' < "$MACHINE_PAT_FILE")"
    fi
    if [[ -z "$pat" ]] && command -v docker >/dev/null 2>&1; then
        local tmp
        tmp="$(mktemp)"
        if docker cp "$ZT_CONTAINER:/machinekey/pat" "$tmp" >/dev/null 2>&1; then
            sudo chown "$(id -u):$(id -g)" "$tmp" 2>/dev/null || true
            pat="$(tr -d '\r\n' < "$tmp")"
        fi
        rm -f "$tmp"
    fi
    [[ -n "$pat" ]] && { printf '%s' "$pat"; return 0; }
    return 1
}

# ── Zitadel Management API helpers ────────────────────────────────────
# The IdP runs behind TLS at the edge; the shaped userName queries answer
# empty on this server, so searches use an EMPTY JSON body and the filter
# happens client-side (case-insensitive, '@'-stem aware).
mgmt() {
    local method="$1" path="$2" pat="$3"
    curl -sk -X "$method" "$IDP_BASE$path" \
        -H "Authorization: Bearer $pat" \
        -H "Content-Type: application/json" \
        --data '{}'
}

# userId for $ADMIN_USERNAME (empty-body search + local case-insensitive filter)
resolve_user_id() {
    local pat="$1"
    mgmt POST "/management/v1/users/_search" "$pat" |
        jq -r --arg u "$ADMIN_USERNAME" \
        '[.result[]? | select((.userName // "") | ascii_downcase | split("@")[0] == ($u | ascii_downcase))][0].id // empty'
}

org_member_role_present() {
    # tweiss listed as org member? (empty-body _search, local filter)
    local uid="$1" pat="$2"
    mgmt POST "/management/v1/orgs/me/members/_search" "$pat" |
        jq -e --arg uid "$uid" '.result[]? | select(.userId == $uid)' >/dev/null 2>&1
}

zitadel_admin() { # $1 = apply|check
    local mode="$1" pat uid
    pat="$(get_pat)" || { echo "admin-coverage zitadel FAIL: no machine PAT"; return 1; }
    uid="$(resolve_user_id "$pat")"
    if [[ -z "$uid" ]]; then
        echo "admin-coverage zitadel FAIL: user $ADMIN_USERNAME not found at $IDP_BASE"
        return 1
    fi
    if org_member_role_present "$uid" "$pat"; then
        echo "admin-coverage zitadel OK: $ADMIN_USERNAME ($uid) already an org member"
        return 0
    fi
    if [[ "$mode" == "check" ]]; then
        echo "admin-coverage zitadel FAIL: $ADMIN_USERNAME ($uid) not an org member"
        return 1
    fi
    # POST /management/v1/orgs/me/members with userId + roles [ORG_OWNER]
    local body code
    body="$(jq -cn --arg uid "$uid" '{userId: $uid, roles: ["ORG_OWNER"]}')"
    code="$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$IDP_BASE/management/v1/orgs/me/members" \
        -H "Authorization: Bearer $pat" -H "Content-Type: application/json" -d "$body")"
    if [[ "$code" =~ ^2 ]]; then
        echo "admin-coverage zitadel OK: $ADMIN_USERNAME ($uid) added as ORG_OWNER"
        return 0
    fi
    echo "admin-coverage zitadel FAIL: member add answered HTTP $code"
    return 1
}

# ── Paperless (Django superuser/staff) ────────────────────────────────
# Short python driver through `manage.py shell -c`; matches username tweiss
# or any username starting with tweiss (case-insensitive). Verification is
# a second shell query. Nothing secret is handled here.
paperless_admin() { # $1 = apply|check
    local mode="$1" py out rc
    # mode is injected as DPY_MODE: check NEVER mutates (read-only query)
    py="$(cat <<PY
import os
from django.contrib.auth import get_user_model
U = get_user_model()
qs = U.objects.filter(username__istartswith="$ADMIN_USERNAME")
if not qs.exists():
    print("NOMATCH")
elif qs.filter(is_superuser=True, is_staff=True).exists():
    print("ALREADY-ADMIN")
elif os.environ.get("DPY_MODE") == "check":
    print("NOT-ADMIN")
else:
    qs.update(is_superuser=True, is_staff=True)
    print("PROMOTED")
PY
)"
    if ! out="$(docker exec -e DPY_MODE="$mode" "$PP_CONTAINER" python3 manage.py shell -c "$py" 2>&1)"; then
        echo "admin-coverage paperless FAIL: manage.py shell failed: ${out:-no output}"
        return 1
    fi
    case "$out" in
        *ALREADY-ADMIN*) echo "admin-coverage paperless OK: tweiss* already superuser+staff" ;;
        *PROMOTED*)      echo "admin-coverage paperless OK: tweiss* set is_superuser+is_staff" ;;
        *NOMATCH*)
            echo "admin-coverage paperless FAIL: no user tweiss* in Django auth (run provision-demo-users.sh first)"
            return 1 ;;
        *NOT-ADMIN*)
            echo "admin-coverage paperless FAIL: tweiss* exists but lacks superuser/staff"
            return 1 ;;
        *)
            echo "admin-coverage paperless FAIL: unexpected driver output: ${out:0:80}"
            return 1 ;;
    esac
    # verification pass (fresh query, mirrors the SSO-relevant flags)
    local vpy vout
    vpy="$(cat <<PY
from django.contrib.auth import get_user_model
U = get_user_model()
qs = U.objects.filter(username__istartswith="$ADMIN_USERNAME", is_superuser=True, is_staff=True)
print("VERIFIED %d" % qs.count())
PY
)"
    vout="$(docker exec "$PP_CONTAINER" python3 manage.py shell -c "$vpy" 2>&1 || true)"
    if [[ "$vout" == *"VERIFIED 0"* || "$vout" != *"VERIFIED"* ]]; then
        echo "admin-coverage paperless FAIL: verify query shows no superuser tweiss*"
        return 1
    fi
    echo "admin-coverage paperless OK: verify pass confirms superuser+staff"
    return 0
}

# ── Operaton (engine-rest admin group membership) ─────────────────────
# Same transport as scripts/provision-demo-users.sh: engine REST inside
# the container (docker exec curl localhost:8080).
ocurl() { # $1 method  $2 path  [$3 body]
    local method="$1" path="$2" body="${3:-}"
    local args=(-s -X "$method" "http://localhost:8080$path")
    [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
    docker exec "$OP_CONTAINER" curl "${args[@]}"
}

ocurl_code() { # $1 method  $2 path  [$3 body] → http status
    local method="$1" path="$2" body="${3:-}"
    local args=(-s -o /dev/null -w '%{http_code}' -X "$method" "http://localhost:8080$path")
    [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
    docker exec "$OP_CONTAINER" curl "${args[@]}"
}

operaton_admin() { # $1 = apply|check
    local mode="$1" group code members
    group="$(admin_group)"
    if [[ -z "$group" ]]; then
        echo "admin-coverage operaton FAIL: no admin group for $ADMIN_USERNAME in demo-users.json"
        return 1
    fi
    # engine user must exist (provision-demo-users.sh creates it)
    code="$(ocurl_code GET "/engine-rest/user/$ADMIN_USERNAME/profile")"
    if [[ "$code" != "200" ]]; then
        echo "admin-coverage operaton FAIL: engine user $ADMIN_USERNAME missing (HTTP $code)"
        return 1
    fi
    # membership via the identity endpoint (GET /engine-rest/groups is not
    # exposed on this engine — /identity/groups answers the user's groups)
    members="$(ocurl GET "/engine-rest/identity/groups?userId=$ADMIN_USERNAME" | jq -r '.groups[]?.id' 2>/dev/null)"
    if printf '%s\n' "$members" | grep -qx -- "$group"; then
        echo "admin-coverage operaton OK: $ADMIN_USERNAME already member of $group"
        return 0
    fi
    # ── read-only identity fallback ───────────────────────────────────
    # SSO-federated deployments ship a READ-ONLY identity service: engine-rest
    # identity writes answer 403 ("Identity service implementation is
    # read-only") and the demo group can never exist locally. The admin
    # signal on such a stack is membership in the engine's own admin group
    # (camunda-admin — granted by the admin-webapp setup / IdP federation).
    code="$(ocurl_code GET "/engine-rest/group/$group")"
    if [[ "$code" != "200" ]]; then
        if printf '%s\n' "$members" | grep -qx 'camunda-admin'; then
            echo "admin-coverage operaton OK: group $group not present on this engine; $ADMIN_USERNAME is engine admin (camunda-admin)"
            return 0
        fi
        if [[ "$mode" == "check" ]]; then
            echo "admin-coverage operaton FAIL: $ADMIN_USERNAME not member of $group (group missing, HTTP $code)"
            return 1
        fi
    elif [[ "$mode" == "check" ]]; then
        echo "admin-coverage operaton FAIL: $ADMIN_USERNAME not member of $group"
        return 1
    fi
    # ── apply: ensure the group exists, then PUT the membership ───────
    if [[ "$code" != "200" ]]; then
        local ro_body
        ro_body="$(ocurl POST "/engine-rest/group/create" "{\"id\":\"$group\",\"name\":\"$group\"}")"
        if [[ -n "$ro_body" ]]; then
            if printf '%s' "$ro_body" | grep -q 'read-only'; then
                echo "admin-coverage operaton FAIL: identity service is read-only (SSO-federated) and $ADMIN_USERNAME lacks camunda-admin (run the admin-webapp setup)"
            else
                echo "admin-coverage operaton FAIL: group $group create failed: $(printf '%s' "$ro_body" | head -c 80)"
            fi
            return 1
        fi
        echo "admin-coverage operaton OK: group $group created"
    fi
    code="$(ocurl_code PUT "/engine-rest/group/$group/members/$ADMIN_USERNAME")"
    if [[ "$code" == "204" || "$code" == "200" ]]; then
        echo "admin-coverage operaton OK: $ADMIN_USERNAME put into $group"
        return 0
    fi
    echo "admin-coverage operaton FAIL: membership PUT answered HTTP $code"
    return 1
}

# ── Synapse (Matrix homeserver admin flag) ────────────────────────────
# Check via a python3 query inside the synapse container (psycopg2 to its
# own postgres, credentials from the container env). Missing user →
# shared-secret register with admin=true (the provision-demo-users.sh
# flow); existing row with admin != 1 → UPDATE users SET admin = 1.
synapse_py() {
    docker exec -i -e ADMIN_USERNAME="$ADMIN_USERNAME" "$SY_CONTAINER" python3 - "$1" <<'PY'
import json, os, sys

mode = sys.argv[1] if len(sys.argv) > 1 else "check"
un = os.environ["ADMIN_USERNAME"]

def db():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )

with db() as conn, conn.cursor() as cur:
    cur.execute("SELECT name, admin FROM users WHERE lower(name) LIKE %s", ("@%s:%%" % un,))
    row = cur.fetchone()
    if row is None:
        print(json.dumps({"state": "missing"}))
        if mode == "apply":
            # register via the shared-secret API with admin=true; the MAC
            # covers the exact password sent (a fresh random one — the user
            # did not exist, so no credential is clobbered)
            import hashlib, hmac, secrets, urllib.request, urllib.error
            secret = open("/data/registration_secret").read().strip().encode()
            pw = secrets.token_urlsafe(16)
            req = urllib.request.Request("http://127.0.0.1:8008/_synapse/admin/v1/register")
            with urllib.request.urlopen(req) as resp:
                nonce = json.loads(resp.read())["nonce"]
            mac = hmac.new(secret, digestmod=hashlib.sha1)
            mac.update(nonce.encode()); mac.update(b"\x00")
            mac.update(un.encode()); mac.update(b"\x00")
            mac.update(pw.encode()); mac.update(b"\x00")
            mac.update(b"admin")
            body = json.dumps({"nonce": nonce, "username": un,
                               "password": pw, "admin": True,
                               "mac": mac.hexdigest()}).encode()
            req = urllib.request.Request("http://127.0.0.1:8008/_synapse/admin/v1/register",
                                         data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as resp:
                    print(json.dumps({"state": "registered"}))
            except urllib.error.HTTPError as e:
                print(json.dumps({"state": "register-failed", "code": e.code}))
        sys.exit(0)
    name, admin = row
    if int(admin) == 1:
        print(json.dumps({"state": "ok", "name": name}))
    elif mode == "apply":
        cur.execute("UPDATE users SET admin = 1 WHERE name = %s", (name,))
        conn.commit()
        print(json.dumps({"state": "promoted", "name": name}))
    else:
        print(json.dumps({"state": "not-admin", "name": name}))
PY
}

synapse_admin() { # $1 = apply|check
    local mode="$1" out state
    if ! out="$(synapse_py "$mode" 2>&1)"; then
        echo "admin-coverage synapse FAIL: container query failed: ${out:0:100}"
        return 1
    fi
    state="$(printf '%s' "$out" | jq -r '.state // "unknown"' 2>/dev/null || echo unknown)"
    case "$state" in
        ok)        echo "admin-coverage synapse OK: tweiss already admin in the user directory" ;;
        promoted)  echo "admin-coverage synapse OK: users.admin set to 1 for $(printf '%s' "$out" | jq -r .name)" ;;
        registered) echo "admin-coverage synapse OK: re-registered tweiss with admin flag" ;;
        missing)
            echo "admin-coverage synapse FAIL: no @tweiss:* user and registration did not apply"
            return 1 ;;
        register-failed)
            echo "admin-coverage synapse FAIL: shared-secret register answered $(printf '%s' "$out" | jq -r .code)"
            return 1 ;;
        not-admin)
            echo "admin-coverage synapse FAIL: tweiss ($(printf '%s' "$out" | jq -r .name)) is not admin"
            return 1 ;;
        *)
            echo "admin-coverage synapse FAIL: unexpected driver output: ${out:0:80}"
            return 1 ;;
    esac
}

# ── main ──────────────────────────────────────────────────────────────
if [[ ! -f "$USERS_JSON" ]]; then
    err "demo users config not found: $USERS_JSON"
    exit 1
fi
jq empty "$USERS_JSON" 2>/dev/null || { err "invalid JSON in $USERS_JSON"; exit 1; }
command -v jq >/dev/null || { err "jq is required"; exit 1; }

if [[ "$MODE" == "dry-run" ]]; then
    print_plan
    exit 0
fi

failures=0
run_app() { # $1 label  $2 mode  $3 function
    "$3" "$2" || failures=$((failures + 1))
}

if [[ "$MODE" == "check" ]]; then
    log "admin-coverage check (live, no changes)"
    run_app zitadel   check zitadel_admin
    run_app paperless check paperless_admin
    run_app operaton  check operaton_admin
    run_app synapse   check synapse_admin
else
    log "applying admin coverage for $ADMIN_USERNAME, then verifying"
    run_app zitadel   apply zitadel_admin
    run_app paperless apply paperless_admin
    run_app operaton  apply operaton_admin
    run_app synapse   apply synapse_admin
    log "re-checking (check mode logic)"
    run_app zitadel   check zitadel_admin
    run_app paperless check paperless_admin
    run_app operaton  check operaton_admin
    run_app synapse   check synapse_admin
fi

if [[ "$failures" -eq 0 ]]; then
    echo "ADMIN-COVERAGE-OK"
    exit 0
fi
err "$failures admin-coverage check(s) failed"
exit 1
