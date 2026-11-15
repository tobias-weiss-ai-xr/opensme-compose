#!/usr/bin/env bash
#
# limesurvey-rpc.sh — LimeSurvey RemoteControl JSON-RPC CLI (ops).
#
# Mirrors the portal's server-side client
# (portal/web/src/lib/limesurvey.ts): POSTs {method, params, id} envelopes to
# <SURVEYS_URL>/admin/remotecontrol and reports LimeSurvey's `result.status`
# error convention. Secrets are never echoed; session keys are only printed
# when the `call` subcommand is used with explicit raw params.
#
# Usage: limesurvey-rpc.sh <subcommand> [args]
#
# Subcommands:
#   check                        health check (auth + survey count)
#   session                      acquire a session key — prints STATUS only
#                                (key length), never the key itself
#   list-surveys                 list surveys (id, title, active)
#   summary <sid>                response/token statistics for one survey
#   questions <sid>              list questions of one survey
#   release                      release the server-side session key
#   call <method> <params-json>  raw RPC call, params as JSON array
#                                (e.g. '["SESSIONKEY",100000]')
#   enable-rpc [--restart]       patch config.php inside the container:
#                                'RPCInterface'=>'json' inside the returned
#                                array's 'config'=>array(...) block
#                                (idempotent; top-level $config assignments
#                                are discarded by LimeSurvey). config.php is
#                                LOST on container recreate — re-run after
#                                recreates/upgrades.
#
# Credential/URL resolution (first hit wins):
#   1. LS_USER / LS_PASSWORD / LS_RPC_URL / LS_SURVEYS_URL environment
#   2. SURVEYS_ADMIN_USER / SURVEYS_ADMIN_PASSWORD / SURVEYS_URL environment
#   3. deployed compose env file (see LS_ENV_FILE below)
#
# Requirements: bash, curl, jq (docker only for enable-rpc).

set -euo pipefail
# Failures inside $( ) command substitutions must abort the script, too
# (without this shopt a dying get_key would be swallowed by the subshell).
shopt -s inherit_errexit 2>/dev/null || true

CONTAINER="${LS_CONTAINER:-opensme-limesurvey}"
CONFIG_PATH="${LS_CONFIG_PATH:-/var/www/html/application/config/config.php}"
ENV_FILE="${LS_ENV_FILE:-/opt/opensme-compose/compose/.env}"
CURL_MAX_TIME=20

die() { echo "Fehler: $*" >&2; exit 1; }

usage() { grep '^#   ' "$0" | sed 's/^#   //'; exit 0; }

# Strip CR and surrounding quotes from a .env value.
env_value() {
  local v="${1%$'\r'}"
  v="${v%\"}"; v="${v#\"}"
  v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}

# The deployed compose env is root-readable only (Ansible Vault secrets) —
# fall back to non-interactive sudo; if that fails the caller's env is used.
read_env_file() {
  if [ -r "$ENV_FILE" ]; then
    cat "$ENV_FILE"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -n cat "$ENV_FILE" 2>/dev/null || true
  fi
}

load_env_file() {
  local line key value
  # Process substitution: the loop stays in this shell (assignments persist).
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in '' |\#*) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      SURVEYS_ADMIN_USER | SURVEYS_ADMIN_PASSWORD | SURVEYS_URL)
        if [ -z "${!key:-}" ]; then
          printf -v "$key" '%s' "$(env_value "$value")"
        fi
        ;;
    esac
  done < <(read_env_file)
}

load_env_file
RPC_USER="${LS_USER:-${SURVEYS_ADMIN_USER:-admin}}"
RPC_PASSWORD="${LS_PASSWORD:-${SURVEYS_ADMIN_PASSWORD:-}}"
SURVEYS_BASE="${LS_SURVEYS_URL:-${SURVEYS_URL:-}}"
RPC_URL="${LS_RPC_URL:-${SURVEYS_BASE:+${SURVEYS_BASE%/}/admin/remotecontrol}}"
RPC_ID=0

