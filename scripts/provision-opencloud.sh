#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# scripts/provision-opencloud.sh — OpenCloud provisioning for openSME demo
#
# Idempotent initialization of OpenCloud (ocis) with demo users, spaces,
# and sharing policies. Runs against a RUNNING opensme-opencloud container.
#
# Usage:
#   ./scripts/provision-opencloud.sh                              # full provision
#   ./scripts/provision-opencloud.sh --dry-run                    # show what would be done
#   ./scripts/provision-opencloud.sh --clean --force              # wipe and re-create
#   ./scripts/provision-opencloud.sh --help                       # show this help
#
# Environment:
#   OPENCLOUD_HOST    - target host (default: opensme-opencloud)
#   OPENCLOUD_PORT    - target port (default: 9200)
#   CLOUD_ADMIN_USER  - admin username (default: admin)
#   CLOUD_ADMIN_PASS  - admin password (default: from OC_ADMIN_PASSWORD env)
#   OC_INVALIDATE_CACHE - set to "true" to invalidate ocis metadata cache
#
# Features:
#   • Healthcheck wait (curl http://localhost:9200/health/ready)
#   • Create admin user (if missing)
#   • Create system users: umfragen-bot, polls-user, portal-user, intercom-bot
#   • Create spaces: Shared/Portal, Shared/Polls, Shared/Intercom, Users
#   • Set demo sharing policies (public shares, link sharing)
#   • Color-coded console output
#   • Idempotent — safe to re-run
#
# The script uses the OCS (Open Collaboration Services) API and WebDAV
# endpoints of OpenCloud/ocis.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -euo pipefail

# ━━━━ color output ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
CYAN='\033[0;36m'; MAGENTA='\033[0;35m'; NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERR]${NC}   $*" >&2; }
debug() { [[ "$VERBOSE" == true ]] && echo -e "${CYAN}[DEBUG]${NC} $*"; }
heading() { echo -e "\n${MAGENTA}=== $* ===${NC}"; }

# ━━━━ defaults ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OPENCLOUD_HOST="${OPENCLOUD_HOST:-opensme-opencloud}"
OPENCLOUD_PORT="${OPENCLOUD_PORT:-9200}"
BASE_URL="http://${OPENCLOUD_HOST}:${OPENCLOUD_PORT}"

# Admin credentials — prefer explicit env, fall back to OC_* vars used by the container
CLOUD_ADMIN_USER="${CLOUD_ADMIN_USER:-${OC_ADMIN_USERNAME:-admin}}"
CLOUD_ADMIN_PASS="${CLOUD_ADMIN_PASS:-${OC_ADMIN_PASSWORD:-}}"

# Demo users to create (username:password:displayName)
declare -A DEMO_USERS=(
    ["umfragen-bot"]="umfragen-bot:Umfragen Bot:umfragen-bot@opensme.local"
    ["polls-user"]="polls-user:Polls User:polls-user@opensme.local"
    ["portal-user"]="portal-user:Portal User:portal-user@opensme.local"
    ["intercom-bot"]="intercom-bot:Intercom Bot:intercom-bot@opensme.local"
)

# Spaces/folders to create
declare -a SPACES=(
    "/Shared/Portal"
    "/Shared/Polls"
    "/Shared/Intercom"
    "/Users"
)

# Flags
CLEAN=false
FORCE=false
DRY_RUN=false
VERBOSE=false
HELP=false

# ━━━━ argument parsing ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
while [[ $# -gt 0 ]]; do
    case "$1" in
        --clean)      CLEAN=true; shift ;;
        --force)      FORCE=true; shift ;;
        --dry-run)    DRY_RUN=true; shift ;;
        --verbose|-v) VERBOSE=true; shift ;;
        --host)       OPENCLOUD_HOST="$2"; shift 2 ;;
        --port)       OPENCLOUD_PORT="$2"; shift 2 ;;
        --admin-user) CLOUD_ADMIN_USER="$2"; shift 2 ;;
        --admin-pass) CLOUD_ADMIN_PASS="$2"; shift 2 ;;
        --help|-h)
            cat <<EOF
