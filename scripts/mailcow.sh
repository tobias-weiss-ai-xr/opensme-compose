#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# scripts/mailcow.sh — drive the mailcow-dockerized mail stack (submodule)
# ═══════════════════════════════════════════════════════════════════════════
# mailcow-dockerized is vendored as a git submodule at
# mail/mailcow-dockerized (pinned to an upstream release tag, GPL-3.0 in
# its own tree — we never copy code into this Apache-2.0 repository).
#
# This script is the ONLY supported entrypoint for the mail stack. It:
#   1. renders mail/mailcow.conf from this repo's .env (secrets included),
#      configured for the openSME topology: TLS is terminated by the
#      openSME Traefik, so mailcow's own HTTP/ACME is disabled and its
#      web UI is reachable only through the overlay network
#   2. boots the mailcow compose project (docker compose, project
#      mailcowdockerized)
#   3. attaches opensme-traefik to the mailcow network and injects the
#      dynamic router (marker-managed block in traefik/dynamic.yml)
#   4. `down` removes the router and detaches — the openSME core is
#      untouched
#
# Usage:
#   scripts/mailcow.sh up [hostname]    # render conf + boot + wire router
#   scripts/mailcow.sh down             # unwire router + stop mailcow
#   scripts/mailcow.sh status           # quick health overview
#   scripts/mailcow.sh logs [service]   # follow logs
#
# Env:
#   MAILCOW_HOSTNAME   FQDN of the web UI   (default: mail.${OPENSME_DOMAIN:-opensme.graphwiz.ai})
#   OPENSME_DOMAIN     mail domain for the demo (default: opensme.graphwiz.ai)
#   MAILCOW_SKIP_CLAMD default y | SKIP_FTS default y — keep the demo lean
#   MAILCOW_{SMTP,SMTPS,SUBMISSION,IMAP,IMAPS,POP,POPS}_PORT
#                      host-side mail-port binds (defaults 25/465/587/143/993/
#                      110/995 — remap when the host already runs a mail server)
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUBMODULE_DIR="${REPO_DIR}/mail/mailcow-dockerized"
CONF="${REPO_DIR}/mail/mailcow.conf"
DYN="${REPO_DIR}/traefik/dynamic.yml"
TRAEFIK_CONTAINER="opensme-traefik"
PROJECT="mailcowdockerized"
MARK_BEGIN="# ── BEGIN MAILCOW ROUTER (managed by scripts/mailcow.sh) ──"
MARK_END="# ── END MAILCOW ROUTER ──"
MAILCOW_HTTP_PORT="$(grep -E '^HTTP_PORT=' "${CONF}" 2>/dev/null | cut -d= -f2 || true)"
MAILCOW_HTTP_PORT="${MAILCOW_HTTP_PORT:-28080}"
# the router target must match the CONTAINER port — mailcow renders
# HTTP_PORT as both the host and the container port (read at inject time,
# after the conf is rendered)

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
info() { echo "${GREEN}→${NC} $*"; }
warn() { echo "${YELLOW}⚠${NC} $*"; }
die()  { echo "${RED}✗${NC} $*" >&2; exit 1; }

MAILCOW_HOSTNAME="${MAILCOW_HOSTNAME:-mail.${OPENSME_DOMAIN:-opensme.graphwiz.ai}}"
MAIL_DOMAIN="${OPENSME_DOMAIN:-opensme.graphwiz.ai}"

# ── strict backend XOR ────────────────────────────────────────────
# Only one mail stack at a time: mailcow and Stalwart(+SOGo) would both
# claim mail.* on the edge and the SMTP/IMAP host ports. The switch is
# `MAIL_BACKEND=stalwart|mailcow` in .env (default: stalwart); mailcow.sh
# refuses to boot unless the switch says mailcow and Stalwart/SOGo are
# not running.
backend_switch_ok() {
  local b="${MAIL_BACKEND:-}"
  if [[ -z "$b" && -f "${REPO_DIR}/.env" ]]; then
    b="$(grep -E '^MAIL_BACKEND=' "${REPO_DIR}/.env" 2>/dev/null | head -1 | cut -d= -f2)"
  fi
  if [[ "$b" != "mailcow" ]]; then
    die "mail backend switch: MAIL_BACKEND=${b:-<unset>} — mailcow requires 'MAIL_BACKEND=mailcow' in .env (Stalwart is the default; the backends are strictly exclusive). Set the switch and retry."
  fi
  local running
  running="$(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^(opensme-stalwart|opensme-sogo)(-|$)' || true)"
  if [[ -n "$running" ]]; then
    die "Stalwart/SOGo containers are still running: $(echo "$running" | tr '\n' ' ') — stop them first (mail backends are exclusive, they would fight over mail.* and the SMTP/IMAP ports)."
  fi
}

