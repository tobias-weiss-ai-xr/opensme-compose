#!/usr/bin/env python3
"""
tests/00-static/check_mutations.py — mutation tests for the static suite.

The coverage guard (check_suite_coverage.py) proves every check is *wired*;
this meta-check proves every critical check has *teeth*: for each declared
mutation it breaks a real repo file in an isolated shadow copy, runs the
targeted check against it, and requires the check to FAIL.

A mutation that leaves the target check green means one of:
  - the invariant it encodes is no longer enforced (weakened/removed logic);
  - the file that check reads changed shape (fragment renamed, config moved)
    so the check now scans a dead path.
Either way the suite is lying about protecting that invariant, and this
check goes red so the exact pair (file, invariant) gets re-anchored.

Safety: every mutation runs against a THROWAWAY shadow tree under
TMPDIR/opensme-mut-* — real files are never touched. The shadow is a full
copy: repo root content is read verbatim via symlink farms on
symlink-capable systems; on systems without symlinks we copy the few files
each check actually reads (the MUTATIONS table declares them).

Usage:  python3 tests/00-static/check_mutations.py
Exit:   0 all mutations detected; 1 a mutation went undetected.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

ROOT = Path(__file__).resolve().parents[2]

# ─────────────────────────────────────────────────────────────────────────────
# Mutation table.
#
# Each entry: a single textual mutation applied to one real repo file, plus
# the check that must FAIL when the mutation is present. Mutations must be
# minimal — flip one character, keyword, or line — exactly what a sloppy
# future edit would accidentally do.
#
# Fields:
#   name      short label
#   file      repo-relative file to mutate (must exist)
#   old/new   exact text replacement (old must appear exactly once)
#   check     repo-relative check script that must exit non-zero
#   why       what invariant this mutation guards (failure message)
# ─────────────────────────────────────────────────────────────────────────────
MUTATIONS = [
    {
        "name": "docs-index-sso-row-removed",
        "file": "docs/README.md",
        "old": "[SSO.md](SSO.md)",
        "new": "[SSO.md](SSO_MUT.md)",
        "check": "tests/00-static/check_docs_index.py",
        "why": "docs-as-contract — dropping an index row silently orphans an operator doc",
    },
    {
        "name": "cloud-bridge-mkdir-route-removed",
        "file": "portal/src/main.rs",
        "old": '.route("/api/cloud/files/mkdir", post(handle_cloud_mkdir_post))',
        "new": '.route("/api/cloud/files/mkdir_MUT", post(handle_cloud_mkdir_post))',
        "check": "tests/00-static/check_cloud_bridge.py",
        "why": "D1-D5 — dropping the session-scoped mkdir route would leave the "
              "browser's built-in New Folder action with a 404",
    },
    {
        "name": "webdav-archive-paperless-mount-writable",
        "file": "services/paperless.yml",
        "old": "paperless-export:/data:ro",
        "new": "paperless-export:/data",
        "check": "tests/00-static/check_webdav_archive.py",
        "why": "B2 — a writable source mount would let the one-way archive "
              "overwrite Paperless' own export directory",
    },
    {
        "name": "portal-cors-permissive-layer-restored",
        "file": "portal/src/main.rs",
        "old": "// NO permissive CORS layer",
        "new": "// MUT permissive CORS layer",
        "check": "tests/00-static/check_portal_cors.py",
        "why": "E2 — restoring permissive CORS would let any origin read /api/* "
              "again; the same-origin proxy is the only browser entry point",
    },
    {
        "name": "demo-env-vc-public-ip",
        "file": "docker-compose.yml",
        "old": "${VC_PUBLIC_IP:?set VC_PUBLIC_IP}",
        "new": "${VC_PUBLIC_IP_MUTATED:?set VC_PUBLIC_IP_MUTATED}",
        "check": "tests/00-static/check_demo_env.py",
        "why": "E2E boot — a fail-closed ${VAR:?} guard the demo generator does "
               "not fill aborts `docker compose up` before any container starts",
    },
    {
        "name": "interop-profiles-requirepass",
        "file": "profiles/medium.yml",
        "old": "- --requirepass\n      - ${REDIS_PASSWORD:?REDIS_PASSWORD must be set in .env}",
        "new": "- --noauth",
        "check": "tests/00-static/check_interop.py",
        "why": "§11 — profile redis-command overrides must keep requirepass",
    },
    {
        "name": "interop-notes-callback",
        "file": "scripts/zitadel-client.sh",
        "old": 'base_paths=("/api/v1.0/callback/")',
        "new": 'base_paths=("/oidc/callback/")',
        "check": "tests/00-static/check_interop.py",
        "why": "§12 — notes Zitadel callback must be the lasuite API path",
    },
    {
        "name": "interop-notes-allowed-hosts",
        "file": "services/notes.yml",
        "old": 'DJANGO_ALLOWED_HOSTS: "notes',
        "new": 'ALLOWED_HOSTS: "notes',
        "check": "tests/00-static/check_interop.py",
        "why": "§9b — impress reads DJANGO_* (django-configurations prefix)",
    },
    {
        "name": "interop-synapse-baseurl",
        "file": "services/synapse-setup.sh",
        "old": 'PUBLIC_BASEURL="https://${SERVER_NAME}"',
        "new": 'PUBLIC_BASEURL="https://matrix.${DOMAIN}"',
        "check": "tests/00-static/check_interop.py",
        "why": "§7 — synapse public_baseurl must derive from SYNAPSE_SERVER_NAME",
    },
    {
        "name": "slo-portal-csp-xreq",
        "file": "portal/web/src/middleware.ts",
        "old": "request: {",
        "new": "requestX: {",
        "check": "tests/00-static/check_oidc.py",
        "why": "R21 — Next's parseRequestHeaders only reads req.headers; without "
              "the CSP in the REQUEST headers the nonce never tags the inline "
              "bootstrap scripts and dynamic pages stay blank",
    },
    {
        "name": "slo-loggedout-dynamic",
        "file": "portal/web/src/app/logged-out/page.tsx",
        "old": "force-dynamic",
        "new": "force-dynamic_STRIPPED",
        "check": "tests/00-static/check_oidc.py",
        "why": "R21 — a static prerender has no per-request nonce at build time, "
              "so the nonce-CSP blocks every inline script and the R20 gallery "
              "landing would render blank",
    },
    {
        "name": "intercom-bridge-slot-share",
        "file": "portal/web/src/lib/bridge.ts",
        "old": '"share.open": ShareOpenProvider;',
        "new": '"share_open_MUT": ShareOpenProvider;',
        "check": "tests/00-static/check_bridge.py",
        "why": "portal-intercom — a renamed slot detaches every consumer; the "
              "guard must flag the missing documented slot in the contract",
    },
    {
        "name": "intercom-bridge-slot-cloud",
        "file": "portal/web/src/lib/bridge.ts",
        "old": '"cloud.pick": CloudPickProvider;',
        "new": '"cloud_pick_MUT": CloudPickProvider;',
        "check": "tests/00-static/check_bridge.py",
        "why": "portal-intercom — the shared cloud-pick slot is the attachment "
              "contract; renaming it silently orphans the picker consumers",
    },
    {
        "name": "intercom-bridge-shell-tab",
        "file": "portal/web/src/components/PortalShell.tsx",
        "old": 'setTab("notizen");',
        "new": 'setTab("dienste");',
        "check": "tests/00-static/check_bridge.py",
        "why": "portal-intercom — the share.open handler must land the visitor in "
              "the notes tab; switching elsewhere hides the prefilled composer",
    },
    {
        "name": "intercom-bridge-feed-share",
        "file": "portal/web/src/components/FeedColumn.tsx",
        "old": 'useSlot("share.open")',
        "new": 'useSlot("share_open_MUT")',
        "check": "tests/00-static/check_bridge.py",
        "why": "portal-intercom — a detached feed share source breaks the share "
              "path; the guard must flag the missing feed affordance",
    },
    {
        "name": "tasks-firewall-removed",
        "file": "portal/src/main.rs",
        "old": "session::require_session,",
        "new": "session::require_session_MUT,",
        "check": "tests/00-static/check_tasks.py",
        "why": "portal-tasks — the /api/tasks routes must sit behind the session "
              "firewall; dropping require_session exposes task CRUD anonymously",
    },
    {
        "name": "tasks-cap-evicts-open",
        "file": "portal/src/tasks.rs",
        "old": "tasks.iter().position(|t| t.status == TaskStatus::Done)",
        "new": "tasks.iter().position(|t| t.status == TaskStatus::Open)",
        "check": "tests/00-static/check_tasks.py",
        "why": "portal-tasks — the cap must evict the oldest DONE task; evicting "
              "OPEN tasks would silently delete unfinished work",
    },
    {
        "name": "tasks-author-client-supplied",
        "file": "portal/src/tasks.rs",
        "old": "pub struct TaskCreate {\n    pub title: String,",
        "new": "pub struct TaskCreate {\n    pub author: String,\n    pub title: String,",
        "check": "tests/00-static/check_tasks.py",
        "why": "portal-tasks — the author must be stamped from the session server-"
              "side; a client-supplied author field enables forged attribution",
    },
    {
        "name": "tasks-status-guard-removed",
        "file": "portal/src/main.rs",
        "old": "if patch.status.is_some() && !tasks::may_complete(&session.email, &session.name, &task) {",
        "new": "if patch.status.is_some() {",
        "check": "tests/00-static/check_tasks.py",
        "why": "portal-tasks — status changes are restricted to assignee/author; "
              "dropping the may_complete guard lets anyone complete any task",
    },
    {
        "name": "tasks-bridge-slot-registry",
        "file": "portal/web/src/lib/bridge.ts",
        "old": '"task.create": TaskCreateProvider;',
        "new": '"task_create_MUT": TaskCreateProvider;',
        "check": "tests/00-static/check_bridge.py",
        "why": "portal-tasks — a renamed task.create slot detaches the drei "
              "sources; the guard must flag the missing documented slot",
    },
    {
        "name": "tasks-bridge-feed-consumer",
        "file": "portal/web/src/components/FeedColumn.tsx",
        "old": 'const taskProvider = useSlot("task.create");',
        "new": 'const taskProvider = useSlot("task_create_MUT");',
        "check": "tests/00-static/check_bridge.py",
        "why": "portal-tasks — a detached feed task source breaks 'Als Aufgabe "
              "anlegen' from feed cards; the guard must flag it",
    },
    {
        "name": "tasks-bridge-note-consumer",
        "file": "portal/web/src/components/IntercomPanel.tsx",
        "old": 'const taskProvider = useSlot("task.create");',
        "new": 'const taskProvider = useSlot("task_create_MUT");',
        "check": "tests/00-static/check_bridge.py",
        "why": "portal-tasks — a detached notes task source breaks 'Als Aufgabe "
              "anlegen' from team notes; the guard must flag it",
    },
    {
        "name": "tasks-bridge-cloud-consumer",
        "file": "portal/web/src/components/CloudFilePicker.tsx",
        "old": 'const taskProvider = useSlot("task.create");',
        "new": 'const taskProvider = useSlot("task_create_MUT");',
        "check": "tests/00-static/check_bridge.py",
        "why": "portal-tasks — a detached cloud task source breaks 'Als Aufgabe "
              "anlegen' from cloud file rows; the guard must flag it",
    },
    {
        "name": "slo-portal-csp-idp",
        "file": "portal/web/src/middleware.ts",
        "old": "process.env.IDP_URL",
        "new": "process.env.IDP_URL_STRIPPED",
        "check": "tests/00-static/check_oidc.py",
        "why": "R21 — the logout 303 hop to the IdP end-session is blocked by "
              "form-action 'self' unless the middleware allow-lists the "
              "IDP_URL origin (Chrome enforces form-action on redirects of "
              "form submissions)",
    },
    {
        "name": "oidc-client-ref",
        "file": "services/notes.yml",
        "old": "NOTES_OIDC_CLIENT_ID",
        "new": "NOTES_OIDC_CLIENT_ID_DROPPED",
        "check": "tests/00-static/check_oidc.py",
        "why": "R1 — every referenced OIDC client var needs a branch",
    },
    {
        "name": "sso-oc-hostgateway-pin",
        "file": "opencloud/opencloud.yml",
        "old": ":host-gateway",
        "new": ":203.0.113.10",
        "check": "tests/00-static/check_oidc.py",
        "why": "R4 — the ocis IdP backchannel must stay pinned to "
              "host-gateway (a hardcoded container IP dead-ends discovery)",
    },
    {
        "name": "sso-oc-autoprovision",
        "file": "opencloud/opencloud.yml",
        "old": 'PROXY_AUTOPROVISION_ACCOUNTS: "true"',
        "new": 'PROXY_AUTOPROVISION_ACCOUNTS: "false"',
        "check": "tests/00-static/check_oidc.py",
        "why": "R5 — external (Zitadel) users must stay autoprovisioned "
              "with the default role driver",
    },
    {
        "name": "sso-element-homeserver",
        "file": "services/element.yml",
        "old": 'ELEMENT_DEFAULT_SERVER_URL: "https://matrix.${OPENSME_DOMAIN:-opensme.graphwiz.ai}"',
        "new": 'ELEMENT_DEFAULT_SERVER_URL: "https://matrix.${OPENSME_DOMAIN:-opensme.graphwiz.ai}"\n      ELEMENT_HOMESERVER_NAME: matrix.opensme.graphwiz.ai',
        "check": "tests/00-static/check_oidc.py",
        "why": "R6 — element server_name must derive from "
              "ELEMENT_DEFAULT_SERVER_URL only (no second source)",
    },
    {
        "name": "sso-zitadel-login-service",
        "file": "idm/zitadel.yml",
        "old": "  zitadel-login:\n",
        "new": "  zitadel-login-disabled:\n",
        "check": "tests/00-static/check_oidc.py",
        "why": "R7 — the v4 login UI service must stay declared in "
              "idm/zitadel.yml",
    },
    {
        "name": "sso-oidc-app-tokentype",
        "file": "scripts/zitadel-client.sh",
        "old": '     accessTokenType: $tokentype,\n     responseTypes: ["OIDC_RESPONSE_TYPE_CODE"],',
        "new": '     accessTokenType: "OIDC_TOKEN_TYPE_OPAQUE",\n     responseTypes: ["OIDC_RESPONSE_TYPE_CODE"],',
        "check": "tests/00-static/check_oidc.py",
        "why": "R8 — the OIDC app must be registered with JWT access "
              "tokens; Zitadel's OPAQUE default is rejected by every "
              "JWKS-validating backend (ocis: 'invalid number of segments')",
    },
    {
        "name": "suite-c3-event-store-regression",
        "file": "scripts/suite-monitor.sh",
        "old": 'JWT_APPS="${MONITOR_JWT_APPS:-${OC_OIDC_CLIENT_ID:-}}"',
        "new": 'JWT_APPS="${MONITOR_JWT_APPS:-${OC_OIDC_CLIENT_ID:-}}"\n'
               '    events=$(curl -sk -X POST "$IDP/admin/v1/events/_search" -d "{}")',
        "check": "tests/00-static/check_monitoring.py",
        "why": "M2 — the C3 guard must read oidcConfig.accessTokenType from "
              "the LIVE app config (apps/_search). The former admin "
              "event-store query false-failed a healthy fleet for a day "
              "(openedusuite.org 2026-11-19..20): this Zitadel version "
              "silently ignores the query's aggregate filters, so login "
              "churn scrolled the oidc_config event out of the window. "
              "Reinstating the events/_search query must fail Layer 0.",
    },
    {
        "name": "suite-c5-edge-latency",
        "file": "scripts/suite-monitor.sh",
        "old": 'slow=$(docker logs traefik --since "$since" 2>&1 \\\n    | grep -E \'"cloud-.*@file"\' \\',
        "new": 'slow=$(docker logs opensme-opencloud --since "$since" 2>&1 \\\n    | grep "access-log" \\\n    | grep -oE \'duration=[0-9.]+\' | head -5) \\',
        "check": "tests/00-static/check_monitoring.py",
        "why": "M9 — OpenCloud access-log durations report the AGE OF THE "
              "UPSTREAM TCP CONNECTION (Traefik keep-alive pool), not "
              "request latency (2026-11-18 phantom-alert incident: client "
              "0.03-0.35 s vs logged 7.4-53.6 s); the suite monitor must "
              "scan the Traefik edge log",
    },
    {
        "name": "sso-notes-dialer-pin",
        "file": "services/notes.yml",
        "old": "    extra_hosts:\n      - \"${ZITADEL_DOMAIN:-auth.opensme.graphwiz.ai}:host-gateway\"",
        "new": "    # extra_hosts pin removed (mutation)",
        "check": "tests/00-static/check_oidc.py",
        "why": "R10 — a server-side IdP dialer without the host-gateway pin "
              "dies when a network alias shadows the public FQDN with the "
              "zitadel container IP (connection refused on :443; notes SSO "
              "outage 2026-11-13)",
    },
    {
        "name": "sso-notes-jwks-path",
        "file": "services/notes.yml",
        "old": 'OIDC_OP_JWKS_ENDPOINT: "https://${ZITADEL_DOMAIN:-auth.opensme.graphwiz.ai}/oauth/v2/keys"',
        "new": 'OIDC_OP_JWKS_ENDPOINT: "https://${ZITADEL_DOMAIN:-auth.opensme.graphwiz.ai}/.well-known/openid-configuration/jwks"',
        "check": "tests/00-static/check_oidc.py",
        "why": "R11 — Zitadel does not implement the OpenID-Connect-"
              "conventional JWKS path (404 → token validation 500s "
              "mid-login); only /oauth/v2/keys is real",
    },
    {
        "name": "sso-zitadel-alias-shadow",
        "file": "idm/zitadel.yml",
        "old": "        aliases:\n          - auth.opensme.local",
        "new": "        aliases:\n          - ${ZITADEL_DOMAIN:-auth.opensme.graphwiz.ai}",
        "check": "tests/00-static/check_oidc.py",
        "why": "R12 — aliasing the PUBLIC IdP hostname makes docker's "
              "embedded DNS shadow it inside every container with the "
              "zitadel container IP (plain :8080 only) — connection "
              "refused on every server-side OIDC call (notes SSO outage "
              "2026-11-13)",
    },
    {
        "name": "webmail-sso-pin-broken",
        "file": "mail/stalwart.yml",
        "old": "    container_name: opensme-stalwart",
        "new": "    container_name: opensme-stalwart\n    extra_hosts:\n      - \"${ZITADEL_DOMAIN:-auth.opensme.graphwiz.ai}:203.0.113.10\"",
        "check": "tests/00-static/check_webmail_sso.py",
        "why": "W3 — a hardcoded test-IP IdP pin dead-ends the :8081 idpplain "
              "userinfo hop (and breaks CI's alias path); only the fleet "
              "overlay's host-gateway pin is allowed",
    },
    {
        "name": "webmail-sso-directory-dropped",
        "file": "mail/stalwart/config.toml.template",
        "old": 'directory = "oidc"',
        "new": 'directory = "internal"',
        "check": "tests/00-static/check_webmail_sso.py",
        "why": "W1 — stalwart's default directory must stay 'oidc' so "
              "XOAUTH2 webmail logins validate against Zitadel's userinfo "
              "endpoint (an internal directory refuses every SSO login)",
    },
    {
        "name": "webmail-sso-issuer-override-dropped",
        "file": "idm/zitadel.yml",
        "old": '      - "traefik.http.middlewares.zitadel-idp-host.headers.customrequestheaders.X-Forwarded-Proto=https"\n',
        "new": "",
        "check": "tests/00-static/check_webmail_sso.py",
        "why": "W5 — the idpplain hop must override X-Forwarded-Proto/Port or "
              "Zitadel derives the wrong issuer at userinfo (SMTP 535 "
              "'issuer does not match' on XOAUTH2 submission)",
    },
    {
        "name": "env-completeness",
        "file": ".env.example",
        "old": "REDIS_PASSWORD=CHANGEME_redis",
        "new": "REDIS_PASSWORD_DROPPED=",
        "check": "tests/00-static/check_env.py",
        "why": "env completeness — every referenced var must stay in .env.example",
    },
    {
        "name": "tiles-routed-exclusion",
        "file": "contracts/tiles.yml",
        "old": "    router: notesapp\n",
        "new": "    # DELETED notesapp router link (mutation)\n",
        "check": "tests/00-static/check_tiles.py",
        "why": "tiles catalog — the notes tile's notesapp router link must stay",
    },
    {
        "name": "coverage-runner-wiring",
        "file": "tests/run.py",
        "old": '"Test-suite coverage (no dead checks)", "tests/00-static/check_suite_coverage.py"',
        "new": '"Test-suite coverage (no dead checks)", "tests/00-static/check_suite_coverage_removed.py"',
        "check": "tests/00-static/check_suite_coverage.py",
        "why": "coverage guard — check_suite_coverage must stay wired",
    },
    {
        "name": "interop-idm-swap",
        "file": "contracts/interop.yml",
        "old": "idm_services: [zitadel, casdoor]",
        "new": "idm_services: [keycloak]",
        "check": "tests/00-static/check_interop.py",
        "why": "§1 — every declared IdM needs its idm/<name>.yml swap point",
    },
    {
        "name": "agent-one-shot",
        "file": "monitoring/dev-agent.yml",
        "old": '    restart: "no"',
        "new": '    restart: "always"',
        "check": "tests/00-static/check_agent.py",
        "why": "dev-maintenance-bot overlay — the acting agent must stay "
              "one-shot (restart: 'no'), not a long-running daemon",
    },
    {
        "name": "artifacts-ignore-backups",
        "file": ".gitignore",
        "old": "backups/",
        "new": "backups_dropped/",
        "check": "tests/00-static/check_artifacts.py",
        "why": "committed-artifact hygiene — the backups/ ignore rule must "
              "stay so `git add -A` cannot commit runtime dumps",
    },
    {
        "name": "backup-known-volumes",
        "file": "scripts/backup.sh",
        "old": "  camunda-data",
        "new": "  # camunda-data REMOVED (mutation)",
        "check": "tests/00-static/check_backup.py",
        "why": "backup drift guard — every stateful fragment volume must stay "
              "in backup.sh's KNOWN_VOLUMES",
    },
    {
        "name": "agent-state-in-backup",
        "file": "scripts/backup.sh",
        "old": "dev-maintenance-bot-state ",
        "new": "# VOLUME_REMOVED ",
        "check": "tests/00-static/check_agent.py",
        "why": "AH2 durability — every write-mode agent's state volume must "
              "remain in scripts/backup.sh",
    },
    {
        "name": "restore-applies-postgres",
        "file": "scripts/restore.sh",
        "old": 'gunzip -c "$ACTUAL_FILE"',
        "new": 'gunzip -c "$ACTUAL_FILE_MISSING"',
        "check": "tests/00-static/check_backup_restore.py",
        "why": "backup/restore round-trip — restore.sh must APPLY the "
              "postgres_ artifact, not merely mention it",
    },
    {
        "name": "bpmn-ai-boundary-event",
        "file": "bootstrap/bpmn/invoice-approval.bpmn",
        "old": '<boundaryEvent id="ai-categorize-error"',
        "new": '<!-- boundaryEvent id="ai-categorize-error" DISABLED -->',
        "check": "tests/00-static/check_bpmn_compensation.py",
        "why": "AF2: BPMN AI ServiceTasks must have boundaryEvent compensation handlers",
    },
    {
        "name": "outbound-secrets-regex",
        "file": "tests/06-security/llm_outbound.py",
        "old": 'r"(?:password|passwd',
        "new": 'r"(?:NEVER_MATCH',
        "check": "tests/00-static/check_outbound_patterns.py",
        "why": "AE1: the outbound-payload catalog must still detect secrets; "
              "weakening a pattern must fail check_outbound_patterns.py"
    },
    {
        "name": "boot-core-image-pinned",
        "file": "docker-compose.yml",
        "old": "image: ${TRAEFIK_IMAGE:-traefik:v3.7.13}",
        "new": "image: traefik:latest",
        "check": "tests/00-static/check_boot.py",
        "why": "boot contracts — core services must stay pinned (a bare "
              ":latest silently changes the boot/config model)",
    },
    {
        "name": "bootstrap-history-ttl",
        "file": "bootstrap/bpmn/invoice-approval.bpmn",
        "old": 'operaton:historyTimeToLive="P180D"',
        "new": 'operaton:historyTimeToLive=""',
        "check": "tests/00-static/check_bootstrap.py",
        "why": "seed BPMN — every Operaton process needs a history TTL "
              "(ENGINE-12018, deploy would be rejected)",
    },
    {
        "name": "bpmn-ai-compensation",
        "file": "bootstrap/bpmn/invoice-approval.bpmn",
        "old": 'attachedToRef="ai-categorize"',
        "new": 'attachedToRef="invoice-received"',
        "check": "tests/00-static/check_bpmn_compensation.py",
        "why": "seed BPMN — an AI ServiceTask without an attached boundary "
              "event loses its compensation path (Epic AF: AI failure must "
              "fall back to human review, not block the process)",
    },
    {
        "name": "mail-backend-default",
        "file": ".env.example",
        "old": "MAIL_BACKEND=stalwart",
        "new": "MAIL_BACKEND=mailcow",
        "check": "tests/00-static/check_mail_backend.py",
        "why": "mail backend XOR — .env.example must keep stalwart as the "
              "default switch",
    },
    {
        "name": "perf-digest-pin-doc",
        "file": ".env.example",
        "old": "TRAEFIK_IMAGE_DIGEST",
        "new": "TRAEFIK_IMAGE_PIN_DROPPED",
        "check": "tests/00-static/check_perf.py",
        "why": "perf budgets — .env.example must keep the digest-pin vars "
              "documented for pinned images",
    },
    {
        "name": "notes-broker-env-name",
        "file": "services/notes.yml",
        "old": "DJANGO_CELERY_BROKER_URL: redis://:${REDIS_PASSWORD:?REDIS_PASSWORD must be set in .env}@redis:6379/0",
        "new": "CELERY_BROKER_URL: redis://:${REDIS_PASSWORD:?REDIS_PASSWORD must be set in .env}@redis:6379/0",
        "check": "tests/00-static/check_notes_broker.py",
        "why": "notes celery broker — the DJANGO_ prefix is mandatory "
              "(django-configurations ignores the bare name); renaming it "
              "must fail check_notes_broker.py",
    },
    {
        "name": "env-quote-brand-printf",
        "file": "scripts/brand.sh",
        "old": "printf '%s=\"%s\"\\n' \"$key\" \"$val\"",
        "new": "printf '%s=%s\\n' \"$key\" \"$val\"",
        "check": "tests/00-static/check_env_quote.py",
        "why": "env-quote hardening — brand.sh must emit double-quoted env "
              "values; the unquoted PORTAL_TAGLINE emission made `source "
              ".env` assign PORTAL_TAGLINE=open and EXECUTE the word "
              "`Source` ('Source: command not found' aborted calendar-setup)",
    },
    {
        "name": "events-sse-compression-exclusion",
        "file": "docker-compose.yml",
        "old": "traefik.http.middlewares.compress.compress.excludedContentTypes=text/event-stream",
        "new": "traefik.http.middlewares.compress.compress.excludedContentTypes=application/json",
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — Traefik must not compress text/event-stream; "
               "buffering/compressing SSE hides frame boundaries from "
               "EventSource and stalls live intercom/task updates",
    },
    {
        "name": "events-task-delete-recipients-dropped",
        "file": "portal/src/main.rs",
        "old": "events::Visibility::Recipients(recipients_for_task(&task, &session.sub)),",
        "new": "events::Visibility::Recipients(vec![]),",
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — the production task.deleted publish must "
               "compute recipients server-side from the task; dropping that "
               "call leaves the publish with no recipient scope and the E4 "
               "pin must catch it",
    },
    {
        "name": "events-reader-sleep-stall",
        "file": "portal/src/events.rs",
        "old": '            if tx.send(Ok(": keep-alive\\n\\n".into())).await.is_err() {\n'
               '                return;\n'
               '            }\n',
        "new": '            if tx.send(Ok(": keep-alive\\n\\n".into())).await.is_err() {\n'
               '                return;\n'
               '            }\n'
               '            tokio::time::sleep(std::time::Duration::from_secs(15)).await;\n',
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — a sleep between XREAD cycles delays every "
               "published SSE event by up to 15 s; XREAD's 1 s BLOCK already "
               "throttles the loop, so E6 must flag any reintroduced sleep",
    },
    {
        "name": "events-read-errors-swallowed",
        "file": "portal/src/events.rs",
        "old": "if errors >= 3 {",
        "new": "if false {",
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — swallowing read errors keeps the HTTP stream "
               "open while no events arrive, so the browser never resumes its "
               "poller; E7 must pin error propagation",
    },
    {
        "name": "events-ready-frame-removed",
        "file": "portal/src/events.rs",
        "old": '"event: ready\\ndata: {}\\n\\n"',
        "new": '"event: nope\\ndata: {}\\n\\n"',
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — without the ready frame the client pauses "
               "polling on headers alone and can miss an event posted before "
               "XREAD starts",
    },
    {
        "name": "events-tenant-filter-dropped",
        "file": "portal/src/events.rs",
        "old": "e.tenant == viewer.tenant && e.visibility.visible_to(viewer)",
        "new": "e.visibility.visible_to(viewer)",
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — the shared stream will carry other tenants; "
               "dropping the tenant check leaks their Team events",
    },
    {
        "name": "events-cursor-sanitize-dropped",
        "file": "portal/src/events.rs",
        "old": "sanitize_cursor(last_id)",
        "new": 'last_id.unwrap_or_else(|| "$".into())',
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — a crafted Last-Event-ID must not wedge the "
               "reader in an endless XREAD error loop",
    },
    {
        "name": "events-client-pauses-on-open",
        "file": "portal/web/src/lib/useEventStream.ts",
        "old": '    es.addEventListener("ready", () => {\n'
               '      setConnected(true);\n'
               '      dispatchEvent({ id: "sse-ready", kind: SSE_READY_KIND, payload: {} });\n'
               '    });',
        "new": '    es.onopen = () => setConnected(true);',
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — onopen fires before the backend issues XREAD; "
               "pausing polling there loses events posted in the gap",
    },
    {
        "name": "events-stale-cursor-check-dropped",
        "file": "portal/src/events.rs",
        "old": "                cursor_checked = reader.ensure_cursor_live().await;\n",
        "new": "                cursor_checked = true;\n",
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — without the tail comparison a valid-but-future "
               "Last-Event-ID returns Nil forever while still emitting ready "
               "and keeping the poller paused; E7 must pin the call site",
    },
    {
        "name": "events-update-recipients-dropped",
        "file": "portal/src/main.rs",
        "old": '                    events::Visibility::Recipients(recipients_for_task_update(\n'
               '                        &task,\n'
               '                        &t,\n'
               '                        &session.sub,\n'
               '                    )),',
        "new": '                    events::Visibility::Recipients(recipients_for_task(&t, &session.sub)),',
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — a reassignment must still notify the previous "
               "assignee; E4 must pin the pre/post-patch recipient union",
    },
    {
        "name": "events-ready-reconcile-dropped",
        "file": "portal/web/src/components/TasksPanel.tsx",
        "old": '    if (event.kind === SSE_READY_KIND) void fullLoad();',
        "new": '    if (event.kind === "never") { /* no reconcile */ }',
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — without authoritative reload on ready, a lost "
               "delete XADD leaves stale tasks after a replay; E7 must pin "
               "the panel reconciliation",
    },
    {
        "name": "events-intercom-ready-reconcile-dropped",
        "file": "portal/web/src/components/IntercomPanel.tsx",
        "old": '    if (event.kind === SSE_READY_KIND) void fullLoad();',
        "new": '    if (event.kind === "never") { /* no reconcile */ }',
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — intercom notes need the same authoritative "
               "reload on ready as tasks; E7 must pin both panels",
    },
    {
        "name": "events-stale-cursor-comparison-flipped",
        "file": "portal/src/events.rs",
        "old": "        if newest < cursor {\n",
        "new": "        if newest > cursor {\n",
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — inverting the tail comparison keeps a future "
               "cursor instead of resetting it, wedgeing XREAD at Nil; E7 "
               "must pin the direction",
    },
    {
        "name": "events-ready-before-cursor-check",
        "file": "portal/src/events.rs",
        "old": "                    if !ready && cursor_checked {\n",
        "new": "                    if !ready {\n",
        "check": "tests/00-static/check_events.py",
        "why": "portal-events — announcing ready while the cursor check is "
               "still inconclusive pauses the poller on a possibly wedged "
               "cursor; E7 must pin the combined gate",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Shadow-incompatible checks.
#
# A wired check that CANNOT meaningfully prove teeth via a shadow mutation
# must be declared here with a truthful reason. The coverage self-check at
# the bottom of this file requires every wired check to be either targeted
# by at least one MUTATIONS entry or documented in this set — anything in
# neither is silently dead teeth and fails the meta-test.
#
# "Shadow-incompatible" means one of:
#   * the check reads NO repo file (a mutation cannot affect its exit code —
#     mutating anything would be a no-op, not a teeth proof);
#   * the check needs tooling/state the throwaway shadow copy cannot provide
#     (mutating would still prove nothing because the run cannot be green on
#     an unmutated baseline).
#
# Keys are `tests/00-static/check_*.py` basenames; values are the one-line
# reason that explains WHY the check cannot be exercised in the shadow.
# ─────────────────────────────────────────────────────────────────────────────
SHADOW_INCOMPATIBLE = {
    "check_platform.py": (
        "platform versions are read LIVE from kubectl/ssh/docker on the "
        "orchestration hosts (check_platform.py opens no repo file), so a "
        "shadow copy changes nothing a mutation could flip red — a shadow "
        "mutation here would be a no-op, not a teeth proof."
    ),
    "check_mutations.py": (
        "is this very meta-test harness — mutating it would mutate the guard "
        "under test. Its teeth are proven by (a) every declared MUTATIONS "
        "entry being required to flip its target check RED, and (b) the "
        "full-coverage self-check at the bottom of this file, which fails "
        "whenever any wired check appears in neither MUTATIONS nor "
        "SHADOW_INCOMPATIBLE."
    ),
}


SKIP_SUBTREES = {
    "portal/target",           # rust build artifacts (3.2 GB, never read)
    "portal/web/node_modules", # portal deps (673 MB, never read)
    "portal/web/.next",        # next.js build output (158 MB, never read)
    "backups",                 # volume dumps (24 MB, never read)
    "openspec",                # spec/change artifacts (checks only need contracts/)
    "mail/mailcow-dockerized", # vendored compose subtree (49 MB, only mail/*.yml is read)
}


def _should_skip(rel: Path) -> bool:
    """True if this repo-relative path lies under a SKIP_SUBTREE prefix."""
    rel_s = rel.as_posix()
    return any(rel_s == sk or rel_s.startswith(sk + "/") for sk in SKIP_SUBTREES)


def build_shadow(mutation: dict) -> Path:
    """Create an isolated copy of the repo with mutation applied.

    A real copy (minus SKIP_SUBTREES) is used instead of symlinks because
    every check derives ROOT via ``Path(__file__).resolve().parents[2]`` —
    a symlinked check would resolve to the original repo and the mutation
    would be invisible. Only the mutated file is edited; the check then
    runs against the throwaway copy, so real files stay intact.
    """
    tmp = Path(tempfile.mkdtemp(prefix="opensme-mut-"))
    _copy_tree(ROOT, tmp, Path())  # start rel at repo-root so skips match
    dst = tmp / mutation["file"]
    text = dst.read_text()
    old = mutation["old"]
    assert text.count(old) == 1, (
        f"{mutation['name']}: old text must match exactly once "
        f"(found {text.count(old)}): {old!r}"
    )
    mutated = text.replace(old, mutation["new"])
    assert mutated != text, (
        f"{mutation['name']}: replacement produced no change — mutation is "
        "a no-op"
    )
    dst.write_text(mutated)
    return tmp


def _copy_tree(src: Path, dst: Path, _rel: Path | None = None) -> None:
    """Deep-copy src into dst, skipping declared bulky subtrees.

    ``_rel`` is the child's repo-relative path (fed through recursion) so
    SKIP_SUBTREES prefixes are matched against the real on-disk location.
    """
    for child in sorted(src.iterdir()):
        rel = (_rel / child.name) if _rel is not None else child
        if _should_skip(rel):
            continue
        out = dst / child.name
        if child.is_symlink():
            # dangling/host-relative symlinks are never read by the checks;
            # skip rather than break the shadow copy. MUST be tested before
            # is_dir(): is_dir() follows symlinks, so a symlink that resolves
            # to a directory would recurse into itself (e.g. a gitignored
            # node_modules symlink) until ELOOP.
            continue
        if child.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            _copy_tree(child, out, rel)
        else:
            try:
                shutil.copy2(child, out)
            except (PermissionError, OSError):
                # unreadable residuals in vendored subtrees (e.g. mode 000)
                # are never read by any targeted check; skip them
                continue


def run_check(shadow: Path, check_rel: str) -> int:
    """Run a check against the shadow tree; returns exit code."""
    env = dict(os.environ)
    # conftest computes ROOT from __file__, so pointing at the shadow's
    # copy of a check gives the shadow ROOT automatically.
    full = shadow / check_rel
    if not full.exists():
        return 99  # phantom reference — nobody can pass that
    proc = subprocess.run(
        [sys.executable, str(full)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(shadow),
        env=env,
    )
    return proc.returncode


def _wired_check_names() -> set[str]:
    """Every tests/00-static/check_*.py registered in tests/run.py Layer 0.

    Same conservative scan check_suite_coverage.py uses: any reference to a
    `tests/00-static/check_*.py` path in run.py counts as wired. This keeps
    the meta-test's teeth inventory in lockstep with the runner, so adding a
    wired check without a mutation (or shadow-incompat reason) goes red here
    automatically.
    """
    run_text = (ROOT / "tests" / "run.py").read_text()
    return set(re.findall(r"tests/00-static/(check_[A-Za-z0-9_]+\.py)", run_text))


def _check_full_coverage(room: Result) -> int:
    """Prove the mutation suite covers EVERY wired static check.

    A wired check that is neither targeted by a MUTATIONS entry nor declared
    in SHADOW_INCOMPATIBLE has no provable teeth: the suite would report
    green even if that check's invariant silently rotted. That is exactly the
    dead-check failure mode this meta-test exists to prevent, so an uncovered
    wired check fails the run. Returns the number of uncovered checks.
    """
    wired = _wired_check_names()
    mutated = {Path(m["check"]).name for m in MUTATIONS}
    covered = mutated | set(SHADOW_INCOMPATIBLE)
    uncovered = sorted(wired - covered)
    total = len(wired)
    count = len(wired & covered)
    print(
        f"mutation-coverage: {count}/{total} wired checks have teeth or "
        "documented shadow-incompat"
    )
    if uncovered:
        room.fail(
            "wired checks with NO mutation teeth and NO shadow-incompat "
            f"reason: {', '.join(uncovered)} — add a MUTATIONS entry that "
            "flips each red, or document a truthful reason in "
            "SHADOW_INCOMPATIBLE"
        )
    else:
        room.ok(
            f"all {total} wired checks are mutation-targeted or documented "
            "shadow-incompatible"
        )
    return len(uncovered)


def check(room: Result) -> int:
    failures: list[str] = []
    _info: list[str] = []

    for mutation in MUTATIONS:
        name = mutation["name"]
        src = ROOT / mutation["file"]
        if not src.exists():
            failures.append(f"{name}: mutation source {mutation['file']} missing")

    if failures:
        for f in failures:
            room.fail(f)
        return 0 if room.summary() else 1

    for mutation in MUTATIONS:
        name = mutation["name"]
        shadow = None
        try:
            shadow = build_shadow(mutation)
            rc = run_check(shadow, mutation["check"])
            if rc == 0:
                failures.append(
                    f"{name}: mutation {'->'.join((mutation['old'], mutation['new']))!r} "
                    f"left {mutation['check']} GREEN — the invariant "
                    f"{mutation['why']} is no longer enforced (either the "
                    "check logic weakened or the file shape moved)"
                )
            else:
                room.ok(f"{name}: caught ({mutation['check']} rc={rc})")
        except Exception as e:
            failures.append(f"{name}: mutation harness error: {e}")
        finally:
            if shadow is not None:
                shutil.rmtree(shadow, ignore_errors=True)

    # Full-teeth coverage: every wired check must be mutation-targeted or
    # documented shadow-incompatible. This runs AFTER the mutations so its
    # red output is attributable to coverage, not to an individual mutation.
    _check_full_coverage(room)

    for f in failures:
        room.fail(f)
    return 0 if room.summary() else 1


def main() -> int:
    room = Result("sme-mutation-tests")
    room.header(
        "Layer 0: mutation tests — every critical check has teeth "
        "(isolated shadow repo)"
    )
    return check(room)


if __name__ == "__main__":
    sys.exit(main())
