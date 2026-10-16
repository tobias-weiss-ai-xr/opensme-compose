#!/usr/bin/env bash
# ── openSME — register persistent OIDC clients in Zitadel ────────
# Idempotent, secret-driven bootstrap helper.
#
# Registers (or reuses) the OIDC application a service uses to log in
# through the central IdP, so the service's web login works instead of
# dead-ending at an unregistered client. Safe to run on every boot: if
# the app already exists it reports "already registered" and exits 0
# without touching anything.
#
# Apps (--app):
#   opencloud  cloud.<domain>/oidc/callback(.html)      → OC_OIDC_CLIENT_ID/SECRET
#   notes      notes.<domain>/api/v1.0/callback/ (lasuite oidc_login)
#              → NOTES_OIDC_CLIENT_ID/SECRET
#   synapse    matrix.<domain>/_synapse/client/oidc/callback
#              → SYNAPSE_OIDC_CLIENT_ID/SECRET
#   ticketing  help.<domain>/auth/oidc/callback (Nosdesk)
#              → NOSDESK_OIDC_CLIENT_ID/SECRET
#
# SECRET-DRIVEN: Zitadel's Management API takes no client_id / client_secret
# input — it mints both and returns them (verified against the v4.19 proto;
# AddOIDCApp only ever responds with clientId / clientSecret). This script
# therefore persists the Zitadel-issued pair into .env under the app's own
# keys (see the --app list above), so the service authenticates as the
# registered client. A well-known default is never written: if .env still
# carries a CHANGEME_ placeholder (or no secret at all), a fresh random
# secret is issued and a warning tells you to regenerate/re-source .env;
# Zitadel stays the single source of truth for the deployed secret.
#
# Redirect URIs registered for BOTH protocol faces of the demo stack (Traefik
# terminates TLS at :443 and redirects http :80 -> https):
#   https://<OPENCLOUD_DOMAIN>/oidc/callback   (+ http)   — real OpenCloud OIDC callback
#   https://<OPENCLOUD_DOMAIN>/oidc-callback.html (+ http) — tests/05-e2e run.py probe
# The protocol-relative "//host/oidc/callback" forms are intentionally NOT
# registered: nothing in this repo references them, and Zitadel rejects
# scheme-less URIs, which would fail the whole registration.
#
# Credentials: uses the machine PAT that `zitadel setup --steps /steps.yaml`
# writes to /machinekey/pat — located exactly like tests/05-e2e/run.py does
# (env override first, then docker compose cp from the running zitadel
# container).
#
# Requires: docker, curl, jq, openssl (same toolbox as scripts/demo.sh).
#
# Usage:
#   scripts/zitadel-client.sh --app opencloud   # register/verify (default)
#   scripts/zitadel-client.sh --app notes       # Impress/Notes client
#   scripts/zitadel-client.sh --app synapse     # Matrix/Synapse client
#   scripts/zitadel-client.sh --dry-run         # print the plan, change nothing
#   scripts/zitadel-client.sh --idp-url https://auth.example.test
#   scripts/zitadel-client.sh --help
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC}  $1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

DRY_RUN=false
IDP_URL_OVERRIDE=""
APP_KIND="opencloud"
LOCAL_SUFFIX_RE='(\.local|\.localhost|\.test|\.invalid)$'

usage() {
  cat <<'EOF'
Usage: scripts/zitadel-client.sh [options]

Register (or reuse) a persistent OIDC client in the running Zitadel
instance. Idempotent — safe to run on every boot.

Options:
  --app NAME         which client to ensure: opencloud (default), notes,
                     synapse or ticketing — selects the redirect URIs and
                     the .env keys the Zitadel-issued credentials are
                     persisted under
  --dry-run          print the plan and the resolved configuration, change nothing
  --idp-url URL      override the IdP base URL (default: $IDP_URL from .env,
                     else https://${ZITADEL_DOMAIN})
  --help             show this help

Environment:
  ZITADEL_PAT / E2E_ZITADEL_PAT   machine PAT (skips docker compose cp)
  <APP>_OIDC_CLIENT_ID / SECRET   existing client credentials for the
                                 selected --app (looked up first)

Requires: docker, curl, jq, openssl. The machine PAT written by
`zitadel setup --steps /steps.yaml` to /machinekey/pat is located the same
way tests/05-e2e/run.py finds it. The IdP is reached through the edge
(Traefik); for *.local-style demo hosts the script resolves the IdP host to
loopback, mirroring tests/05-e2e and .github/workflows/ci.yml.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)  DRY_RUN=true; shift ;;
    --idp-url)  IDP_URL_OVERRIDE="${2:-}"; shift 2 ;;
    --app)
      [[ -n "${2:-}" ]] || { err "--app requires a value (opencloud|notes|synapse)"; exit 1; }
      APP_KIND="$2"; shift 2 ;;
    --help|-h)  usage; exit 0 ;;
    *)          err "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