require_deps() {
  command -v jq >/dev/null 2>&1 || die "jq wird benötigt."
  command -v curl >/dev/null 2>&1 || die "curl wird benötigt."
  [ -n "$RPC_URL" ] || die "RPC-URL unbekannt — LS_RPC_URL oder SURVEYS_URL setzen."
  [ -n "$RPC_PASSWORD" ] || die "Kein Passwort — LS_PASSWORD oder SURVEYS_ADMIN_PASSWORD setzen (Fallback: $ENV_FILE)."
}

# rpc <method> <params-json-array> — one JSON-RPC round trip, raw envelope.
rpc() {
  RPC_ID=$((RPC_ID + 1))
  local payload
  payload="$(jq -nc --arg m "$1" --argjson p "${2:-[]}" --argjson id "$RPC_ID" \
    '{method: $m, params: $p, id: $id}')"
  curl -fsS --max-time "$CURL_MAX_TIME" -X POST \
    -H 'Content-Type: application/json' \
    --data "$payload" "$RPC_URL"
}

# rpc_key <method> <extra-params-json> — params prefixed with a fresh key.
# get_key runs OUTSIDE command substitution assignment pitfalls: its failure
# must abort the whole script (subshell dies would be swallowed otherwise).
rpc_key() {
  RPC_ID=$((RPC_ID + 1))
  local key payload
  key="$(get_key)"
  # Inline --data (NOT --data @-): a piped body makes curl use chunked
  # transfer-encoding, which the PHP/Yii RPC controller cannot read — it
  # sees an empty body and answers an empty HTTP 200. Inline data sends a
  # Content-Length, which LS parses correctly.
  payload="$(jq -nc --arg k "$key" --arg m "$1" --argjson rest "${2:-[]}" \
    --argjson id "$RPC_ID" '{method: $m, params: ([$k] + $rest), id: $id}')"
  curl -fsS --max-time "$CURL_MAX_TIME" -X POST \
    -H 'Content-Type: application/json' --data "$payload" "$RPC_URL"
}

# get_key — acquire a session key; dies with LimeSurvey's status message.
# Verified LS6 behavior: get_session_key REQUIRES the object form
# {username, password} — the positional array form answers HTTP 200 with an
# EMPTY body. Same for wrong credentials (empty 200 body); a rejected key
# arrives as result:{status:"Invalid session key"}.
get_key() {
  local res payload rtype
  # Inline --data (NOT --data @-): see rpc_key — chunked bodies arrive
  # empty at the PHP RPC controller. id MUST be non-zero: LS treats a
  # falsy JSON-RPC id as a notification and answers an EMPTY 200 body.
  payload="$(jq -nc --arg u "$RPC_USER" --arg p "$RPC_PASSWORD" \
      '{method: "get_session_key", params: {username: $u, password: $p}, id: 1}')"
  res="$(curl -fsS --max-time "$CURL_MAX_TIME" -X POST \
      -H 'Content-Type: application/json' --data "$payload" "$RPC_URL")"
  [ -n "$res" ] || die "Keine Antwort vom Umfragen-Service — Benutzer/Passwort prüfen ($RPC_URL)."
  rtype="$(printf '%s' "$res" | jq -r '.result | type' 2>/dev/null || true)"
  if [ "$rtype" != "string" ]; then
    die "Anmeldung fehlgeschlagen: $(printf '%s' "$res" | jq -r '.result.status // .error // "unerwartete Antwort"')"
  fi
  printf '%s' "$res" | jq -r '.result'
}

# result_array <raw-envelope> — LS6 returns arrays, older LS JSON strings.
result_array() { jq '(if (.result | type) == "string" then (.result | fromjson) else .result end)'; }

# expect_ok <raw-envelope> — fail loudly on LimeSurvey's
# result:{status:"…"} error convention (invalid sid, expired key, …).
expect_ok() {
  if printf '%s' "$1" | jq -e '(.result | type) == "object" and has("status")' >/dev/null 2>&1; then
    die "Umfragen-RPC: $(printf '%s' "$1" | jq -r '.result.status // "unbekannter Fehler"')"
  fi
}

require_sid() {
  [[ "${1:-}" =~ ^[0-9]+$ ]] || die "Numerische Survey-ID erwartet (Beispiel: summary 100000)."
  printf '%s' "$1"
}

