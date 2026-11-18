#!/usr/bin/env bash
#
# scripts/suite-monitor.sh — cron-driven live monitor for the openSME suite.
#
# Born from the 2026-11-18 openCloud incident: the registrar's split
# oidc_config PUTs silently flipped the OAuth access-token type back to
# OPAQUE, every login died with 'failed to verify access token: token
# contains an invalid number of segments', and nothing noticed for days.
# The host watchdog only relaunches host-side portal ports; the external
# uptime prober only checks status codes — neither sees token types,
# auth-error signatures, or request durations.
#
# Checks (each alerts on failure, with a cooldown to avoid mail storms):
#   C1 container health   — core opensme-* containers must be Up (healthy)
#   C2 edge latency       — page loads / IdP discovery under MAX_EDGE_SEC
#   C3 OIDC token type    — accessTokenType must be JWT(1) in the Zitadel
#                           event store for every client in MONITOR_JWT_APPS
#   C4 auth-error scan    — opencloud logs: opaque-token signatures in window
#   C5 slow-request scan  — opencloud access-log duration= > MAX_API_SEC
#                           (caught the 12-14s PROPFIND /dav/spaces regression)
#
# Cron wiring (root):
#   */5 * * * * /opt/opensme-compose/compose/scripts/suite-monitor.sh >> /var/log/opensme-suite-monitor.log 2>&1
#
# Requires: exim configured as 'internet' site (dc_eximconfig_configtype) —
# the 2026-11-18 default 'local' config silently swallowed all alert mail
# ('Mailing to remote domains not supported' into /var/mail/mail).
#
# Env overrides:
#   ALERT_TO         recipient (default tobias@tobias-weiss.org)
#   WINDOW_MIN       log-scan window in minutes (default 10)
#   MAX_EDGE_SEC     edge page-load budget seconds (default 5)
#   MAX_API_SEC      opencloud request budget seconds (default 10)
#   COOLDOWN_SEC     re-alert interval for the same failure (default 3600)
#   MONITOR_JWT_APPS space-separated Zitadel CLIENT ids that must mint JWT
#                    tokens (default: OC_OIDC_CLIENT_ID from .env)
#   STATE_DIR        cooldown state dir (default /var/lib/opensme-monitor)
#   MONITOR_LOG      log file (default /var/log/opensme-suite-monitor.log)
#   SUPPRESS_MAIL    set to 1 for dry-runs (log only, no mail)
#
set -uo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$COMPOSE_DIR"

ALERT_TO="${ALERT_TO:-tobias@tobias-weiss.org}"
WINDOW_MIN="${WINDOW_MIN:-10}"
MAX_EDGE_SEC="${MAX_EDGE_SEC:-5}"
MAX_API_SEC="${MAX_API_SEC:-10}"
COOLDOWN_SEC="${COOLDOWN_SEC:-3600}"
STATE_DIR="${STATE_DIR:-/var/lib/opensme-monitor}"
MONITOR_LOG="${MONITOR_LOG:-/var/log/opensme-suite-monitor.log}"
MAIL_FROM="suite-monitor@openedusuite.org"