if [[ "$DRY_RUN" == true ]]; then
  # a dry-run must work without docker or a running stack
  for tool in curl jq openssl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      err "Required tool not found: ${tool}"
      exit 1
    fi
  done
else
  for tool in docker curl jq openssl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      err "Required tool not found: ${tool}"
      exit 1
    fi
  done
fi

# ── env lookup: shell env > .env > fallback ─────────────────────────────────
env_get() {
  local key="$1" fallback="${2:-}"
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
  elif [[ -f .env ]] && grep -q "^${key}=" .env; then
    grep -m1 "^${key}=" .env | cut -d= -f2- | tr -d '\r'
  else
    printf '%s' "$fallback"
  fi
}

# ── resolved configuration ──────────────────────────────────────────────────
IDP_URL="$(env_get IDP_URL "")"
if [[ -n "$IDP_URL_OVERRIDE" ]]; then
  IDP_URL="$IDP_URL_OVERRIDE"
fi
if [[ -z "$IDP_URL" ]]; then
  idp_domain="$(env_get ZITADEL_DOMAIN auth.opensme.local)"
  IDP_URL="https://${idp_domain}"
fi
IDP_HOST="$(printf '%s' "$IDP_URL" | sed -E 's#^[a-z]+://##; s#[:/].*$##')"

# ── per-app configuration ────────────────────────────────────────────────
# Each app: Zitadel project + app name, the .env keys the issued pair is
# persisted under, and the callback paths it expects. Callbacks are
# registered for BOTH protocol faces (Traefik terminates TLS at :443 and
# redirects http :80 -> https).
case "$APP_KIND" in
  opencloud)
    APP_PROJECT="opencloud"
    APP_NAME="opencloud"
    CLIENT_ID_KEY="OC_OIDC_CLIENT_ID"
    CLIENT_SECRET_KEY="OC_OIDC_CLIENT_SECRET"
    SECRET_ALIAS_KEY="OC_OIDC_SECRET"   # legacy alias kept in sync
    app_domain="$(env_get OPENCLOUD_DOMAIN "$(env_get OC_DOMAIN cloud.opensme.local)")"
    CLIENT_ID_LABEL="$(env_get OC_OIDC_CLIENT_ID opencloud)"
    base_paths=("/oidc/callback" "/oidc-callback.html")
    ;;
  notes)
    APP_PROJECT="opensme"
    APP_NAME="opensme-notes"
    CLIENT_ID_KEY="NOTES_OIDC_CLIENT_ID"
    CLIENT_SECRET_KEY="NOTES_OIDC_CLIENT_SECRET"
    SECRET_ALIAS_KEY=""
    app_domain="$(env_get NOTES_DOMAIN "notes.opensme.local")"
    CLIENT_ID_LABEL="$(env_get NOTES_OIDC_CLIENT_ID opensme-notes)"
    # lasuite.oidc_login callback: impress's OIDC lives under the API
    # prefix (/api/v1.0/...), NOT mozilla-django-oidc's /oidc/ — verified
    # on the lasuite/impress image (core/urls.py includes lasuite.oidc_login
    # under api/<version>/; reverse('oidc_authentication_init') =
    # /api/v1.0/authenticate/, callback = /api/v1.0/callback/). Registering
    # the wrong URI (e.g. /oidc/callback/) makes Zitadel answer
    # redirect_uri_mismatch on an otherwise working login.
    base_paths=("/api/v1.0/callback/")
    ;;
  synapse)
    APP_PROJECT="opensme"
    APP_NAME="opensme-synapse"
    CLIENT_ID_KEY="SYNAPSE_OIDC_CLIENT_ID"
    CLIENT_SECRET_KEY="SYNAPSE_OIDC_CLIENT_SECRET"
    SECRET_ALIAS_KEY=""
    app_domain="$(env_get MATRIX_DOMAIN "matrix.opensme.local")"
    CLIENT_ID_LABEL="$(env_get SYNAPSE_OIDC_CLIENT_ID synapse)"
    # synapse derives its redirect from public_baseurl — exactly this URI
    base_paths=("/_synapse/client/oidc/callback")
    ;;
  ticketing)
    APP_PROJECT="opensme"
    APP_NAME="opensme-ticketing"
    CLIENT_ID_KEY="NOSDESK_OIDC_CLIENT_ID"
    CLIENT_SECRET_KEY="NOSDESK_OIDC_CLIENT_SECRET"
    SECRET_ALIAS_KEY=""
    # Nosdesk serves the helpdesk under help.<domain>; the OIDC block in
    # services/ticketing.yml wires its redirect to $FRONTEND_URL/auth/oidc/callback
    app_domain="$(env_get NOSDESK_DOMAIN "help.$(env_get OPENSME_DOMAIN opensme.graphwiz.ai)")"
    CLIENT_ID_LABEL="$(env_get NOSDESK_OIDC_CLIENT_ID opensme-ticketing)"
    base_paths=("/auth/oidc/callback")
    ;;
  *)
    err "Unknown --app '$APP_KIND' (opencloud|notes|synapse|ticketing)"
    exit 1
    ;;