Usage: $0 [OPTIONS]

Provision OpenCloud (ocis) with demo users, spaces, and sharing policies.
Target: http://opensme-opencloud:9200 (internal Docker network).

OPTIONS:
  --clean         Delete existing demo users and spaces before re-creating
  --force         Skip idempotency checks (recreate even if exists)
  --dry-run       Show planned actions without executing
  --verbose, -v   Verbose output (shows curl commands)
  --host HOST     OpenCloud hostname/container (default: opensme-opencloud)
  --port PORT     OpenCloud port (default: 9200)
  --admin-user U  Admin username (default: admin or OC_ADMIN_USERNAME)
  --admin-pass P  Admin password (default: OC_ADMIN_PASSWORD env)
  --help, -h      Show this help

ENVIRONMENT:
  CLOUD_ADMIN_USER, CLOUD_ADMIN_PASS - alternate admin credential sources
  OC_ADMIN_USERNAME, OC_ADMIN_PASSWORD - container defaults

EXAMPLES:
  $0                                    # Full provision (idempotent)
  $0 --dry-run                          # Preview actions
  $0 --clean --force                    # Wipe demo data and recreate
EOF
            exit 0
            ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
done

# ━━━━ validation ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if [[ -z "$CLOUD_ADMIN_PASS" ]]; then
    err "Admin password is required. Set CLOUD_ADMIN_PASS or OC_ADMIN_PASSWORD environment variable."
    exit 1
fi

# ━━━━ helper functions ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Basic Auth header generator
basic_auth() {
    local user="$1" pass="$2"
    echo -n "$user:$pass" | base64 -w0
}

ADMIN_AUTH=$(basic_auth "$CLOUD_ADMIN_USER" "$CLOUD_ADMIN_PASS")

# Make a curl request with admin auth
# Usage: ocis_curl GET/POST/PUT/DELETE path [data]
# Returns: HTTP status code
ocis_curl() {
    local method="$1"
    local path="$2"
    local data="${3:-}"
    local url="${BASE_URL}${path}"

    if [[ "$DRY_RUN" == true ]]; then
        debug "DRY-RUN: $method $url"
        if [[ -n "$data" ]]; then
            debug "DRY-RUN: data=$data"
        fi
        return 0
    fi

    debug "curl -s -o /dev/null -w '%{http_code}' -X $method \\"
          -H 'Authorization: Basic $ADMIN_AUTH' \\"
          -H 'OCS-APIRequest: true' \\"
          -H 'Content-Type: application/json' \\"
          '$url'"

    local response
    if [[ -n "$data" ]]; then
        response=$(curl -s -w '\n%{http_code}' -X "$method" \
            -H "Authorization: Basic $ADMIN_AUTH" \
            -H "OCS-APIRequest: true" \
            -H "Content-Type: application/json" \
            -d "$data" \
            "$url" 2>/dev/null) || true
    else
        response=$(curl -s -w '\n%{http_code}' -X "$method" \
            -H "Authorization: Basic $ADMIN_AUTH" \
            -H "OCS-APIRequest: true" \
            "$url" 2>/dev/null) || true
    fi

    # Split body and status
    local http_code
    http_code=$(echo "$response" | tail -n1)
    local body
    body=$(echo "$response" | head -n -1)

    debug "HTTP $http_code"
    if [[ "$VERBOSE" == true ]] && [[ -n "$body" ]] && [[ "$http_code" != "200" ]] && [[ "$http_code" != "201" ]] && [[ "$http_code" != "100" ]]; then
        debug "Response: $body"
    fi

    echo "$http_code"
    return 0
}

