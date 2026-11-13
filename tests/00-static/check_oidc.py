#!/usr/bin/env python3
"""
tests/00-static/check_oidc.py — Layer 0 validation of the Zitadel OIDC
client registration contract.

A service that logs in through the central IdP (Zitadel) needs a REGISTERED
OIDC client app. When that client is missing the login dead-ends: the 2026-10
OpenCloud outage (infinite loading screen at cloud.home.opensme.graphwiz.ai)
was exactly this — authorize → 400 Errors.App.NotFound because env.j2 never
set OC_OIDC_CLIENT_ID and no app 'opencloud' existed in Zitadel.

This check pins the registration contract statically so the same drift is
caught on a fresh checkout, without a running stack:

  R1 — Registration coverage: every OIDC client-id variable referenced in a
       compose service must have a matching --app branch in
       scripts/zitadel-client.sh (the idempotent registrar that persists
       the Zitadel-minted pair into .env).
  R2 — Env documentation: every OIDC variable referenced by compose must be
       declared in .env.example. (check_env.py skips ${VAR:-default} forms,
       which is exactly how OC_OIDC_CLIENT_ID slipped through.)
  R3 — No phantom defaults: a non-empty ${VAR:-DEFAULT} fallback for a
       client-id must equal the APP_NAME of the branch that owns VAR — a
       made-up default (like 'opencloud') is a client that can never log in.

Plus the RUNTIME SSO invariants (post-registration contract — the client
exists, but the stack must still complete a real login):

  R4 — ocis IdP backchannel pin: opencloud.extra_hosts must pin the IdP
       hostname to host-gateway. Inside the compose network the public IdP
       hostname resolves to the Zitadel container IP where :443 is not
       listening — ocis discovery/JWKS fails and every API call 401s.
  R5 — ocis external-user handling: PROXY_AUTOPROVISION_ACCOUNTS must be
       'true' (external Zitadel subjects are provisioned on first login;
       without it ocis 401s every SSO user after token validation) and
       PROXY_ROLE_ASSIGNMENT_DRIVER must default to 'default' (the 'oidc'
       driver needs a groups claim the IdP does not emit).
  R6 — element server_name source: services/element.yml must NOT set
       ELEMENT_HOMESERVER_NAME — the entrypoint derives server_name from
       ELEMENT_DEFAULT_SERVER_URL so the two can never desync (a mismatch
       breaks @user:server IDs against synapse).
  R7 — zitadel login UI declared: idm/zitadel.yml must declare the
       'zitadel-login' service (the v4 login UI behind /ui/v2/login); with
       loginV2 required, dropping it dead-ends every browser login in a 404.
  R8 — JWT access tokens: scripts/zitadel-client.sh must register OIDC
       apps with accessTokenType (TOKEN_TYPE, default OIDC_TOKEN_TYPE_JWT)
       — Zitadel's OPAQUE default is rejected by every JWKS-validating
       backend (ocis 'invalid number of segments'), so a registrar that
       drops the field breaks every SSO login after the code exchange.
  R9 — sogo6 callback allowlist: the sogo branch of scripts/zitadel-client.sh
       must register the webmail OIDC callback(s) the RUNNING sogo6
       actually emits. sogo6 builds its redirect_uri as
       {public_base}/api/user/v1/auth/callback/{TENANT} where TENANT is
       taken from the USER's login domain (= the webmail host's parent,
       e.g. webmail.home.opensme.graphwiz.ai -> home.opensme.graphwiz.ai) —
       deriving it from OPENSME_DOMAIN (the apex) made Zitadel answer
       authorize with HTTP 400 "redirect_uri is missing in the client
       configuration" on every webmail login (live regression 2026-11-10).
       The branch must therefore derive the callback domain from SOGO_DOMAIN
       (or the SOGO_OIDC_DOMAIN override) and register BOTH the bare form
       and the tenant-suffixed form.

  R10 — IdP dialer pin: every compose service that dials
       https://${ZITADEL_DOMAIN} SERVER-SIDE (OIDC token/userinfo/JWKS,
       issuer URIs) must pin the hostname to host-gateway via extra_hosts.
       Inside the compose network a network alias on the zitadel service
       can shadow the public FQDN with the zitadel container IP, where
       only plain :8080 listens — server-side TLS calls then die with
       connection refused and the login 500s after the browser callback
       (live notes SSO outage 2026-11-13). Browser-facing URL vars
       (IDP_URL/OC_IDP_URL) are exempt — the user's browser resolves the
       public FQDN via public DNS — as are services that backchannel to
       the internal http://zitadel:8080 listener (portal).
  R11 — JWKS path: OIDC_OP_JWKS_ENDPOINT must use Zitadel's real key path
       /oauth/v2/keys — the OpenID-Connect-conventional
       /.well-known/openid-configuration/jwks 404s on Zitadel and kills
       the callback with a 500 during token validation.
  R12 — alias shadow guard: the zitadel service network alias must NEVER
       be ${ZITADEL_DOMAIN} — see R10. The alias is reserved for the
       non-public auth.opensme.local direct-connect name.

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_oidc.py
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import ComposeLoader, Result, ROOT

ZITADEL_CLIENT_SH = ROOT / "scripts" / "zitadel-client.sh"
SYNAPSE_SETUP_SH = ROOT / "services" / "synapse-setup.sh"
ENV_EXAMPLE = ROOT / ".env.example"
OPENCLOUD_YML = ROOT / "opencloud" / "opencloud.yml"
ELEMENT_YML = ROOT / "services" / "element.yml"
ZITADEL_YML = ROOT / "idm" / "zitadel.yml"

# Compose files that may carry OIDC client references (base + optionals).
COMPOSE_CANDIDATES = [
    "docker-compose.yml",
    "idm/zitadel.yml",
    "idm/casdoor.yml",
    "opencloud/opencloud.yml",
    "services/ticketing.yml",
    "services/notes.yml",
    "services/synapse.yml",
    "services/paperless.yml",
]


# ── Parsing helpers ─────────────────────────────────────────────

def extract_oidc_vars_from_compose(compose_path: Path) -> dict:
    """Return {var_name: default_or_None} for OIDC client references in a
    service env, e.g. OC_OIDC_CLIENT_ID → 'opencloud'.

    Only variables whose name clearly marks an OIDC client credential are
    collected (OIDC containing 'CLIENT', the RP alias, issuer is informational
    and excluded).
    """
    out: dict[str, str | None] = {}
    if not compose_path.exists():
        return out
    data = yaml.safe_load(compose_path.read_text())
    if not data:
        return out

    env_ref = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}")

    def is_client_var(name: str) -> bool:
        n = name.upper()
        if "OIDC" not in n:
            return False
        return ("CLIENT" in n) or n in ("OIDC_RP_CLIENT_ID", "OIDC_RP_CLIENT_SECRET")

    for svc in (data.get("services") or {}).values():
        if not svc:
            continue
        env = svc.get("environment") or {}
        # compose allows a list form ["KEY=value", ...] or a mapping
        env_items = env.items() if isinstance(env, dict) else [(e.split("=", 1)[0], e.split("=", 1)[1] if "=" in e else "") for e in env]
        for _, val in env_items:
            if not isinstance(val, str):
                continue
            for m in env_ref.finditer(val):
                var, default = m.group(1), m.group(2)
                if is_client_var(var):
                    # first occurrence wins (base file order)
                    out.setdefault(var, default or None)
    return out


def extract_app_branches(shell_path: Path) -> dict:
    """Parse scripts/zitadel-client.sh into {app_name: {APP_NAME, CLIENT_ID_KEY,
    CLIENT_SECRET_KEY, APP_PROJECT}} for every --app branch."""
    branches: dict[str, dict] = {}
    if not shell_path.exists():
        return branches
    lines = shell_path.read_text().splitlines()

    # A branch starts at a case-arm line like "  opencloud)" followed by
    # APP_NAME= / CLIENT_ID_KEY= / CLIENT_SECRET_KEY= assignments.
    arm_re = re.compile(r"^  ([a-z][a-z0-9_-]*)\)\s*$")
    assign_re = re.compile(r'^\s{4}(APP_NAME|CLIENT_ID_KEY|CLIENT_SECRET_KEY|APP_PROJECT)="([^"]*)"')

    current: str | None = None
    for ln in lines:
        m = arm_re.match(ln)
        if m:
            current = m.group(1)
            branches[current] = {}
            continue
        if current is None:
            continue
        a = assign_re.match(ln)
        if a:
            branches[current][a.group(1)] = a.group(2)
        # stop collecting once we leave the case arm assignments
        if a is None and "base_paths=" in ln:
            current = None
    # keep only branches that declare an OIDC client id key
    return {name: cfg for name, cfg in branches.items() if "CLIENT_ID_KEY" in cfg}


def env_example_vars(env_path: Path) -> set[str]:
    if not env_path.exists():
        return set()
    return set(
        re.findall(r"^([A-Z][A-Z0-9_]*)\s*[:=]", env_path.read_text(), re.M)
    )


def load_service(compose_path: Path, service: str) -> dict:
    """Return a parsed compose service block ({} if file/service missing)."""
    if not compose_path.exists():
        return {}
    data = yaml.safe_load(compose_path.read_text()) or {}
    return (data.get("services") or {}).get(service) or {}


def service_env(svc: dict) -> dict:
    """Flatten a service environment block (dict or list form) to a mapping."""
    env = svc.get("environment") or {}
    if isinstance(env, dict):
        return dict(env)
    out: dict[str, str] = {}
    for item in env:
        if isinstance(item, str):
            k, _, v = item.partition("=")
            out[k] = v
    return out


# ── Runtime SSO invariants (R4–R7) ─────────────────────────────

def check_ocis_backchannel_pin(result: Result) -> None:
    """R4 — ocis IdP backchannel must go through the host (host-gateway).

    ocis validates tokens against OC_OIDC_ISSUER over HTTPS. Docker's
    embedded DNS resolves the public IdP hostname to the Zitadel container
    IP, where only plain-HTTP :8080 listens — TLS lives at the edge
    traefik. A missing (or hardcoded-container-IP) extra_hosts pin makes
    server-side discovery/JWKS fail with connection refused on :443 and
    the login never completes, even with a perfectly registered client.
    """
    svc = load_service(OPENCLOUD_YML, "opencloud")
    if not svc:
        result.fail(
            "opencloud/opencloud.yml: service 'opencloud' missing — cannot "
            "verify the IdP backchannel pin"
        )
        return
    hosts = svc.get("extra_hosts") or []
    entries = hosts.values() if isinstance(hosts, dict) else hosts
    pinned = any(
        isinstance(e, str) and e.endswith(":host-gateway") for e in entries
    )
    if pinned:
        result.ok("ocis IdP backchannel pinned to host-gateway (extra_hosts)")
    else:
        result.fail(
            "opencloud extra_hosts has no '<idp-host>:host-gateway' entry — "
            "inside the compose network the IdP hostname resolves to the "
            "Zitadel container IP where :443 is not listening; ocis "
            "discovery/JWKS fails and the OIDC login dead-ends"
        )


def check_ocis_external_users(result: Result) -> None:
    """R5 — external (IdP) users must be autoprovisioned with the default
    role driver, or ocis 401s every SSO user right after a successful
    token validation ("Autoprovisioning disabled", proxy/account_resolver).
    """
    env = service_env(load_service(OPENCLOUD_YML, "opencloud"))

    autoprov = str(env.get("PROXY_AUTOPROVISION_ACCOUNTS", "")).strip().lower()
    ok_autoprov = autoprov == "true"

    driver = env.get("PROXY_ROLE_ASSIGNMENT_DRIVER")
    driver_default: str | None = None
    if isinstance(driver, str):
        m = re.match(r"^\$\{[^:}]+:-(.*)\}$", driver.strip())
        driver_default = (m.group(1) if m else driver).strip()
    ok_driver = driver_default is not None and driver_default.lower() == "default"

    if ok_autoprov and ok_driver:
        result.ok(
            "ocis external users: autoprovision=true, role driver 'default'"
        )
        return
    if not ok_autoprov:
        result.fail(
            "opencloud PROXY_AUTOPROVISION_ACCOUNTS must be 'true' — without "
            "it ocis 401s every SSO user after a successful token validation"
        )
    if not ok_driver:
        result.fail(
            "opencloud PROXY_ROLE_ASSIGNMENT_DRIVER must default to 'default' "
            "— the 'oidc' driver needs a groups claim the IdP does not emit"
        )


def check_element_homeserver_source(result: Result) -> None:
    """R6 — element server_name has exactly ONE source:
    ELEMENT_DEFAULT_SERVER_URL. The entrypoint derives server_name from it
    (scheme stripped) precisely so base_url and server_name stay in sync; a
    separate ELEMENT_HOMESERVER_NAME would reintroduce the two-variable
    desync that broke @user:server IDs against synapse.
    """
    if not ELEMENT_YML.exists():
        result.fail("services/element.yml missing — cannot verify server_name source")
        return
    if "ELEMENT_HOMESERVER_NAME" in ELEMENT_YML.read_text():
        result.fail(
            "services/element.yml must not set ELEMENT_HOMESERVER_NAME — "
            "server_name is derived from ELEMENT_DEFAULT_SERVER_URL in the "
            "entrypoint; a second source desyncs @user:server IDs against "
            "synapse"
        )
    else:
        result.ok(
            "element server_name derived from ELEMENT_DEFAULT_SERVER_URL "
            "(no ELEMENT_HOMESERVER_NAME)"
        )


def check_zitadel_login_ui(result: Result) -> None:
    """R7 — the Zitadel v4 login UI service must stay declared.

    `authorize` redirects humans to /ui/v2/login (loginV2.required is on).
    The Next.js app behind it is a separate service — if it is dropped or
    renamed, every browser login dead-ends in Zitadel's 404 while the
    headless journeys (Session API) still look green.
    """
    data: dict = {}
    if ZITADEL_YML.exists():
        data = yaml.safe_load(ZITADEL_YML.read_text()) or {}
    if "zitadel-login" in (data.get("services") or {}):
        result.ok("zitadel-login (v4 login UI) service declared in idm/zitadel.yml")
    else:
        result.fail(
            "idm/zitadel.yml: service 'zitadel-login' missing — loginV2 is "
            "required, so every browser login dead-ends in a 404 without it"
        )


def check_app_token_type(result: Result) -> None:
    """R8 — OIDC apps must be registered with JWT access tokens.

    Zitadel v4.19 defaults new OIDC apps to OPAQUE access tokens, but
    every backend in this stack validates the bearer token against the
    IdP JWKS endpoint — opencloud/ocis answers opaque tokens with
    'invalid number of segments', notes reads OIDC_OP_JWKS_ENDPOINT, and
    synapse/ticketing validate the JWKS too. The registrar must therefore
    pin accessTokenType in the AddOIDCApp POST body (from TOKEN_TYPE) and
    keep the OIDC_TOKEN_TYPE_JWT default — otherwise every SSO login
    401s right after a successful code exchange.
    """
    if not ZITADEL_CLIENT_SH.exists():
        result.fail(
            "scripts/zitadel-client.sh missing — cannot verify JWT "
            "access-token registration"
        )
        return
    text = ZITADEL_CLIENT_SH.read_text()

    # Extract the jq program that builds the AddOIDCApp POST body
    # (from 'app_body=' up to the closing quote of the '{ ... }' block).
    start = text.find("app_body=")
    end = text.find("}')\"", start) if start != -1 else -1
    jq_body = text[start:end] if (start != -1 and end != -1) else ""

    if "accessTokenType: $tokentype" in jq_body:
        result.ok(
            "OIDC app POST body pins accessTokenType (JWT by default)"
        )
    else:
        result.fail(
            "scripts/zitadel-client.sh: the OIDC app POST body must set "
            "accessTokenType: $tokentype — Zitadel's OPAQUE default is "
            "rejected by every JWKS-validating backend (ocis: 'invalid "
            "number of segments')"
        )

    if "OIDC_TOKEN_TYPE_JWT" in text:
        result.ok("OIDC_TOKEN_TYPE_JWT default present in zitadel-client.sh")
    else:
        result.fail(
            "scripts/zitadel-client.sh must default TOKEN_TYPE to "
            "OIDC_TOKEN_TYPE_JWT — JWKS-validating backends cannot read "
            "OPAQUE tokens"
        )


def check_sogo_redirect_allowlist(result: Result) -> None:
    """R9 — the sogo branch must register the callback URI(s) sogo6 emits.

    A wrong (apex-derived) tenant domain in the allowlist makes Zitadel
    answer the webmail authorize request with HTTP 400 "invalid_request:
    The requested redirect_uri is missing in the client configuration"
    (live 2026-11-10: deployed sogo6 emitted
    .../callback/home.opensme.graphwiz.ai but the allowlist only held
    .../callback/opensme.graphwiz.ai + opensme.local).
    """
    if not ZITADEL_CLIENT_SH.exists():
        result.fail(
            "scripts/zitadel-client.sh missing — cannot verify the sogo "
            "callback allowlist (R9)"
        )
        return
    text = ZITADEL_CLIENT_SH.read_text()

    # 1) the callback tenant domain must be derived from SOGO_DOMAIN (or its
    #    SOGO_OIDC_DOMAIN override), never from OPENSME_DOMAIN directly.
    if 'oidc_domain="$(env_get SOGO_OIDC_DOMAIN' in text and "${app_domain#webmail.}" in text:
        result.ok("sogo callback tenant domain derived from the webmail host "
                  "(SOGO_DOMAIN / SOGO_OIDC_DOMAIN, not OPENSME_DOMAIN)")
    else:
        result.fail(
            "scripts/zitadel-client.sh (R9): the sogo branch must derive "
            "oidc_domain from SOGO_OIDC_DOMAIN / ${SOGO_DOMAIN#webmail.} — "
            "deriving it from OPENSME_DOMAIN registers the apex callback and "
            "Zitadel 400s ('redirect_uri missing in client configuration') "
            "every webmail SSO login on a home.<apex> deployment"
        )

    # 2) both the bare and the tenant-suffixed sogo6 callback paths must be
    #    in the branch's base_paths.
    bare = "/api/user/v1/auth/callback\"" in text
    suffixed = ("/api/user/v1/auth/callback/${oidc_domain}") in text
    if bare and suffixed:
        result.ok("sogo6 callbacks registered: bare + tenant-suffixed "
                  "(/api/user/v1/auth/callback[/{domain}])")
    else:
        missing = []
        if not bare:
            missing.append("/api/user/v1/auth/callback (bare)")
        if not suffixed:
            missing.append("/api/user/v1/auth/callback/${oidc_domain}")
        result.fail(
            "scripts/zitadel-client.sh (R9): sogo branch base_paths missing "
            + "; ".join(missing) + " — the running sogo6 emits exactly "
            "these redirect_uris and Zitadel rejects any allowlist that "
            "lacks them with HTTP 400"
        )

    # 3) the .local tenant variant should stay registered too (the *.local
    #    login domain of the demo stack).
    if "/api/user/v1/auth/callback/${local_cb_domain}" in text:
        result.ok("sogo6 .local callback variant registered "
                  "(/api/user/v1/auth/callback/${local_cb_domain})")
    else:
        result.fail(
            "scripts/zitadel-client.sh (R9): sogo branch lacks the .local "
            "callback variant — demo-stack *.local logins (testuser@opensme.local) "
            "would 400 at the IdP"
        )


# ── Runtime SSO invariants (R10–R12) ───────────────────────────

DIAL_URL_RE = re.compile(r"https://\$\{ZITADEL_DOMAIN[^}]*\}")
# baked into SPA/nginx for the USER's browser only (public DNS resolves it)
BROWSER_ONLY_VARS = {"IDP_URL", "OC_IDP_URL"}
# services that dial the IdP server-side but whose issuer URL arrives from
# .env (no static ${ZITADEL_DOMAIN} literal in compose to scan): enforce the
# pin via the netns-owning service (network_mode: service:X shares it)
KNOWN_NETNS_DIALERS = {"invoiceninja": "invoiceninja-web"}


def _pinned(svc: dict) -> bool:
    """True if svc pins the IdP hostname to host-gateway via extra_hosts."""
    hosts = svc.get("extra_hosts") or []
    entries = hosts.values() if isinstance(hosts, dict) else hosts
    for e in entries:
        if not isinstance(e, str) or not e.endswith(":host-gateway"):
            continue
        host = e.rsplit(":", 1)[0]
        if "${ZITADEL_DOMAIN" in host or host.startswith("auth."):
            return True
    return False


def _env(svc: dict) -> dict:
    """Flattened service environment ({} when absent)."""
    env = svc.get("environment") or {}
    if isinstance(env, dict):
        return dict(env)
    out = {}
    for item in env:
        if isinstance(item, str) and "=" in item:
            k, _, v = item.partition("=")
            out[k] = v
    return out


def check_idp_dialer_pins(result: Result) -> None:
    """R10 — every server-side dialer of https://${ZITADEL_DOMAIN} needs the
    extra_hosts host-gateway pin (directly, or on its netns owner)."""
    loader = ComposeLoader(ROOT)
    loader.load()
    services = loader.services

    dialers: dict[str, str] = {}
    for name, entry in services.items():
        env = _env(entry["data"])
        if any(
            var not in BROWSER_ONLY_VARS
            and isinstance(val, str)
            and DIAL_URL_RE.search(val)
            for var, val in env.items()
        ):
            dialers[name] = "env"
    # synapse's issuer lives in the generated homeserver.yaml, not in compose
    if SYNAPSE_SETUP_SH.is_file() and DIAL_URL_RE.search(SYNAPSE_SETUP_SH.read_text()):
        dialers.setdefault("synapse", "services/synapse-setup.sh oidc_providers issuer")

    for name, src in sorted(dialers.items()):
        entry = services.get(name) or {}
        svc = entry.get("data") or {}
        env = _env(svc)
        netmode = svc.get("network_mode") or ""
        if isinstance(netmode, str) and netmode.startswith("service:"):
            owner = netmode.split("service:", 1)[1].strip()
            owner_svc = (services.get(owner) or {}).get("data") or {}
            if _pinned(owner_svc):
                result.ok(
                    f"{name}: IdP dialer — netns owner '{owner}' pinned "
                    "to host-gateway (extra_hosts)"
                )
            else:
                result.fail(
                    f"{name} dials https://${{ZITADEL_DOMAIN}} server-side "
                    f"({src}) through the netns of '{owner}', but '{owner}' "
                    "has no extra_hosts '<idp>:host-gateway' pin — inside "
                    "the compose network the IdP hostname can resolve to "
                    "the zitadel container IP where :443 is not listening; "
                    "the OIDC callback 500s on token exchange "
                    "(notes SSO outage 2026-11-13)"
                )
        elif _pinned(svc):
            result.ok(f"{name}: IdP dialer pinned to host-gateway (extra_hosts)")
        elif any(INTERNAL := ("http://zitadel" in v for v in env.values())):
            result.ok(
                f"{name}: dials the internal http://zitadel:8080 listener "
                "(no TLS pin needed)"
            )
        else:
            result.fail(
                f"{name} dials https://${{ZITADEL_DOMAIN}} server-side ({src}) "
                "but has no extra_hosts '<idp>:host-gateway' pin — inside "
                "the compose network the IdP hostname can resolve to the "
                "zitadel container IP where :443 is not listening; the OIDC "
                "callback 500s on token exchange (notes SSO outage 2026-11-13)"
            )

    # issuer-from-.env dialers: pin enforced on the netns owner
    for name, owner in sorted(KNOWN_NETNS_DIALERS.items()):
        if name not in services:
            continue  # optional stack not present in this checkout
        owner_svc = (services.get(owner) or {}).get("data") or {}
        if _pinned(owner_svc):
            result.ok(
                f"{name}: IdP dialer (issuer from .env) — netns owner "
                f"'{owner}' pinned to host-gateway"
            )
        else:
            result.fail(
                f"{name} exchanges OIDC tokens against https://"
                "${ZITADEL_DOMAIN} (issuer from .env) through the netns of "
                f"'{owner}', but '{owner}' has no extra_hosts "
                "'<idp>:host-gateway' pin (notes SSO outage 2026-11-13)"
            )

    if not dialers and not KNOWN_NETNS_DIALERS:
        result.fail("no IdP dialers found — is the compose set stale?")


def check_jwks_endpoint(result: Result) -> None:
    """R11 — JWKS endpoint vars must use Zitadel's real key path."""
    loader = ComposeLoader(ROOT)
    loader.load()
    checked = 0
    for name, entry in sorted(loader.services.items()):
        for var, val in sorted(_env(entry["data"]).items()):
            if "JWKS" not in var.upper() or not isinstance(val, str) or not val:
                continue
            if "${" in val and ":-" in val and "/oauth" not in val                     and "/.well-known" not in val:
                continue  # fully delegated to .env, nothing to verify
            checked += 1
            if "/.well-known" in val:
                result.fail(
                    f"{name}: {var} uses the OpenID-Connect-conventional "
                    "/.well-known path — Zitadel does not implement it "
                    "(404 → token validation 500s mid-login); use "
                    "/oauth/v2/keys (the jwks_uri Zitadel publishes)"
                )
            elif "/oauth/v2/keys" in val:
                result.ok(f"{name}: {var} uses Zitadel's real JWKS path")
            else:
                result.fail(
                    f"{name}: {var} is neither /oauth/v2/keys nor clearly "
                    "delegated to .env — cannot verify the JWKS path"
                )
    if not checked:
        result.fail("no JWKS endpoint var found in compose — contract drifted?")