esac

REDIRECTS=()
POST_LOGOUT=()
for p in "${base_paths[@]}"; do
  REDIRECTS+=("https://${app_domain}${p}" "http://${app_domain}${p}")
done
POST_LOGOUT=("${REDIRECTS[@]}")

# ── secret placeholder handling (never write a fixed default) ───────────────
SECRET_CANDIDATE="$(env_get "$CLIENT_SECRET_KEY" "$( [[ -n "$SECRET_ALIAS_KEY" ]] && env_get "$SECRET_ALIAS_KEY" || printf '' )")"
if [[ -z "$SECRET_CANDIDATE" || "$SECRET_CANDIDATE" == CHANGEME_* ]]; then
  warn "${CLIENT_SECRET_KEY} in .env is '${SECRET_CANDIDATE:-<empty>}' (missing or a CHANGEME_ placeholder)."
  warn "The registered secret is minted by Zitadel and written back into .env — no fixed default"
  warn "is ever shipped. Regenerate .env (./scripts/init-env.sh --force-env) or re-source it"
  warn "so the deployed secret stays in sync with the registered client."
fi

# ── machine PAT — mirror tests/05-e2e/run.py fetch_seeded() ────────────────
locate_pat() {
  local pat
  pat="${ZITADEL_PAT:-${E2E_ZITADEL_PAT:-}}"
  if [[ -n "$pat" ]]; then
    printf '%s' "$pat"
    return 0
  fi
  local -a sets args
  local files f fetched
  sets=()
  if [[ -n "${COMPOSE_FILE:-}" ]]; then
    sets+=("$COMPOSE_FILE")
  fi
  sets+=("docker-compose.yml idm/zitadel.yml opencloud/opencloud.yml profiles/demo.dev.yml")
  sets+=("docker-compose.yml idm/zitadel.yml")
  for files in "${sets[@]}"; do
    args=()
    for f in ${files}; do
      [[ -n "$f" ]] && args+=(-f "$f")
    done
    if [[ -f .env ]]; then
      args+=(--env-file .env)
    fi
    if fetched="$(docker compose "${args[@]}" cp zitadel:/machinekey/pat - 2>/dev/null \
        | tar -xOf - 2>/dev/null | tr -d '\r\n')"; then
      if [[ -n "$fetched" ]]; then
        printf '%s' "$fetched"
        return 0
      fi
    fi
  done
  return 1
}

PAT=""
if [[ "$DRY_RUN" == true ]]; then
  # best-effort only: a dry-run must not depend on the stack being up
  PAT="$(locate_pat 2>/dev/null || true)"
else
  if ! PAT="$(locate_pat)"; then
    err "No Zitadel machine PAT found (looked in ZITADEL_PAT / E2E_ZITADEL_PAT, then via"
    err "'docker compose cp zitadel:/machinekey/pat'). The PAT is written by"
    err "'zitadel setup --steps /steps.yaml' on a fresh IdP DB. Start the stack first."
    exit 1
  fi
fi

# ── Management API helper (grpc-gateway JSON, exactly the endpoints run.py uses) ──
# The helper runs inside a $(...) substitution (a subshell), so the HTTP status
# is persisted to a temp file instead of a shell global; api_http() reads it back.
API_TMP="$(mktemp "${TMPDIR:-/tmp}/zc-oidc-http.XXXXXX")"
trap 'rm -f "$API_TMP"' EXIT