require_submodule() {
  [[ -f "${SUBMODULE_DIR}/docker-compose.yml" ]] || die \
    "submodule missing — run: git submodule update --init mail/mailcow-dockerized"
}

rand_key() {
  # head closes the pipe early → tr gets SIGPIPE; shield from pipefail
  ( set +o pipefail; LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 )
}

# ── render mail/mailcow.conf from the repo .env ─────────────────────────
render_conf() {
  [[ -f "${CONF}" ]] && { info "mailcow.conf already rendered — keeping (delete to re-render)"; return 0; }
  # reuse secrets from the repo .env when present, else generate fresh
  local dbpass redispass apikey
  dbpass="$(grep -E '^MAILCOW_DBPASS=' "${REPO_DIR}/.env" 2>/dev/null | cut -d= -f2- || true)"
  redispass="$(grep -E '^MAILCOW_REDISPASS=' "${REPO_DIR}/.env" 2>/dev/null | cut -d= -f2- || true)"
  apikey="$(grep -E '^MAILCOW_API_KEY=' "${REPO_DIR}/.env" 2>/dev/null | cut -d= -f2- || true)"
  [[ -n "${dbpass}" ]] || dbpass="$(rand_key)"
  [[ -n "${redispass}" ]] || redispass="$(rand_key)"
  [[ -n "${apikey}" ]] || apikey="$(rand_key)"

  cat > "${CONF}" <<EOF
# mail/mailcow.conf — rendered by scripts/mailcow.sh from .env
# This file carries secrets: it is gitignored and must never be committed.
MAILCOW_HOSTNAME=${MAILCOW_HOSTNAME}
MAILCOW_TZ=${TZ:-Europe/Berlin}

# TLS is terminated by the openSME Traefik — mailcow's web binding stays
# on host loopback with high ports (compose expands an empty bind to
# 0.0.0.0, which would collide with Traefik on :80/:443). The web UI is
# reachable ONLY via Traefik; no ACME inside mailcow.
HTTP_PORT=${MAILCOW_HTTP_PORT:-28080}
HTTP_BIND=127.0.0.1
HTTP_REDIRECT=n
HTTPS_PORT=${MAILCOW_HTTPS_PORT:-28443}
HTTPS_BIND=127.0.0.1
SKIP_LETS_ENCRYPT=y
SKIP_IP_CHECK=y
SKIP_HTTP_VERIFICATION=y
SKIP_UNBOUND_HEALTHCHECK=y

# keep the demo lean (re-enable for production-grade deployments)
SKIP_CLAMD=${MAILCOW_SKIP_CLAMD:-y}
SKIP_FTS=${MAILCOW_SKIP_FTS:-y}
SKIP_SOGO=n

# mail ports stay on the host — that is the mail server's contract.
# Remap via MAILCOW_*_PORT when the host already runs another mail server
# (e.g. coexistence with a legacy Stalwart holding 25/143/587).
SMTP_PORT=${MAILCOW_SMTP_PORT:-25}
SMTPS_PORT=${MAILCOW_SMTPS_PORT:-465}
SUBMISSION_PORT=${MAILCOW_SUBMISSION_PORT:-587}
IMAP_PORT=${MAILCOW_IMAP_PORT:-143}
IMAPS_PORT=${MAILCOW_IMAPS_PORT:-993}
POP_PORT=${MAILCOW_POP_PORT:-110}
POPS_PORT=${MAILCOW_POPS_PORT:-995}

DBNAME=mailcow
DBUSER=mailcow
DBPASS=${dbpass}
DBROOT=$(rand_key)
REDISPASS=${redispass}
REDIS_PORT=127.0.0.1:7654

# dual-stack off by default: hosts with IPv6 disabled in the kernel refuse
# to create the br-mailcow bridge otherwise (enable on dual-stack hosts)
ENABLE_IPV6=${MAILCOW_ENABLE_IPV6:-false}

# REST API — reachable from localhost and the openSME network only.
# The e2e journey uses it from inside the nginx container; the web UI
# path is the operator route and does not expose the API.
API_KEY=${apikey}
API_ALLOW_FROM=127.0.0.1

ACL_ANYONE=disallow
WEBAUTHN_ONLY_TRUSTED_VENDORS=n
COMPOSE_PROJECT_NAME=${PROJECT}
EOF
  chmod 600 "${CONF}"
  info "rendered mail/mailcow.conf (hostname=${MAILCOW_HOSTNAME})"
}

# compose v2 ships as a docker CLI plugin (`docker compose`) or a standalone
# binary (`docker-compose`, e.g. Debian's docker-compose package) — accept both
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  COMPOSE=(docker-compose)
fi