# load .env without choking on values with spaces (OAUTH_SCOPES quirk)
if [[ -f .env ]]; then
    while IFS= read -r line; do
        [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*=[^\"[:space:]]*$ ]] && export "$line"
    done < .env
fi

DOMAIN="${OPENSME_DOMAIN:-opensme.local}"
PORTAL_DOMAIN="${PORTAL_DOMAIN:-home.${DOMAIN}}"
OPENCLOUD_DOMAIN="${OPENCLOUD_DOMAIN:-cloud.${DOMAIN}}"
SOGO_DOMAIN="${SOGO_DOMAIN:-webmail.${DOMAIN}}"
IDP="https://${ZITADEL_DOMAIN:-auth.home.${DOMAIN}}"
ZITADEL_PAT="${ZITADEL_PAT:-}"

log()  { echo "$(date '+%F %T') $*" | tee -a "$MONITOR_LOG"; }

# ── alert plumbing: mail with per-failure cooldown ──────────────────
mkdir -p "$STATE_DIR" 2>/dev/null || true

maybe_alert() {
    local key="$1" msg="$2"
    local fp="$STATE_DIR/${key}.alert"
    local now; now=$(date +%s)
    if [[ -f "$fp" ]]; then
        local last; last=$(cat "$fp" 2>/dev/null || echo 0)
        if (( now - last < COOLDOWN_SEC )); then
            log "ALERT (suppressed, cooldown): $key — $msg"
            return
        fi
    fi
    echo "$now" > "$fp"
    log "ALERT: $key — $msg"
    if [[ "${SUPPRESS_MAIL:-0}" == "1" ]]; then
        return
    fi
    echo -e "openSME suite-monitor failure on $(hostname -f 2>/dev/null || hostname)\n\n$key\n$msg\n\nCheck: $MONITOR_LOG\nTime: $(date -Is)" \
        | mail -r "$MAIL_FROM" -s "[openSME] suite-monitor FAIL: $key" "$ALERT_TO" \
        && log "alert mail queued to $ALERT_TO for $key" \
        || log "ERROR: alert mail to $ALERT_TO failed for $key (is exim an internet site?)"
}

clear_alert() {
    rm -f "$STATE_DIR/$1.alert" 2>/dev/null || true
}

# ── collect failures ────────────────────────────────────────────────
declare -a FAIL_KEYS=()
declare -a FAIL_MSGS=()
fail() { FAIL_KEYS+=("$1"); FAIL_MSGS+=("$2"); }

# C1 — container health
C1_CONTAINERS="${C1_CONTAINERS:-opensme-opencloud opensme-zitadel opensme-sogo6-server opensme-sogo6-ui opensme-sogo6-postgres opensme-stalwart opensme-portal}"
bad_containers=()
for c in $C1_CONTAINERS; do
    state=$(docker ps --filter "name=^${c}$" --format '{{.Status}}' 2>/dev/null)
    if [[ -z "$state" ]]; then
        bad_containers+=("$c: missing")
    elif [[ "$state" != Up\ * ]]; then
        bad_containers+=("$c: $state")
    elif [[ "$state" == *unhealthy* ]]; then
        bad_containers+=("$c: $state")
    fi
done
if (( ${#bad_containers[@]} )); then
    fail "C1-container-health" "core containers not healthy: ${bad_containers[*]}"
else
    log "C1 ok: ${C1_CONTAINERS//opensme-/} healthy"
fi

# C2 — edge latency
while read -r label url expect; do
    readarray -t res < <(curl -sk -o /dev/null -w "%{http_code}\n%{time_total}" --max-time $((MAX_EDGE_SEC * 2)) "$url" 2>/dev/null)
    code="${res[0]:-000}"; dur="${res[1]:-999}"
    ok_code=0
    for e in $expect; do [[ "$code" == "$e" ]] && ok_code=1; done
    if (( ! ok_code )); then
        fail "C2-edge-$label" "GET $url -> HTTP $code (expected $expect)"
    elif python3 -c "import sys; sys.exit(0 if float('${dur:-999}') <= float('$MAX_EDGE_SEC') else 1)" 2>/dev/null; then
        log "C2 ok: $label ${dur}s HTTP $code"
    else
        fail "C2-edge-$label" "GET $url took ${dur}s (budget ${MAX_EDGE_SEC}s), HTTP $code"
    fi
done <<EOF
opencloud https://${OPENCLOUD_DOMAIN}/ 200 302 401
webmail https://${SOGO_DOMAIN}/sogo6 200 302 307 400 401
portal https://${PORTAL_DOMAIN}/ 200 302
idp-discovery ${IDP}/.well-known/openid-configuration 200
EOF

# C3 — OIDC token type via Zitadel event store (the R15 regression guard)
JWT_APPS="${MONITOR_JWT_APPS:-${OC_OIDC_CLIENT_ID:-}}"
if [[ -n "$ZITADEL_PAT" && -n "$JWT_APPS" ]]; then
    events=$(curl -sk -X POST "$IDP/admin/v1/events/_search" \
        -H "Authorization: Bearer $ZITADEL_PAT" -H 'Content-Type: application/json' \
        --max-time 20 -d '{"queries":[{"aggregateTypes":["app"],"limit":200,"desc":true}]}' 2>/dev/null)
    for client in $JWT_APPS; do
        # resolve client id -> app id (events carry the app aggregate id)
        app_id=$(curl -sk -X POST "$IDP/management/v1/projects/_search" \
            -H "Authorization: Bearer $ZITADEL_PAT" -H 'Content-Type: application/json' \
            --max-time 20 -d '{}' 2>/dev/null \
            | jq -r '.result[]?.id' 2>/dev/null \
            | while read -r pid; do
                curl -sk -X POST "$IDP/management/v1/projects/$pid/apps/_search" \
                    -H "Authorization: Bearer $ZITADEL_PAT" -H 'Content-Type: application/json' \
                    --max-time 20 -d '{}' 2>/dev/null \
                    | jq -r --arg c "$client" '.result[]? | select(.oidcConfig.clientId == $c) | .id' 2>/dev/null
              done | head -1)
        if [[ -z "$app_id" ]]; then
            fail "C3-token-type-$client" "client $client not found in Zitadel — OIDC app missing/deregistered"
            continue
        fi
        # latest event with an accessTokenType payload for this app
        tok=$(echo "$events" | jq -r --arg a "$app_id" '
            [.events[] | select(.payload.accessTokenType != null)
                         | select(.aggregate.id == $a or .payload.appId == $a)]
            | sort_by(.creationDate) | last | .payload.accessTokenType // "never"' 2>/dev/null)
        if [[ "$tok" == "1" ]]; then
            log "C3 ok: client $client mints JWT access tokens"
        elif [[ "$tok" == "never" ]]; then
            fail "C3-token-type-$client" "no accessTokenType event for app $app_id — client likely mints OPAQUE tokens (Zitadel v4.19 default); JWKS-validating backends will reject every login ('invalid number of segments')"
        else
            fail "C3-token-type-$client" "app $app_id accessTokenType=$tok (1=JWT required) — an oidc_config PUT reset the token type; re-run scripts/zitadel-client.sh"
        fi
    done
else
    log "C3 skip: ZITADEL_PAT or OC_OIDC_CLIENT_ID not set"
fi

# C4 — opencloud opaque-token error signature within window
since=$(date -u -d "-${WINDOW_MIN} min" '+%Y-%m-%dT%H:%M:%SZ')
auth_errs=$(docker logs opensme-opencloud --since "$since" 2>&1 \
    | grep -c "failed to verify access token" || true)
if (( auth_errs > 0 )); then
    fail "C4-auth-errors" "$auth_errs 'failed to verify access token' errors in the last ${WINDOW_MIN}min — tokens failing JWKS validation (opaque-token regression or stale browser tokens)"
else
    log "C4 ok: no auth errors in ${WINDOW_MIN}min"
fi

# C5 — opencloud slow-request scan within window
slow=$(docker logs opensme-opencloud --since "$since" 2>&1 \
    | grep "access-log" \
    | awk -v max="$MAX_API_SEC" '
        {
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^duration=/) {
                    d = substr($i, 10) + 0
                    if (d > max) { print $0; break }
                }
            }
        }' | head -5)
if [[ -n "$slow" ]]; then
    fail "C5-slow-requests" "requests exceeded ${MAX_API_SEC}s in the last ${WINDOW_MIN}min: $(echo "$slow" | grep -oE 'duration=[0-9.]+ .*method=[A-Z]+ path=[^ ]+' | sed -E 's/line=[^ ]+ //' | tr '\n' '; ')"
else
    log "C5 ok: no requests over ${MAX_API_SEC}s in ${WINDOW_MIN}min"
fi

# ── resolve ─────────────────────────────────────────────────────────
if (( ${#FAIL_KEYS[@]} )); then
    for i in "${!FAIL_KEYS[@]}"; do
        maybe_alert "${FAIL_KEYS[$i]}" "${FAIL_MSGS[$i]}"
    done
    log "FAIL: ${#FAIL_KEYS[@]} check(s) failing: ${FAIL_KEYS[*]}"
    exit 1
fi

# recovery notices for previously-failing checks
for fp in "$STATE_DIR"/*.alert; do
    [[ -e "$fp" ]] || break
    key=$(basename "$fp" .alert)
    rm -f "$fp"
    log "RECOVERED: $key"
    if [[ "${SUPPRESS_MAIL:-0}" != "1" ]]; then
        echo -e "openSME suite-monitor check '$key' recovered on $(hostname)\n\nTime: $(date -Is)" \
            | mail -r "$MAIL_FROM" -s "[openSME] suite-monitor RECOVERED: $key" "$ALERT_TO" || true
    fi
done
log "PASS: all checks green"
exit 0