api() {
  # api METHOD PATH [BODY]  -> prints response body on stdout; code in $API_TMP
  local method="$1" path="$2" body="${3:-}"
  local -a curl_args=(-sS --max-time 20 -k -L
    -H "Authorization: Bearer ${PAT}"
    -H 'Content-Type: application/json'
    -w '\n%{http_code}'
    -X "$method")
  if [[ -n "$body" ]]; then
    curl_args+=(-d "$body")
  fi
  if [[ "$IDP_HOST" =~ $LOCAL_SUFFIX_RE || "$IDP_HOST" == "localhost" || "$IDP_HOST" == "127.0.0.1" ]]; then
    curl_args+=(--resolve "${IDP_HOST}:443:127.0.0.1" --resolve "${IDP_HOST}:80:127.0.0.1")
  fi
  local out
  if ! out="$(curl "${curl_args[@]}" "${IDP_URL}${path}")"; then
    printf '%s' '' > "$API_TMP"
    return 1
  fi
  printf '%s' "$out" | tail -n1 > "$API_TMP"
  printf '%s' "$out" | sed '$d'
}

api_http() { tr -d '\r\n' < "$API_TMP"; }

# ── persist a KEY=value into .env (upsert, atomic via temp file) ────────────
set_env() {
  local key="$1" val="$2"
  local tmp="$PROJECT_DIR/.env.tmp.$$"
  if [[ ! -f .env ]]; then
    err ".env missing — cannot persist ${key}"
    return 1
  fi
  if grep -q "^${key}=" .env; then
    awk -v k="$key" -v v="$val" 'BEGIN{OFS="="} $0 ~ ("^" k "=") { print k, v; next } { print }' .env > "$tmp"
  else
    { cat .env; printf '\n%s=%s\n' "$key" "$val"; } > "$tmp"
  fi
  mv "$tmp" .env
}

# ── --dry-run: print the plan, touch nothing ────────────────────────────────
if [[ "$DRY_RUN" == true ]]; then
  echo ""
  info "Dry run — no API calls, no file writes."
  info "IdP base:      ${IDP_URL}"
  info "App to ensure: '${APP_NAME}' (project '${APP_PROJECT}')"
  info "Client id:     '${CLIENT_ID_LABEL}' (looked up first; persisted to .env as ${CLIENT_ID_KEY})"
  info "Machine PAT:   $([[ -n "$PAT" ]] && echo "found" || echo "NOT FOUND")"
  info "Redirect URIs:"
  for u in "${REDIRECTS[@]}"; do
    info "  - ${u}"
  done
  info "Plan:"
  info "  1. POST ${IDP_URL}/management/v1/projects/_search  (readiness + auth probe)"
  info "  2. scan projects' apps for '$APP_NAME' / client id '$CLIENT_ID_LABEL'"
  info "     -> if found: report 'already registered', exit 0 (no changes)"
  info "  3. otherwise: create project '$APP_PROJECT' (if needed) and the OIDC app"
  info "     (web, authorization-code, client_secret_basic, devMode, both http+https URIs)"
  info "  4. write ${CLIENT_ID_KEY} + ${CLIENT_SECRET_KEY} into .env"
  exit 0
fi

# ── 1) readiness + auth probe via the proven project-search endpoint ────────
# Docker's health state goes green slightly before the management API answers:
# right after `zitadel setup` the _search route can still return 404 for a
# short window, so transient failures (connect / 404 / 5xx) are retried and
# only credential errors (401/403) abort immediately.
projects_json=""
code=""
for _ in $(seq 1 24); do
  if projects_json="$(api POST /management/v1/projects/_search '{}' 2>/dev/null)"; then
    code="$(api_http)"
    [[ "$code" == "200" ]] && break
    [[ "$code" == "401" || "$code" == "403" ]] && break
  fi
  sleep 5
done
if [[ "$code" == "200" ]]; then
  :  # ready
elif [[ "$code" == "401" || "$code" == "403" ]]; then
  err "Zitadel rejected the machine PAT (HTTP ${code}). Check /machinekey/pat."
  exit 1
else
  warn "IdP management API not ready at ${IDP_URL} after retries" \
       "(last HTTP ${code:-no-response})."
  warn "Start the stack first (./scripts/demo.sh or make up), then rerun this script."
  exit 0
fi

# ── 2) idempotent lookup by persisted client id, then by app name ───────────
FOUND_PROJECT=""
FOUND_APP=""
FOUND_CLIENT_ID=""
while IFS= read -r pid; do
  [[ -n "$pid" ]] || continue
  apps_json="$(api POST "/management/v1/projects/${pid}/apps/_search" '{}' 2>/dev/null || true)"
  [[ -n "$apps_json" ]] || continue
  hit="$(printf '%s' "$apps_json" | jq -r --arg cid "$CLIENT_ID_LABEL" --arg name "$APP_NAME" \
    '.result[]? | select((.oidcConfig.clientId // "") == $cid or .name == $name) | .id' 2>/dev/null | head -1)"
  if [[ -n "$hit" ]]; then
    FOUND_PROJECT="$pid"
    FOUND_APP="$hit"
    FOUND_CLIENT_ID="$(printf '%s' "$apps_json" | jq -r --arg id "$hit" '.result[]? | select(.id == $id) | .oidcConfig.clientId // empty' 2>/dev/null | head -1)"
    break
  fi