# Check if a user exists
# Usage: user_exists username
# Returns: 0 if exists, 1 if not
user_exists() {
    local username="$1"
    
    # In dry-run mode, assume user does not exist so we can show what would be created
    if [[ "$DRY_RUN" == true ]]; then
        debug "DRY-RUN: Assuming user '$username' does not exist"
        return 1
    fi
    
    local path="/ocs/v1.php/cloud/users/${username}"
    local http_code
    http_code=$(ocis_curl GET "$path")
    
    # OCS API: 200 = exists, 404 = not found, 997 = not authenticated
    if [[ "$http_code" == "200" ]]; then
        debug "User '$username' exists"
        return 0
    elif [[ "$http_code" == "404" ]]; then
        debug "User '$username' does NOT exist"
        return 1
    elif [[ "$http_code" == "997" ]] || [[ "$http_code" == "401" ]]; then
        err "Unauthorized — check admin credentials for user check '$username'"
        return 2
    else
        warn "Unexpected HTTP status checking user '$username': $http_code"
        return 1
    fi
}

# Create a user
# Usage: create_user username password display_name
create_user() {
    local username="$1"
    local password="$2"
    local display_name="$3"
    
    # In dry-run mode, just report what would be done
    if [[ "$DRY_RUN" == true ]]; then
        info "DRY-RUN: Would create user: $username ($display_name)"
        echo "  CREDENTIALS: username=$username password=$password"
        return 0
    fi
    
    local path="/ocs/v1.php/cloud/users"
    local data
    data=$(printf '{"userid": "%s", "password": "%s", "email": "%s", "displayname": "%s"}' \
        "$username" "$password" "${username}@opensme.local" "$display_name")
    
    info "Creating user: $username ($display_name)"
    local http_code
    http_code=$(ocis_curl POST "$path" "$data")
    
    # OCS API returns 100 (OK) on success, or 997 (unauthorized), or 101/102/103 for errors
    if [[ "$http_code" == "100" ]] || [[ "$http_code" == "200" ]] || [[ "$http_code" == "201" ]]; then
        ok "Created user: $username"
        echo "  CREDENTIALS: username=$username password=$password"
        return 0
    elif [[ "$http_code" == "997" ]] || [[ "$http_code" == "401" ]]; then
        err "Failed to create user '$username': unauthorized"
        return 1
    elif [[ "$http_code" == "102" ]]; then
        # 102 = user already exists in ocis
        warn "User '$username' already exists (102)"
        return 0
    else
        err "Failed to create user '$username': HTTP $http_code"
        return 1
    fi
}

# Delete a user
# Usage: delete_user username
delete_user() {
    local username="$1"
    local path="/ocs/v1.php/cloud/users/${username}"
    
    info "Deleting user: $username"
    local http_code
    http_code=$(ocis_curl DELETE "$path")
    
    if [[ "$http_code" == "100" ]] || [[ "$http_code" == "200" ]] || [[ "$http_code" == "204" ]]; then
        ok "Deleted user: $username"
        return 0
    elif [[ "$http_code" == "997" ]] || [[ "$http_code" == "401" ]]; then
        err "Failed to delete user '$username': unauthorized"
        return 1
    else
        warn "Could not delete user '$username': HTTP $http_code (may not exist)"
        return 0
    fi
}

# Check if a space path exists via WebDAV PROPFIND
# Usage: space_exists path
# Returns: 0 if exists, 1 if not
space_exists() {
    local path="$1"
    
    # In dry-run mode, assume space does not exist so we can show what would be created
    if [[ "$DRY_RUN" == true ]]; then
        debug "DRY-RUN: Assuming space '$path' does not exist"
        return 1
    fi
    
    # Normalize path: ensure it starts with /
    path="${path#/}"
    path="/${path}"
    
    local webdav_path="/remote.php/dav/spaces/${path}"
    local http_code
    
    # Use PROPFIND to check existence
    local response
    response=$(curl -s -w '\n%{http_code}' -X PROPFIND \
        -H "Authorization: Basic $ADMIN_AUTH" \
        -H "Depth: 0" \
        "${BASE_URL}${webdav_path}" 2>/dev/null) || true
    
    http_code=$(echo "$response" | tail -n1)
    
    if [[ "$http_code" == "207" ]] || [[ "$http_code" == "200" ]]; then
        debug "Space '$path' exists"
        return 0
    elif [[ "$http_code" == "404" ]]; then
        debug "Space '$path' does NOT exist"
        return 1
    else
        debug "Space check '$path': HTTP $http_code"
        return 1
    fi
}

