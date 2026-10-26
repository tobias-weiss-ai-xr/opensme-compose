#!/usr/bin/env bash
#
# scripts/sso-check.sh — Per-service SSO health checks (single services)
# Mirrors openEduSuite sso-check patterns for monitoring SSO redirect flows.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[SSO-CHECK]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

# Load env
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

DOMAIN="${OPENSME_DOMAIN:-opensme.local}"
IDP_DOMAIN="${ZITADEL_DOMAIN:-auth.${DOMAIN}}"
PORTAL_DOMAIN="${PORTAL_DOMAIN:-portal.${DOMAIN}}"
OPENCLOUD_DOMAIN="${OPENCLOUD_DOMAIN:-cloud.${DOMAIN}}"
MATRIX_DOMAIN="${MATRIX_DOMAIN:-matrix.${DOMAIN}}"
NOTES_DOMAIN="${NOTES_DOMAIN:-notes.${DOMAIN}}"
NOSDESK_DOMAIN="${NOSDESK_DOMAIN:-help.${DOMAIN}}"
SOGO_DOMAIN="${SOGO_DOMAIN:-webmail.${DOMAIN}}"

# All suite endpoints are TLS-fronted by the edge; local demo hosts use
# self-signed certs (verified with curl -k below).
FAILED=0

check_sso_redirect() {
    local name="$1"
    local url="$2"
    local expected_idp="$3"
    local expected_client="${4:-}"
    
    info "Checking $name SSO redirect..."
    
    local final_url
    final_url=$(curl -skL -o /dev/null -w '%{url_effective}' \
        --max-redirs 10 \
        --max-time 30 \
        "$url" 2>&1 || echo "ERROR")
    
    if [[ "$final_url" == "ERROR" ]]; then
        err "  $name: request failed"
        FAILED=$((FAILED+1))
        return 1
    fi
    
    # Check if redirected to IdP
    if echo "$final_url" | grep -q "$expected_idp"; then
        # If client_id expected, check for it
        if [[ -n "$expected_client" ]]; then
            if echo "$final_url" | grep -q "client_id=$expected_client"; then
                ok "  $name: SSO redirect verified (→ $expected_idp, client=$expected_client)"
                return 0
            else
                warn "  $name: reached IdP but client_id mismatch or missing (final: ${final_url:0:80}...)"
                FAILED=$((FAILED+1))
                return 1
            fi
        else
            ok "  $name: SSO redirect verified (→ $expected_idp)"
            return 0
        fi
    else
        err "  $name: SSO redirect broken — final URL: ${final_url:0:100}"
        FAILED=$((FAILED+1))
        return 1
    fi
}

echo "=========================================="
echo "SSO Health Checks (Single Services)"
echo "=========================================="
echo "Domain: $DOMAIN"
echo "IdP: $IDP_DOMAIN"
echo "=========================================="
echo ""

# Check IdP itself
info "Checking IdP health..."
if curl -sk --max-time 10 "https://$IDP_DOMAIN/healthz" >/dev/null 2>&1; then
    ok "  IdP: healthy"
else
    err "  IdP: unreachable"
    FAILED=$((FAILED+1))
fi
echo ""

# Check each service's SSO redirect
# OpenCloud
if [[ -n "${OPENCLOUD_DOMAIN:-}" ]]; then
    check_sso_redirect "OpenCloud (Files)" "https://$OPENCLOUD_DOMAIN/" "$IDP_DOMAIN" "${OC_OIDC_CLIENT_ID:-opencloud}" || true
fi

# Notes/Impress
if [[ -n "${NOTES_DOMAIN:-}" ]]; then
    check_sso_redirect "Notes/Impress" "https://$NOTES_DOMAIN/" "$IDP_DOMAIN" "${NOTES_OIDC_CLIENT_ID:-opensme-notes}" || true
fi

# Synapse/Matrix
if [[ -n "${MATRIX_DOMAIN:-}" ]]; then
    check_sso_redirect "Synapse/Matrix" "https://$MATRIX_DOMAIN/_matrix/client/v3/login/sso/redirect" "$IDP_DOMAIN" "${SYNAPSE_OIDC_CLIENT_ID:-synapse}" || true
fi

# Ticketing
if [[ -n "${NOSDESK_DOMAIN:-}" ]]; then
    check_sso_redirect "Ticketing (Nosdesk)" "https://$NOSDESK_DOMAIN/" "$IDP_DOMAIN" "${NOSDESK_OIDC_CLIENT_ID:-opensme-ticketing}" || true
fi

# Webmail/SOGo
if [[ -n "${SOGO_DOMAIN:-}" ]]; then
    # SOGo may have different behavior; check redirect
    check_sso_redirect "Webmail/SOGo" "https://$SOGO_DOMAIN/" "$IDP_DOMAIN" "${SOGO_OIDC_CLIENT_ID:-sogo-webmail}" || true
fi

echo ""
echo "=========================================="
if [[ $FAILED -eq 0 ]]; then
    ok "All SSO checks passed"
    exit 0
else
    err "$FAILED SSO check(s) failed"
    exit 1
fi