def check_zitadel_alias_shadow(result: Result) -> None:
    """R12 — the zitadel network alias must not shadow the public FQDN."""
    data = yaml.safe_load(ZITADEL_YML.read_text()) or {}
    svc = (data.get("services") or {}).get("zitadel") or {}
    nets = svc.get("networks") or {}
    net = nets.get("opensme-net") if isinstance(nets, dict) else None
    aliases = net.get("aliases") if isinstance(net, dict) else None
    aliases = aliases or []
    shadow = [
        a for a in aliases
        if isinstance(a, str) and "${ZITADEL_DOMAIN" in a
    ]
    if shadow:
        result.fail(
            f"idm/zitadel.yml: zitadel network alias {shadow!r} shadows the "
            "PUBLIC IdP hostname: docker's embedded DNS then resolves it to "
            "the zitadel container IP for EVERY container on opensme-net, "
            "where only plain :8080 listens — server-side OIDC calls to "
            "https://auth.<domain> die with connection refused (notes SSO "
            "outage 2026-11-13). Alias the non-public auth.opensme.local "
            "instead; public-hostname dialers pin via extra_hosts (R10)"
        )
    else:
        result.ok(
            "zitadel network alias does not shadow the public IdP FQDN"
        )


# ── Checks ─────────────────────────────────────────────────────

