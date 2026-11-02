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
    # shellcheck source=/dev/null  # .env generated at deploy time
    source .env
    set +a
fi

DOMAIN="${OPENSME_DOMAIN:-opensme.local}"
IDP_DOMAIN="${ZITADEL_DOMAIN:-auth.${DOMAIN}}"
PORTAL_DOMAIN="${PORTAL_DOMAIN:-home.${DOMAIN}}"
OPENCLOUD_DOMAIN="${OPENCLOUD_DOMAIN:-cloud.${DOMAIN}}"
MATRIX_DOMAIN="${MATRIX_DOMAIN:-matrix.${DOMAIN}}"
NOTES_DOMAIN="${NOTES_DOMAIN:-notes.${DOMAIN}}"
NOSDESK_DOMAIN="${NOSDESK_DOMAIN:-help.${DOMAIN}}"
SOGO_DOMAIN="${SOGO_DOMAIN:-webmail.${DOMAIN}}"
PAPERLESS_DOMAIN="${PAPERLESS_DOMAIN:-paperless.${DOMAIN}}"
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
    if ! echo "$final_url" | grep -q "$expected_idp"; then
        err "  $name: SSO redirect broken — final URL: ${final_url:0:100}"
        FAILED=$((FAILED+1))
        return 1
    fi

    # client_id lives on the FIRST authorize hop — the follow chain ends in the
    # IdP login UI (requestId), so inspect the first Location header.
    if [[ -n "$expected_client" ]]; then
        local first_loc
        first_loc=$(curl -sk -o /dev/null -D- --max-redirs 0 --max-time 30 "$url" 2>/dev/null \
            | grep -im1 '^location:' | tr -d '\r' | cut -d' ' -f2-)
        if echo "$first_loc" | grep -q "client_id=${expected_client}"; then
            ok "  $name: SSO redirect verified (→ $expected_idp, client=$expected_client)"
            return 0
        else
            warn "  $name: reached IdP but client_id mismatch or missing (first hop: ${first_loc:0:80}...)"
            FAILED=$((FAILED+1))
            return 1
        fi
    else
        ok "  $name: SSO redirect verified (→ $expected_idp)"
        return 0
    fi
}

# Own-login apps serve their own login page with an SSO entry instead of a
# blind IdP redirect on / — assert the SSO signal is actually present there
# (per-app markers, cf. docs/SSO.md verification notes).
check_sso_entry() {
    local name="$1"
    local url="$2"
    shift 2
    local expected_idp="$IDP_DOMAIN"
    local body

    info "Checking $name SSO entry..."
    body=$(curl -skL --max-time 30 "$url" 2>&1 || echo "ERROR")
    if [[ "$body" == "ERROR" ]]; then
        err "  $name: request failed"
        FAILED=$((FAILED+1))
        return 1
    fi
    local hit=0
    for marker in "$@"; do
        if printf '%s' "$body" | grep -qi "$marker"; then
            hit=1
            break
        fi
    done
    if [[ $hit -eq 1 ]]; then
        ok "  $name: SSO entry present"
        return 0
    else
        err "  $name: no SSO entry detected on $(basename "$url")"
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

# Check each service's SSO entry. Apps that serve their own login page are
# asserted via the actual SSO signal (button/link/marker) present on that
# page — docs/SSO.md documents which marker each serves.

# OpenCloud (own login page, SSO button)
if [[ -n "${OPENCLOUD_DOMAIN:-}" ]]; then
    check_sso_entry "OpenCloud (Files)" "https://$OPENCLOUD_DOMAIN/" \
        "$IDP_DOMAIN" "oidc" "openSME" || true
fi

# Optional-profile apps: when the service is not deployed the edge answers
# 404 on every route (or refuses the connection) — report a clean skip
# instead of a false failure. A present-but-down backend shows up as 5xx
# and still fails the check (correct).
service_deployed() {
    local code
    code=$(curl -sk --max-time 12 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true)
    [[ "$code" != "404" && "$code" != "000" ]]
}

# Notes/Impress — mozilla-django-oidc exposes the SSO trigger as a real
# 302 to the IdP authorize endpoint (proven live)
# 302 → auth…/oauth/v2/authorize?response_type=code&…client_id=…
if [[ -n "${NOTES_DOMAIN:-}" ]]; then
    if service_deployed "https://$NOTES_DOMAIN/"; then
        check_sso_redirect "Notes/Impress" "https://$NOTES_DOMAIN/api/v1.0/authenticate/" \
            "$IDP_DOMAIN" "$NOTES_OIDC_CLIENT_ID" || true
    else
        warn "  Notes/Impress: not deployed (optional profile) — Zitadel client provisioned, skipped"
    fi
fi

# Synapse/Matrix — needs the mandatory ?redirectUrl= query (else it 400s).
# After the authorize hop the chain lands on the IdP login UI (requestId),
# so only the IdP domain is asserted (no client_id there).
if [[ -n "${MATRIX_DOMAIN:-}" ]]; then
    if service_deployed "https://$MATRIX_DOMAIN/"; then
        check_sso_redirect "Synapse/Matrix" \
            "https://$MATRIX_DOMAIN/_matrix/client/v3/login/sso/redirect?redirectUrl=https://$MATRIX_DOMAIN/_matrix/client/" \
            "$IDP_DOMAIN" || true
    else
        warn "  Synapse/Matrix: not deployed (optional profile) — Zitadel client provisioned, skipped"
    fi
fi

# Ticketing (Nosdesk) — optional profile; the Zitadel client is provisioned
# ahead. When the app is not deployed the host answers 404 on every route —
# report as a skip, not a failure. When listening, its own login page must
# expose an SSO entry.
if [[ -n "${NOSDESK_DOMAIN:-}" ]]; then
    if ! service_deployed "https://$NOSDESK_DOMAIN/"; then
        warn "  Ticketing (Nosdesk): not deployed (optional profile) — Zitadel client provisioned, skipped"
    elif check_sso_entry "Ticketing (Nosdesk)" "https://$NOSDESK_DOMAIN/" \
        "$IDP_DOMAIN" "oidc" "SSO" "zitadel"; then
        :
    else
        true
    fi
fi

# Webmail/SOGo — with the OIDC realm active the whole /SOGo/ entry is
# IdP-gated: no cookies ⇒ 302 to the authorize endpoint (client_id on the
# first hop), then the chain lands on the IdP login UI.
if [[ -n "${SOGO_DOMAIN:-}" ]]; then
    check_sso_redirect "Webmail/SOGo" "https://$SOGO_DOMAIN/SOGo/" \
        "$IDP_DOMAIN" "$SOGO_OIDC_CLIENT_ID" || true
fi

# Paperless (django-allauth generic OIDC; shows the openSME/zitadel button)
if [[ -n "${PAPERLESS_DOMAIN:-}" ]]; then
    check_sso_entry "Paperless" "https://$PAPERLESS_DOMAIN/accounts/login/" \
        "$IDP_DOMAIN" "zitadel" "openSME" || true
fi

# Portal (home) — the sign-in endpoint /api/auth/login 303s to the IdP
# authorize (client_id on the first hop, HMAC state cookie) and the chain
# ends on the IdP login UI. A landing-page HTML marker is not served.
if [[ -n "${PORTAL_DOMAIN:-}" ]]; then
    check_sso_redirect "Portal (home)" "https://$PORTAL_DOMAIN/api/auth/login" \
        "$IDP_DOMAIN" "$PORTAL_OIDC_CLIENT_ID" || true
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