compose() {
  (cd "${SUBMODULE_DIR}" && "${COMPOSE[@]}" --env-file "${CONF}" "$@")
}

ensure_certs() {
  # With SKIP_LETS_ENCRYPT=y the acme container does nothing, but nginx,
  # dovecot and postfix all load /etc/ssl/mail/cert.pem (a bind mount of
  # data/assets/ssl inside the submodule tree — gitignored). Seed it with
  # a self-signed cert for the hostname; Traefik presents its own TLS to
  # browsers anyway, this only satisfies the internal listeners.
  local ssl_dir="${SUBMODULE_DIR}/data/assets/ssl"
  mkdir -p "${ssl_dir}"
  if [[ ! -s "${ssl_dir}/cert.pem" || ! -s "${ssl_dir}/key.pem" ]]; then
    if openssl req -new -x509 -days 3650 -nodes \
      -subj "/CN=${MAILCOW_HOSTNAME}" \
      -addext "subjectAltName=DNS:${MAILCOW_HOSTNAME}" \
      -keyout "${ssl_dir}/key.pem" -out "${ssl_dir}/cert.pem" 2>/dev/null; then
      info "self-signed mail certificate created (${MAILCOW_HOSTNAME})"
    else
      die "openssl unavailable — cannot seed mail certificates"
    fi
  fi
  if [[ ! -s "${ssl_dir}/dhparams.pem" ]]; then
    if openssl dhparam -out "${ssl_dir}/dhparams.pem" 2048 2>/dev/null; then
      info "DH parameters created"
    else
      warn "dhparam generation failed — dovecot may refuse to start"
    fi
  fi
}

kernel_without_ipv6() { [[ ! -d /proc/sys/net/ipv6/conf/all ]]; }

patch_ipv6_listen() {
  # Hosts booted with ipv6.disable=1 never register IPv6 in the kernel (a
  # later modprobe ipv6 does NOT help) — mailcow's dual-stack listeners
  # die with EAFNOSUPPORT. Patch the OFFICIAL user-editable mailcow config
  # points (data/conf — persisted across mailcow updates by design) to
  # plain IPv4; the bridge network is already created enable_ipv6=false.
  kernel_without_ipv6 || return 0
  local pools="${SUBMODULE_DIR}/data/conf/phpfpm/php-fpm.d/pools.conf"
  if [[ -f "${pools}" ]] && grep -q 'listen = \[::\]:' "${pools}"; then
    sed -i 's/listen = \[::\]:/listen = 0.0.0.0:/' "${pools}"
  fi
  local dove="${SUBMODULE_DIR}/data/conf/dovecot/dovecot.conf"
  if [[ -f "${dove}" ]] && grep -qE '^listen = \*,' "${dove}"; then
    sed -i 's/^listen = .*/listen = */' "${dove}"
  fi
  local ng="${SUBMODULE_DIR}/data/conf/nginx/templates/nginx.conf.j2"
  if [[ -f "${ng}" ]] && grep -q '\[::\]:' "${ng}"; then
    sed -i '/listen \[::\]:/d' "${ng}"
  fi
  warn "host kernel runs without IPv6 — mailcow listeners patched to IPv4"
}

traefik_ip_on_mailcow_net() {
  docker inspect "${TRAEFIK_CONTAINER}" --format \
    '{{range $net, $cfg := .NetworkSettings.Networks}}{{$net}}={{$cfg.IPAddress}}{{"\n"}}{{end}}' \
    2>/dev/null | awk -F= -v p="${PROJECT}_mailcow-network" '$1==p{print $2}'
}

ensure_router() {
  # 1) attach traefik to the mailcow network (idempotent)
  if ! docker network inspect "${PROJECT}_mailcow-network" \
      --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null | grep -q "${TRAEFIK_CONTAINER}"; then
    if docker network connect "${PROJECT}_mailcow-network" "${TRAEFIK_CONTAINER}" 2>/dev/null; then
      info "traefik attached to the mailcow network"
    fi
  fi
  # 2) inject the dynamic router into traefik/dynamic.yml (marker block).
  #    The REST API is deliberately NOT allowlisted for traefik's IP —
  #    the public edge must never serve the API (the journey proves it).
  #    Journeys call the API from inside the nginx container (localhost).
  if ! grep -qF "${MARK_BEGIN}" "${DYN}"; then
    cat >> "${DYN}" <<EOF
${MARK_BEGIN}
  routers:
    mailcow-ui:
      rule: "Host(\`${MAILCOW_HOSTNAME}\`)"
      entrypoints: [websecure]
      service: mailcow-web
      tls: {}
  services:
    mailcow-web:
      loadBalancer:
        servers:
          - url: "http://nginx-mailcow:${MAILCOW_HTTP_PORT:-28080}"
${MARK_END}
EOF
    info "router injected into traefik/dynamic.yml (Host ${MAILCOW_HOSTNAME})"
  fi
}