# Create a space via WebDAV MKCOL
# Usage: create_space path
create_space() {
    local path="$1"
    
    # In dry-run mode, just report what would be done
    if [[ "$DRY_RUN" == true ]]; then
        info "DRY-RUN: Would create space: $path"
        return 0
    fi
    
    # Normalize path
    path="${path#/}"
    path="/${path}"
    
    local webdav_path="/remote.php/dav/spaces/${path}"
    
    info "Creating space: $path"
    
    local response
    response=$(curl -s -w '\n%{http_code}' -X MKCOL \
        -H "Authorization: Basic $ADMIN_AUTH" \
        "${BASE_URL}${webdav_path}" 2>/dev/null) || true
    
    local http_code
    http_code=$(echo "$response" | tail -n1)
    
    if [[ "$http_code" == "201" ]] || [[ "$http_code" == "200" ]]; then
        ok "Created space: $path"
        return 0
    elif [[ "$http_code" == "405" ]]; then
        # 405 Method Not Allowed - space might already exist
        warn "Space '$path' may already exist (405)"
        return 0
    else
        warn "Could not create space '$path': HTTP $http_code"
        return 1
    fi
}

# Delete a space via WebDAV DELETE
delete_space() {
    local path="$1"
    path="${path#/}"
    path="/${path}"
    
    local webdav_path="/remote.php/dav/spaces/${path}"
    
    info "Deleting space: $path"
    
    if [[ "$DRY_RUN" == true ]]; then
        debug "DRY-RUN: DELETE ${BASE_URL}${webdav_path}"
        return 0
    fi
    
    # Try to delete - may fail if space doesn't exist
    local response
    response=$(curl -s -w '\n%{http_code}' -X DELETE \
        -H "Authorization: Basic $ADMIN_AUTH" \
        "${BASE_URL}${webdav_path}" 2>/dev/null) || true
    
    local http_code
    http_code=$(echo "$response" | tail -n1)
    
    if [[ "$http_code" == "204" ]] || [[ "$http_code" == "200" ]] || [[ "$http_code" == "404" ]]; then
        ok "Deleted space: $path"
        return 0
    else
        warn "Could not delete space '$path': HTTP $http_code"
        return 0
    fi
}

# Wait for OpenCloud healthcheck
# Usage: wait_for_health [timeout_seconds]
wait_for_health() {
    local timeout="${1:-300}"
    local interval=5
    local elapsed=0
    
    info "Waiting for OpenCloud healthcheck at http://localhost:9200/health/ready (timeout: ${timeout}s)"
    
    while [[ $elapsed -lt $timeout ]]; do
        if curl -s -f "http://localhost:9200/health/ready" >/dev/null 2>&1; then
            ok "OpenCloud is ready"
            return 0
        fi
        sleep $interval
        elapsed=$((elapsed + interval))
        debug "Waiting... ($elapsed/$timeout)"
    done
    
    err "OpenCloud healthcheck failed after ${timeout}s"
    return 1
}

# Configure sharing policies for demo
# Enable public shares and link sharing
configure_sharing_policies() {
    heading "Configuring Sharing Policies"
    
    if [[ "$DRY_RUN" == true ]]; then
        info "DRY-RUN: Would configure sharing policies"
        return 0
    fi
    
    # Enable public sharing via OCS API
    # /ocs/v1.php/apps/files_sharing/api/v1/persistent_public_link
    # First, get current app config
    local path="/ocs/v1.php/apps/files_sharing/config"
    local http_code
    
    # ocis uses its own configuration. Try to set via OCS API
    # For link sharing enable:
    local data='{"key": "allowPublicLink", "value": "true"}'
    http_code=$(ocis_curl POST "/ocs/v2.php/apps/files_sharing/api/v1/settings" "$data")
    
    if [[ "$http_code" == "100" ]] || [[ "$http_code" == "200" ]]; then
        ok "Enabled public link sharing"
    else
        warn "Could not configure public link sharing (HTTP $http_code) — may already be enabled"
    fi
    
    # Enable sharing with anyone
    data='{"key": "allowShareWithLink", "value": "true"}'
    http_code=$(ocis_curl POST "/ocs/v2.php/apps/files_sharing/api/v1/settings" "$data")
    
    if [[ "$http_code" == "100" ]] || [[ "$http_code" == "200" ]]; then
        ok "Enabled link sharing"
    else
        warn "Could not configure link sharing (HTTP $http_code)"
    fi
}