def main() -> bool:
    result = Result("sme-oidc-clients")
    result.header(
        "Layer 0: Zitadel OIDC client registration contract "
        "(all services that log in through Zitadel need a registered app)"
    )

    branches = extract_app_branches(ZITADEL_CLIENT_SH)
    result.info(
        f"zitadel-client.sh --app branches: {sorted(branches)}"
    )

    # Collect every OIDC client-id var referenced anywhere in the compose set.
    referenced: dict[str, str | None] = {}
    for rel in COMPOSE_CANDIDATES:
        referenced.update(extract_oidc_vars_from_compose(ROOT / rel))

    if not referenced:
        result.fail("no OIDC client references found — is the compose set stale?")
        return result.summary()

    env_vars = env_example_vars(ENV_EXAMPLE)

    # Map CLIENT_ID_KEY → owning branch for the registration-coverage check.
    id_key_owner: dict[str, str] = {}
    for app, cfg in branches.items():
        k = cfg.get("CLIENT_ID_KEY")
        if k:
            id_key_owner[k] = app

    # ── R2: every referenced OIDC var must be documented in .env.example ──
    missing_doc = sorted(v for v in referenced if v not in env_vars)
    if missing_doc:
        for var in missing_doc:
            result.fail(
                f"{var} referenced by compose but NOT declared in .env.example "
                "(add a placeholder line so deployers know to set it)"
            )
    else:
        result.ok("all referenced OIDC vars are declared in .env.example")

    # ── R1: every client-id var needs a registration branch ──
    id_refs = sorted(v for v in referenced if v.endswith("CLIENT_ID") or v in ("OIDC_RP_CLIENT_ID",))
    orphan = sorted(v for v in id_refs if v not in id_key_owner)
    if orphan:
        for var in orphan:
            result.fail(
                f"{var} has no --app branch in scripts/zitadel-client.sh — "
                "this service can never complete a Zitadel login"
            )
    else:
        result.ok(f"all {len(id_refs)} OIDC client-id vars have a registration branch")

    # ── R3: a non-empty client-id default must match the branch APP_NAME ──
    phantom = []
    for var in id_refs:
        default = referenced.get(var)
        if not default:
            continue  # empty fallback = explicit opt-in, fine
        owner = id_key_owner.get(var)
        if owner is None:
            continue  # R1 already failed this one
        app_name = branches[owner].get("APP_NAME", "")
        if default != app_name:
            phantom.append(f"{var} (default '{default}' != branch '{owner}' APP_NAME '{app_name}')")
    if phantom:
        for p in phantom:
            result.fail(
                f"phantom OIDC client default: {p} — this default is not a "
                "registered client and users will hit Errors.App.NotFound"
            )
    else:
        result.ok("no phantom (unregistered) client-id defaults")

    # ── Runtime SSO invariants (R4–R7) ──
    check_ocis_backchannel_pin(result)
    check_ocis_external_users(result)
    check_element_homeserver_source(result)
    check_zitadel_login_ui(result)

    # ── R8: JWT access-token registration ──
    check_app_token_type(result)

    # ── R9: sogo6 webmail callback allowlist (tenant-domain mismatch) ──
    check_sogo_redirect_allowlist(result)

    # ── R10: server-side IdP dialers must pin the hostname (alias shadow) ──
    check_idp_dialer_pins(result)

    # ── R11: JWKS endpoint must be Zitadel's real key path ──
    check_jwks_endpoint(result)

    # ── R12: zitadel network alias must not shadow the public FQDN ──
    check_zitadel_alias_shadow(result)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