cmd_check() {
  require_deps
  local envelope count
  envelope="$(rpc_key list_surveys)"
  expect_ok "$envelope"
  count="$(printf '%s' "$envelope" | result_array | jq 'length' 2>/dev/null || true)"
  case "$count" in
    '' | *[!0-9]*) die "Umfragen-RPC: ungültige Antwort (keine Umfrage-Liste)." ;;
  esac
  echo "OK: am Umfragen-Service angemeldet ($count Umfrage(n) sichtbar)."
}

cmd_session() {
  require_deps
  local key
  key="$(get_key)"
  echo "OK: Session-Key erhalten (Länge ${#key})."
}

cmd_list_surveys() {
  require_deps
  local envelope
  envelope="$(rpc_key list_surveys)"
  expect_ok "$envelope"
  printf '%s' "$envelope" | result_array | jq 'map({id: .sid, title: .surveyls_title, aktiv: .active, startdatum: .startdate, ablauftag: .expires})'
}

cmd_summary() {
  require_deps
  local sid envelope
  sid="$(require_sid "${1:-}")"
  envelope="$(rpc_key get_summary "[${sid}]")"
  expect_ok "$envelope"
  printf '%s' "$envelope" | jq '.result'
}

cmd_questions() {
  require_deps
  local sid envelope
  sid="$(require_sid "${1:-}")"
  envelope="$(rpc_key list_questions "[${sid}]")"
  expect_ok "$envelope"
  printf '%s' "$envelope" | jq '.result'
}

cmd_release() {
  require_deps
  local key
  key="$(get_key)"
  jq -nc --arg k "$key" --arg u "$RPC_USER" '{method: "release_session_key", params: [$k, $u], id: 99}' |
    curl -fsS --max-time "$CURL_MAX_TIME" -X POST -H 'Content-Type: application/json' --data @- "$RPC_URL"
}

cmd_call() {
  require_deps
  local method="${1:-}" params="${2:-[]}"
  [ -n "$method" ] || die "Methode erwartet (Beispiel: call list_surveys '[]')."
  printf '%s' "$params" | jq -e 'type == "array"' >/dev/null 2>&1 \
    || die "Params müssen ein JSON-Array sein (Beispiel: '[\"KEY\",100000]')."
  rpc "$method" "$params"
}

cmd_enable_rpc() {
  command -v docker >/dev/null 2>&1 || die "docker wird benötigt."
  docker exec "$CONTAINER" test -f "$CONFIG_PATH" 2>/dev/null \
    || die "config.php nicht gefunden: $CONTAINER:$CONFIG_PATH (Container läuft?)."
  if docker exec "$CONTAINER" grep -q "'RPCInterface'=>'json'" "$CONFIG_PATH"; then
    echo "RPCInterface ist bereits 'json' — nichts zu tun."
  else
    # Insert into the returned array's 'config'=>array(...) block — top-level
    # $config assignments are discarded by LimeSurvey's config.php layout.
    printf "/'config'=>array(/a\\\n    'RPCInterface'=>'json',\n" |
      docker exec -i "$CONTAINER" sed -i -f - "$CONFIG_PATH"
    docker exec "$CONTAINER" grep -q "'RPCInterface'=>'json'" "$CONFIG_PATH" \
      || die "Patch nicht angewendet — config.php manuell prüfen."
    echo "config.php gepatcht: 'RPCInterface'=>'json' im 'config'=>array-Block."
  fi
  if [ "${1:-}" = "--restart" ]; then
    docker restart "$CONTAINER" >/dev/null
    echo "Container $CONTAINER neu gestartet (Traefik kann ~20 s brauchen)."
  else
    echo "Hinweis: PHP liest config.php pro Request; --restart erzwingt einen Neustart."
  fi
  echo "Achtung: config.php geht bei Container-NEUANLAGE verloren — Skript erneut ausführen."
}

cmd="${1:-}"
case "$cmd" in
  check)        cmd_check ;;
  session)      cmd_session ;;
  list-surveys) cmd_list_surveys ;;
  summary)      shift; cmd_summary "$@" ;;
  questions)    shift; cmd_questions "$@" ;;
  release)      cmd_release ;;
  call)         shift; cmd_call "$@" ;;
  enable-rpc)   shift; cmd_enable_rpc "$@" ;;
  -h|--help|help|'') usage ;;
  *) die "Unbekannter Subcommand: $cmd (help zeigt alle)." ;;
esac