# ━━━━ main provisioning logic ━━━━━━━━━━━━━━━━━━━━━━━━━━━━

heading "OpenCloud Provisioning Script"
info "Target: ${BASE_URL}"
info "Admin user: $CLOUD_ADMIN_USER"

# Step 0: Wait for OpenCloud to be ready (skip wait in dry-run, but still check once)
if [[ "$DRY_RUN" == true ]]; then
    info "DRY-RUN: Skipping healthcheck wait, attempting single check..."
    if ! curl -s -f "http://localhost:9200/health/ready" >/dev/null 2>&1; then
        warn "DRY-RUN: OpenCloud not reachable at http://localhost:9200/health/ready"
        warn "DRY-RUN: This is expected if OpenCloud is not running"
    else
        ok "DRY-RUN: OpenCloud is reachable"
    fi
else
    if ! wait_for_health; then
        err "OpenCloud is not ready. Aborting."
        exit 1
    fi
fi

# Step 1: Clean (if requested)
if [[ "$CLEAN" == true ]]; then
    heading "Cleaning existing demo data"
    
    # Delete demo users (in reverse order for safety)
    for username in "${!DEMO_USERS[@]}"; do
        delete_user "$username"
    done
    
    # Delete demo spaces
    for space in "${SPACES[@]}"; do
        delete_space "$space"
    done
    
    ok "Clean complete"
fi

# Step 2: Create admin user (if missing)
heading "Ensuring admin user exists"
if ! user_exists "$CLOUD_ADMIN_USER"; then
    info "Admin user '$CLOUD_ADMIN_USER' does not exist, creating..."
    create_user "$CLOUD_ADMIN_USER" "$CLOUD_ADMIN_PASS" "OpenCloud Admin"
else
    ok "Admin user '$CLOUD_ADMIN_USER' exists"
fi

# Step 3: Create spaces/folders
heading "Creating spaces and folders"
for space in "${SPACES[@]}"; do
    if [[ "$FORCE" == true ]]; then
        delete_space "$space" 2>/dev/null || true
    fi
    
    if ! space_exists "$space"; then
        create_space "$space"
    else
        ok "Space '$space' already exists"
    fi
done

# Step 4: Create system users
heading "Creating system users"
for username in "${!DEMO_USERS[@]}"; do
    IFS=':' read -r -a user_data <<< "${DEMO_USERS[$username]}"
    password="${user_data[0]}"
    display_name="${user_data[1]}"
    
    if [[ "$FORCE" == true ]]; then
        delete_user "$username" 2>/dev/null || true
    fi
    
    if ! user_exists "$username"; then
        create_user "$username" "$password" "$display_name"
    else
        ok "User '$username' already exists"
    fi
done

# Step 5: Configure sharing policies for demo
configure_sharing_policies

# Step 6: Output credentials
heading "Demo Credentials"
echo ""
echo "OpenCloud Admin:"
echo "  Username: $CLOUD_ADMIN_USER"
echo "  Password: $CLOUD_ADMIN_PASS"
echo ""
echo "System Users:"
for username in "${!DEMO_USERS[@]}"; do
    IFS=':' read -r -a user_data <<< "${DEMO_USERS[$username]}"
    echo "  $username: password='${user_data[0]}' display='${user_data[1]}'"
done
echo ""
echo "Spaces created:"
for space in "${SPACES[@]}"; do
    echo "  $space"
done
echo ""

ok "OpenCloud provisioning complete!"
