#!/usr/bin/env bash
#
# scripts/provision-demo-users.sh — Provision 12 demo users in Zitadel
#
# Part of the bootstrap process: creates users for an imaginary SME
# company (OpenSME Muster GmbH) for demo and e2e testing purposes.
#
# Users are created with:
#   - Realistic German names across 5 departments
#   - Role-based group memberships
#   - Predefined passwords (CHANGE IN PRODUCTION!)
#   - Email addresses under @<domain>
#
# Idempotent: running twice does NOT duplicate users.
#
# Usage:
#   ./scripts/provision-demo-users.sh              # default: use docker exec to zitadel
#   ./scripts/provision-demo-users.sh --host zitadel.opensme.local
#   ./scripts/provision-demo-users.sh --wait      # wait for Zitadel ready
#   ./scripts/provision-demo-users.sh --clean     # delete ALL demo users first
#   ./scripts/provision-demo-users.sh --dry-run   # show JSON, exit
#
# Environment (overrides, highest precedence first):
#   ZITADEL_HOST:       admin API host:port  (default: opensme-zitadel:8080 via docker)
#   ZITADEL_EXTERNAL:   external host for IdP URLs (default: same as HOST)
#   ZITADEL_singleSignOnDomain: SSO domain (default: opensme.local)
#   ZITADEL_PAT:        machine PAT override (skips docker fetch)
#   E2E_ZITADEL_PAT:    alternate PAT env var
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
        --help|-h)
            echo "Usage: $0 [options]"
            echo "Options:"
            echo "  --host HOST        Zitadel host:port (default: opensme-zitadel:8080 in docker)"
            echo "  --external HOST    External IdP hostname (default: same as --host)"
            echo "  --domain DOM       SSO domain (default: opensme.local)"
            echo "  --wait             Wait for Zitadel to be ready"
            echo "  --clean            Delete all demo users first"
            echo "  --dry-run          Show JSON config and exit"
            echo "  --verbose, -v      Verbose output"
            echo "  --in-docker        Running inside docker (adjusts curl Host header)"
            echo "  --help, -h         This help"
            echo ""
            echo "Environment:"
            echo "  ZITADEL_HOST        Override --host"
            echo "  ZITADEL_EXTERNAL    Override --external"
            echo "  ZITADEL_PAT         Machine PAT (skips docker fetch)"
            echo "  E2E_ZITADEL_PAT     Alternate PAT env var"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Default: talk to the zitadel container via docker network
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
    info "Waiting for Zitadel at https://$EXTERNAL_HOST..."
    for i in $(seq 1 60); do
        if ztcurl -k "https://$CURL_HOST/healthz" >/dev/null 2>&1; then
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
    
    local url="https://$CURL_HOST$path"
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

# ── main provisioning ────────────────────────────────────────────────
main() {
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
        email_local=$(echo "$email" | sed "s/@.*//")
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

main "$@"
