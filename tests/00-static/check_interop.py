#!/usr/bin/env python3
"""
tests/00-static/check_interop.py — Layer 0 validation of cross-service
interoperability (contracts/interop.yml).

Drift between consumer and provider breaks user journeys that static
layers cannot see (they need a running stack). This check pins the
DECLARED integration surfaces to the files that implement them:

  1. OIDC: both IdM swap points exist; the portal takes its IdP from IDP_URL.
  2. SMTP: every declared mail consumer defaults to the stalwart provider.
  3. Feeds: feeds.rs admits http(s) only; PORTAL_FEEDS reaches the portal.
  4. Share links: INTERCOM_ATTACHMENT_HOSTS is wired, defaulting to the
     cloud domain; the portal never proxies attachments.
  5. Processes: portal gets REST-only BPM_URL — no engine DB credentials
     anywhere in the portal env; BPMN bootstrap artifacts exist.
  6. Document flow: invoice storage and paperless export volumes exist.

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_interop.py
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT  # noqa: E402

INTEROP = ROOT / "contracts" / "interop.yml"
COMPOSE_MAIN = ROOT / "docker-compose.yml"


def main():
    result = Result("sme-interoperability")
    result.header("Layer 0: cross-service interoperability validation")

    failures = []
    contract = yaml.safe_load(INTEROP.read_text())
    surfaces = contract["surfaces"]
    main_doc = yaml.safe_load(COMPOSE_MAIN.read_text())
    portal_env = set((main_doc["services"]["portal"].get("environment") or {}).keys())

    def env_of(compose_file: Path) -> dict:
        doc = yaml.safe_load((ROOT / compose_file).read_text())
        merged = {}
        for svc in (doc or {}).get("services", {}).values():
            if isinstance(svc, dict) and isinstance(svc.get("environment"), dict):
                merged.update(svc["environment"])
        return merged

    # ── 1. OIDC: IdM swap points + portal wiring ───────────────────────
    oidc = surfaces["oidc"]
    for idm in oidc["idm_services"]:
        if not (ROOT / "idm" / f"{idm}.yml").exists():
            failures.append(f"IdM swap point missing: idm/{idm}.yml")
    if oidc["portal_env"] not in portal_env:
        failures.append(f"portal env lacks {oidc['portal_env']} (IdP wiring)")

    # ── 2. SMTP: consumers default to the provider ─────────────────────
    smtp = surfaces["smtp_submission"]
    provider = smtp["provider"]
    for c in smtp["consumers"]:
        env = env_of(Path(c["env_file"]))
        val = env.get(c["env"], "")
        if provider not in str(val):
            failures.append(
                f"mail consumer {c['service']}: {c['env']} does not default "
                f"to '{provider}' (got {val!r})"
            )

    # ── 3. Feeds: scheme allow-list + portal env ───────────────────────
    feeds = surfaces["feeds"]
    if feeds["portal_env"] not in portal_env:
        failures.append(f"portal env lacks {feeds['portal_env']}")
    feeds_src = (ROOT / "portal" / "src" / "feeds.rs").read_text()
    if not re.search(
        r'starts_with\("https://"\)\s*\|\|\s*\w+\.starts_with\("http://"\)', feeds_src
    ):
        failures.append("feeds.rs does not admit http(s) URLs only (scheme allow-list drifted)")

    # ── 4. Share links: attachment host wiring ─────────────────────────
    shares = surfaces["share_links"]
    if shares["portal_env"] not in portal_env:
        failures.append(f"portal env lacks {shares['portal_env']}")
    intercom_src = (ROOT / "portal" / "src" / "intercom.rs").read_text()
    if shares["portal_env"] not in intercom_src:
        failures.append("intercom.rs no longer honors INTERCOM_ATTACHMENT_HOSTS")
    # No undeclared file-content routes: portal routes are navigation/API
    # only. Declared exceptions live in contracts/interop.yml
    # (share_links.picked_file_routes — the picked-file local store).
    main_src = (ROOT / "portal" / "src" / "main.rs").read_text()
    routes = re.findall(r'\.route\(\s*"([^"]+)"', main_src, re.DOTALL)
    declared = [r.rstrip("/") for r in shares.get("picked_file_routes", [])]

    def is_declared(route: str) -> bool:
        return any(route == d or route.startswith(d + "/") for d in declared)

    fileish = [
        r
        for r in routes
        if re.search(r"/s/|attachment|file|share", r) and not is_declared(r.rstrip("/"))
    ]
    if fileish:
        failures.append(
            f"portal serves undeclared file-content routes (must link, "
            f"not mediate; declare in contracts/interop.yml): {fileish}"
        )

    # ── 5. Processes: REST-only + BPMN bootstrap ───────────────────────
    proc = surfaces["processes"]
    if proc["portal_env"] not in portal_env:
        failures.append(f"portal env lacks {proc['portal_env']} (engine REST wiring)")
    dbish = [k for k in portal_env if re.search(r"DB|DATABASE|SQL", k) and "camunda" in k.lower()]
    if dbish:
        failures.append(f"portal holds engine DB credentials: {dbish}")
    for bpmn in proc["bootstrap"]:
        if not (ROOT / bpmn).exists():
            failures.append(f"BPMN bootstrap artifact missing: {bpmn}")

    # ── 6. Document flow volumes ───────────────────────────────────────
    ninja_env = env_of(Path("services/invoice-ninja.yml"))
    paper_doc = yaml.safe_load((ROOT / "services" / "paperless.yml").read_text())
    paper_vols = " ".join(
        " ".join(s.get("volumes") or []) for s in paper_doc["services"].values()
    )
    if "invoiceninja-storage" not in str(env_of(Path("services/invoice-ninja.yml"))) and \
       "invoiceninja-storage" not in (ROOT / "services" / "invoice-ninja.yml").read_text():
        failures.append("invoice storage volume (document source) missing")
    if "paperless-export" not in paper_vols:
        failures.append("paperless export volume (DMS ingest sink) missing")

    # ── 7. SSO round-trip surfaces: routed host == IdP-facing host ──────
    # Every OIDC consumer must derive its public/callback base URL from the
    # SAME variables Compose routes (ZITADEL_DOMAIN / OPENSME_DOMAIN). A
    # leftover ${DOMAIN} without the OPENSME_ prefix silently produces a
    # public_baseurl that differs from the routed host — the login redirect
    # then bounces to an unroutable name (seen 2026-10-16: synapse
    # public_baseurl=https://matrix.opensme.graphwiz.ai while the router
    # listens on matrix.home.opensme.graphwiz.ai).
    synapse_sh = (ROOT / "services" / "synapse-setup.sh").read_text()
    if re.search(r"matrix\.\$\{DOMAIN(?::|-)", synapse_sh):
        failures.append(
            "synapse-setup.sh derives public_baseurl from ${DOMAIN}; must use "
            "OPENSME_DOMAIN (SYNAPSE_SERVER_NAME already does) so SSO "
            "redirects resolve on the routed host"
        )
    # public_baseurl must be derived from SYNAPSE_SERVER_NAME (the compose /
    # coexist-authoritative host) so server_name and public_baseurl always
    # agree — an independent derivation drifts on multi-domain deploys.
    if "PUBLIC_BASEURL=\"https://${SERVER_NAME}\"" not in synapse_sh:
        failures.append(
            "synapse-setup.sh must derive public_baseurl from "
            "SYNAPSE_SERVER_NAME (server_name/public_baseurl agreement)"
        )
    # The compose-provided server_name must be the authoritative host, and
    # the setup script must reference it (not hardcode a bare domain).
    synapse_yml = (ROOT / "services" / "synapse.yml").read_text()
    if "SYNAPSE_SERVER_NAME" not in synapse_yml or "OPENSME_DOMAIN" not in synapse_yml:
        failures.append(
            "services/synapse.yml must set SYNAPSE_SERVER_NAME from "
            "OPENSME_DOMAIN (authoritative routed matrix host)"
        )
    if '"${SYNAPSE_SERVER_NAME' not in synapse_sh:
        failures.append(
            "synapse-setup.sh must take server_name from "
            "SYNAPSE_SERVER_NAME (compose-provided)"
        )

    # ── 8. SSO: groupware must not fall back to an own login ───────────
    # Redis auth must be uniform: the server enforces requirepass
    # (REDIS_PASSWORD), so every consumer URL must carry it. A bare
    # redis://host/db consumer stays green while the server is unauthenticated
    # and breaks the moment requirepass arrives (seen 2026-10-16: paperless
    # bare, notes broken with AUTH error because the live server still ran
    # without requirepass from a pre-password deploy).
    # Epic C3: webmail must reach the central IdP. SOGo ≥5.12 supports
    # OpenID natively (SOGoAuthenticationType = openid). Implementing the
    # full chain also requires a Stalwart XOAUTH2 directory — deploy-wide
    # change, tracked as a warning (the E2E journey section_sogo_sso
    # likewise warns, it cannot pass until the whole chain is wired).
    sogo_yml = (ROOT / "mail" / "sogo.yml").read_text()
    if "SOGoOIDCEnabled" not in sogo_yml and "OIDC" not in sogo_yml.upper():
        result.warn(
            "mail/sogo.yml ships no OIDC wiring — webmail login falls "
            "back to SOGo's own form (Epic C3, needs Zitadel client + "
            "Stalwart XOAUTH2 directory)"
        )

    # ── 9. Notes: the Django backend (mozilla-django-oidc) must be on the
    # public router, not just the static SPA. The coexist overlay routes
    # PathPrefix(/api) to notes-backend but began without /oidc — every
    # OIDC path (authenticate/callback/logout) then 404s on the frontend
    # and Notes SSO is dead even with a registered Zitadel client.
    notes_yml = (ROOT / "services" / "notes.yml").read_text()
    # Require a backend router whose Rule routes BOTH the JSON API and the
    # OIDC exchange to notes-backend; a bare SPA router (as shipped at
    # 2026-10-16) leaves /oidc/* 404ing on the static frontend.
    api_route = "PathPrefix(`/api`)" in notes_yml or "PathPrefix(\"/api\")" in notes_yml
    oidc_route = "PathPrefix(`/oidc`)" in notes_yml or "PathPrefix(\"/oidc\")" in notes_yml
    if not (api_route and oidc_route):
        failures.append(
            "services/notes.yml must route notes-backend to BOTH "
            "PathPrefix(/api) AND PathPrefix(/oidc) on the public router — "
            "mozilla-django-oidc lives under /oidc/* (authenticate/"
            "callback/logout); unrouted, the static SPA 404s them and "
            "Notes SSO is dead (registered client or not)"
        )
    # impress uses django-configurations: values.ListValue(
    # environ_name="ALLOWED_HOSTS") inherits the DJANGO_ class prefix, so
    # the env var must be DJANGO_ALLOWED_HOSTS. Setting ALLOWED_HOSTS is
    # silently ignored (settings only ever see the container IP → every
    # public request is DisallowedHost → SSO/API 400; seen 2026-10-16).
    if "DJANGO_ALLOWED_HOSTS" not in notes_yml:
        failures.append(
            "services/notes.yml must set DJANGO_ALLOWED_HOSTS (impress/"
            "django-configurations prefix) — plain ALLOWED_HOSTS is ignored "
            "and the backend 400s every public request with DisallowedHost"
        )

    # ── 10. Redis consumers must all carry the server requirepass ─────┐─
    paper_yml = (ROOT / "services" / "paperless.yml").read_text()
    if "PAPERLESS_REDIS: redis://" in paper_yml and \
       "${REDIS_PASSWORD" not in paper_yml:
        failures.append(
            "services/paperless.yml PAPERLESS_REDIS must include the "
            "REDIS_PASSWORD (redis://:*@...) — a bare URL works only while "
            "the server runs without requirepass and breaks the moment "
            "Redis enforces it (Notes already broke live 2026-10-16)"
        )

    # ── 11. Profile redis command overrides must keep requirepass ──────
    # profiles/{demo.dev,demo.live,medium,small}.yml re-declare the redis
    # command and silently DROPPED --requirepass — the server came up
    # unauthenticated in production while consumers sent REDIS_PASSWORD
    # (Notes broke live with AUTH errors, 2026-10-16). Any override of the
    # redis command must carry requirepass.
    for pf in (ROOT / "profiles").glob("*.yml"):
        body = pf.read_text()
        if "\n  redis:\n    command:\n" in body and "--requirepass" not in body:
            failures.append(
                f"{pf.name} re-declares the redis command without "
                "--requirepass — profile overrides win over docker-compose.yml "
                "and the server quietly runs unauthenticated; add "
                "--requirepass ${REDIS_PASSWORD}"
            )

    if failures:
        for f in failures:
            result.fail(f)
    else:
        result.ok(
            "interoperability surfaces declared and implemented: "
            "OIDC swap points, stalwart SMTP consumers, http(s) feeds, "
            "share-link hosts, REST-only engine + BPMN bootstrap, "
            "document-flow volumes"
        )
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