remove_router() {
  python3 - "${DYN}" "${MARK_BEGIN}" "${MARK_END}" <<'PYEOF'
import sys
path, b, e = sys.argv[1], sys.argv[2], sys.argv[3]
src = open(path).read()
if b in src:
    head, rest = src.split(b, 1)
    tail = rest.split(e, 1)[1] if e in rest else ""
    open(path, "w").write(head.rstrip() + "\n" + tail.lstrip("\n"))
    print("→ mailcow router removed from dynamic.yml")
PYEOF
  docker network disconnect "${PROJECT}_mailcow-network" "${TRAEFIK_CONTAINER}" 2>/dev/null || true
}

restore_stalwart_hint() {
  echo "  To return to the default backend: set MAIL_BACKEND=stalwart in .env,"
  echo "  then start the stack with the stalwart+sogo overlays:"
  echo "    export COMPOSE_FILE=\"docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:mail/stalwart.yml:mail/sogo.yml\""
  echo "    docker compose up -d"
}

cmd_up() {
  backend_switch_ok
  require_submodule
  if [[ ! -f "${CONF}" ]]; then
    # refuse to render fresh credentials over an EXISTING database volume:
    # mariadb keeps the password from its FIRST initialization, a new conf
    # would silently split-brain the stack (postfix/php-fpm can no longer
    # authenticate). Purging is an explicit operator decision.
    if docker volume inspect "${PROJECT}_mysql-vol-1" >/dev/null 2>&1; then
      die "mail/mailcow.conf missing but mailcow database volume exists —
credentials unknown. Run 'scripts/mailcow.sh down --purge' to reset (destroys mail data) or restore the conf."
    fi
    render_conf
  fi
  # keep HOSTNAME consistent with the caller (router rule uses it)
  sed -i "s|^MAILCOW_HOSTNAME=.*|MAILCOW_HOSTNAME=${MAILCOW_HOSTNAME}|" "${CONF}"
  ensure_certs
  patch_ipv6_listen
  info "booting mailcow (this pulls ~1.5 GB on first run)…"
  local rc=0
  compose up -d || rc=$?
  # compose sometimes leaves key services in "created" (sometimes even
  # network-less) on a rocky first boot — stop+recreate settles it
  for svc in nginx-mailcow postfix-mailcow dovecot-mailcow; do
    state="$(docker inspect "mailcowdockerized-${svc}-1" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
    if [[ "${state}" != "running" ]]; then
      compose rm -sf "${svc}" >/dev/null 2>&1 || true
      compose up -d "${svc}" >/dev/null 2>&1 || true
    fi
  done
  # wiring must happen even when some containers failed to start
  ensure_router
  if [[ ${rc} -ne 0 ]]; then
    die "some mailcow containers failed to start — inspect with:
  docker compose --project-directory ${SUBMODULE_DIR} --env-file ${CONF} ps -a"
  fi
  echo ""
  info "mailcow UI:  https://${MAILCOW_HOSTNAME}/  (via openSME Traefik)"
  info "mail domain: ${MAIL_DOMAIN} — create mailboxes via the UI or REST API"
}

cmd_down() {
  local purge=0
  [[ "${1:-}" == "--purge" ]] && purge=1
  remove_router
  [[ -f "${CONF}" ]] || { info "no mailcow.conf — nothing to stop"; exit 0; }
  if [[ ${purge} -eq 1 ]]; then
    compose down -v
    rm -f "${CONF}"
    rm -rf "${SUBMODULE_DIR}/data/assets/ssl"
    info "mailcow stopped and purged (volumes, conf, certs); openSME core untouched"
  else
    compose down
    info "mailcow stopped (volumes kept); openSME core untouched"
  fi
  restore_stalwart_hint
}

cmd_status() {
  compose ps --format "table {{.Service}}\t{{.Status}}" 2>/dev/null || die "not running"
  echo ""
  local tip; tip="$(traefik_ip_on_mailcow_net)"
  echo "   traefik on mailcow-net: ${tip:-not attached}"
  grep -qF "${MARK_BEGIN}" "${DYN}" && echo "   router: injected" || echo "   router: absent"
}

cmd_logs() { compose logs -f "${1:-}"; }

case "${1:-}" in
  up) shift || true; [[ -n "${1:-}" ]] && MAILCOW_HOSTNAME="$1"; cmd_up ;;
  down) shift || true; cmd_down "${1:-}" ;;
  status) cmd_status ;;
  logs) shift || true; cmd_logs "${1:-}" ;;
  *) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