done <<< "$(printf '%s' "$projects_json" | jq -r '.result[]?.id' 2>/dev/null || true)"

if [[ -n "$FOUND_APP" ]]; then
  ok "OIDC app '${APP_NAME}' already registered (client id '${FOUND_CLIENT_ID:-?}', project '${FOUND_PROJECT}')"
  info "Nothing to do — idempotent no-op. The client credentials in .env are the working ones."
  exit 0
fi

# ── 3) ensure the project, then register the app ─────────────────────────────
pid="$(printf '%s' "$projects_json" | jq -r --arg p "$APP_PROJECT" '.result[]? | select(.name == $p) | .id' 2>/dev/null | head -1)"
if [[ -z "$pid" ]]; then
  info "Creating project '${APP_PROJECT}' in Zitadel..."
  proj_resp="$(api POST /management/v1/projects "{\"name\":\"${APP_PROJECT}\"}")"
  case "$(api_http)" in
    200) pid="$(printf '%s' "$proj_resp" | jq -r '.id // empty' 2>/dev/null)" ;;
    *)
      err "Create project failed (HTTP $(api_http)): $(printf '%s' "$proj_resp" | head -c 200)"
      exit 1 ;;
  esac
  if [[ -z "$pid" ]]; then
    err "Create project returned no project id."
    exit 1
  fi
fi

app_body="$(jq -nc --arg name "$APP_NAME" \
  --argjson redirects "$(printf '%s\n' "${REDIRECTS[@]}" | jq -R . | jq -s -c .)" \
  --argjson logout "$(printf '%s\n' "${POST_LOGOUT[@]}" | jq -R . | jq -s -c .)" \
  '{ name: $name,
     redirectUris: $redirects,
     postLogoutRedirectUris: $logout,
     responseTypes: ["OIDC_RESPONSE_TYPE_CODE"],
     grantTypes: ["OIDC_GRANT_TYPE_AUTHORIZATION_CODE"],
     appType: "OIDC_APP_TYPE_WEB",
     authMethodType: "OIDC_AUTH_METHOD_TYPE_BASIC",
     devMode: true }')"

info "Registering OIDC app '${APP_NAME}' in project '${APP_PROJECT}'..."
app_resp="$(api POST "/management/v1/projects/${pid}/apps/oidc" "$app_body")"
case "$(api_http)" in
  200) ;;
  *)
    err "Add OIDC app failed (HTTP $(api_http)): $(printf '%s' "$app_resp" | head -c 300)"
    exit 1 ;;
esac

new_client_id="$(printf '%s' "$app_resp" | jq -r '.clientId // empty' 2>/dev/null)"
new_secret="$(printf '%s' "$app_resp" | jq -r '.clientSecret // empty' 2>/dev/null)"
if [[ -z "$new_client_id" || -z "$new_secret" ]]; then
  err "Zitadel did not return clientId/clientSecret for the new app (authMethod BASIC should return both)."
  exit 1
fi

# ── 4) persist the Zitadel-issued credentials into .env ──────────────────
set_env "$CLIENT_ID_KEY" "$new_client_id"
set_env "$CLIENT_SECRET_KEY" "$new_secret"
if [[ -n "$SECRET_ALIAS_KEY" ]] && grep -q "^${SECRET_ALIAS_KEY}=" .env; then
  # legacy alias kept in sync for older .env files / overlay consumers
  set_env "$SECRET_ALIAS_KEY" "$new_secret"
fi

echo ""
ok "Registered persistent OIDC client (${APP_KIND})."
info "  Project:          ${APP_PROJECT}  (id ${pid})"
info "  App:              ${APP_NAME}  (id ${FOUND_APP:-new})"
info "  Client id:        ${new_client_id}"
info "  Client secret:    <set> (written to .env, never printed)"
info "  Redirect URIs:    $(IFS=', '; echo "${REDIRECTS[*]}")"
info "  Post-logout URIs: $(IFS=', '; echo "${POST_LOGOUT[*]}")"
info "Persisted into .env: ${CLIENT_ID_KEY} and ${CLIENT_SECRET_KEY}"
warn "Re-source .env (or restart the stack) so the service picks up the new credentials."
