#!/usr/bin/env python3
"""
tests/00-static/check_webmail_sso.py — Layer 0 validation of the webmail
(stalwart) OIDC backchannel contract.

Webmail users sign in through the central IdP (Zitadel): SOGo obtains an
access token and authenticates to stalwart via SASL XOAUTH2, and stalwart
validates that token against the IdP's userinfo endpoint. The whole hop is
compose-internal, so a silent drift does not break any compose lint — the
webmail just stays empty / refuses every SSO login. This check pins the
contract statically on a fresh checkout:

  W1 — Default directory: mail/stalwart/config.toml.template must select the
       oidc directory (`directory = "oidc"`). Any other default directory
       has nothing to validate OIDC access tokens against — XOAUTH2 logins
       are refused and plain-password logins only ever see app passwords.
  W2 — Userinfo endpoint shape: the [directory.oidc] block must use
       endpoint.method = "userinfo" and an endpoint.url that points at the
       traefik internal-only 'idpplain' entrypoint (:8081) under the
       external IdP hostname (${ZITADEL_DOMAIN}) — plain HTTP because the
       edge's per-boot self-signed certificate cannot be pinned (the
       release build of stalwart verifies TLS unconditionally), external
       Host header because Zitadel 404s requests whose Host differs from
       its configured ExternalDomain.
  W3 — IdP backchannel pin: the shared compose must NOT hardcode the IdP
       hostname to a container/private/link-local IP. Inside the compose
       network Docker's DNS resolves the external IdP hostname via the
       traefik network alias, whose internal-only `idpplain` entrypoint
       (:8081) answers the userinfo call (Host-rewrite middleware keeps
       Zitadel's ExternalDomain check happy) — the demo/CI stacks rely on
       this. Fleet deployments (VM: host-network traefik owns :8081 on the
       docker0 bridge) inject an extra_hosts '<idp>:host-gateway' pin from
       their ansible overlay, which is also accepted here. A hardcoded
       test/private IP dead-ends the hop (W1/W3 regression guard).
  W4 — Container identity: container_name must stay opensme-stalwart —
       traefik routing, healthchecks and operator runbooks refer to the
       container by this exact name.

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_webmail_sso.py
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

STALWART_YML = ROOT / "mail" / "stalwart.yml"
CONFIG_TEMPLATE = ROOT / "mail" / "stalwart" / "config.toml.template"
ZITADEL_YML = ROOT / "idm" / "zitadel.yml"


# ── Parsing helpers ─────────────────────────────────────────────

def load_service(compose_path: Path, service: str) -> dict:
    """Return a parsed compose service block ({} if file/service missing)."""
    if not compose_path.exists():
        return {}
    data = yaml.safe_load(compose_path.read_text()) or {}
    return (data.get("services") or {}).get(service) or {}


def toml_section(text: str, section: str) -> str:
    """Return the body of a TOML `[section]` header.

    The body spans from the header line up to the next `[section.header]`
    line (or EOF), with the header itself excluded.
    """
    m = re.search(
        rf"^\[{re.escape(section)}\][^\n]*\n(.*?)(?=^\[|\Z)",
        text,
        re.M | re.S,
    )
    return m.group(1) if m else ""


# ── Rules ───────────────────────────────────────────────────────

def check_default_directory(result: Result) -> None:
    """W1 — the rendered config must keep 'oidc' as the default directory.

    Only an oidc-type directory validates Zitadel access tokens
    (XOAUTH2/OAUTHBEARER from SOGo) against the userinfo endpoint and
    auto-provisions the principal on first login.
    """
    if not CONFIG_TEMPLATE.exists():
        result.fail(
            "mail/stalwart/config.toml.template missing — cannot verify "
            "the webmail SSO directory (W1)"
        )
        return
    text = CONFIG_TEMPLATE.read_text()
    if re.search(r'^directory = "oidc"\s*$', text, re.M):
        result.ok(
            'stalwart default directory is oidc (directory = "oidc")'
        )
    else:
        result.fail(
            'mail/stalwart/config.toml.template (W1): no line '
            '`directory = "oidc"` — without the oidc directory stalwart '
            "cannot validate Zitadel access tokens (XOAUTH2) and every "
            "webmail SSO login is refused"
        )


def check_userinfo_endpoint(result: Result) -> None:
    """W2 — [directory.oidc] must call userinfo on the idpplain hop.

    endpoint.method must be "userinfo" and endpoint.url must carry the
    external IdP hostname (${ZITADEL_DOMAIN} — Zitadel 404s other Host
    headers) on plain HTTP port :8081 (traefik's internal-only 'idpplain'
    entrypoint — TLS at the edge uses a per-boot self-signed certificate
    that cannot be pinned).
    """
    if not CONFIG_TEMPLATE.exists():
        result.fail(
            "mail/stalwart/config.toml.template missing — cannot verify "
            "the userinfo endpoint (W2)"
        )
        return
    body = toml_section(CONFIG_TEMPLATE.read_text(), "directory.oidc")
    if not body:
        result.fail(
            "mail/stalwart/config.toml.template (W2): no [directory.oidc] "
            "block — there is no userinfo endpoint to validate webmail "
            "SSO tokens against"
        )
        return

    ok_method = re.search(r'^endpoint\.method = "userinfo"\s*$', body, re.M) is not None

    m = re.search(r'^endpoint\.url = "([^"]+)"\s*$', body, re.M)
    url = m.group(1) if m else ""
    ok_host = "${ZITADEL_DOMAIN}" in url
    ok_port = ":8081" in url

    if ok_method and ok_host and ok_port:
        result.ok(
            "[directory.oidc] endpoint: method=userinfo, "
            "url=${ZITADEL_DOMAIN}:8081 (idpplain, external Host header)"
        )
        return
    if not ok_method:
        result.fail(
            'mail/stalwart/config.toml.template (W2): [directory.oidc] '
            'endpoint.method must be "userinfo" — the webmail token '
            "validation dead-ends without it"
        )
    if not (ok_host and ok_port):
        result.fail(
            "mail/stalwart/config.toml.template (W2): [directory.oidc] "
            "endpoint.url must contain ${ZITADEL_DOMAIN} (external Host "
            "header — Zitadel 404s unknown Hosts) and :8081 (traefik's "
            f"internal-only idpplain entrypoint); found {url!r}"
        )


def check_idp_backchannel_pin(result: Result) -> None:
    """W3 — no hardcoded-IP IdP backchannel pin in the shared compose.

    Inside the compose network Docker's DNS resolves the external IdP
    hostname through the traefik network alias; the internal-only
    'idpplain' entrypoint (:8081) answers the userinfo call with a
    Host-rewrite middleware (see config.toml.template [directory.oidc] for
    why the TLS entrypoint cannot serve it). The demo/CI stacks rely on
    this alias path, so stalwart.yml must NOT pin the IdP hostname to a
    container/private/link-local IP — that dead-ends the hop there. Fleet
    deployments (VM: host-network fleet traefik owns :8081 on the docker0
    bridge) inject an extra_hosts '<idp>:host-gateway' pin from their
    ansible overlay; host-gateway is accepted (and harmless) here too.
    """
    svc = load_service(STALWART_YML, "stalwart")
    if not svc:
        result.fail(
            "mail/stalwart.yml: service 'stalwart' missing — cannot verify "
            "the IdP backchannel pin (W3)"
        )
        return
    hosts = svc.get("extra_hosts") or []
    entries = hosts.values() if isinstance(hosts, dict) else hosts
    hard = []
    for e in entries:
        if not isinstance(e, str):
            continue
        if e.endswith(":host-gateway"):
            continue  # fleet overlay pattern; harmless in shared compose
        # any non-host-gateway pin for the IdP host, or any literal
        # TEST-NET / RFC1918 / link-local target
        if "ZITADEL_DOMAIN" in e or re.search(
                r":(203\.0\.113\.|10\.|192\.168\.|"
                r"172\.(1[6-9]|2\d|3[01])\.|127\.)", e):
            hard.append(e)
    if hard:
        result.fail(
            "mail/stalwart.yml (W3): IdP backchannel must not be pinned to "
            "a hardcoded IP (got %r) — the composed stacks resolve the IdP "
            "hostname via the traefik network alias / idpplain; the fleet "
            "overlay's host-gateway pin is injected outside this repo"
            % hard[0]
        )
        return
    result.ok(
        "stalwart IdP backchannel pin-free in shared compose (fleet "
        "overlay injects host-gateway; alias path covers demo/CI)"
    )


def check_container_identity(result: Result) -> None:
    """W4 — container_name must stay opensme-stalwart.

    Traefik routing, healthchecks and operator runbooks refer to the
    container by this exact name; a rename orphans all of them.
    """
    svc = load_service(STALWART_YML, "stalwart")
    if not svc:
        result.fail(
            "mail/stalwart.yml: service 'stalwart' missing — cannot verify "
            "container_name (W4)"
        )
        return
    if svc.get("container_name") == "opensme-stalwart":
        result.ok("stalwart container_name is opensme-stalwart")
    else:
        result.fail(
            "mail/stalwart.yml (W4): container_name is "
            f"{svc.get('container_name')!r}, expected 'opensme-stalwart' — "
            "traefik labels, healthchecks and runbooks refer to the "
            "container by that exact name"
        )


def check_issuer_override(result: Result) -> None:
    """W5 — the idpplain router's Host-rewrite must also override the
    forwarded proto/port.

    Stalwart dials the userinfo endpoint through the internal-only
    'idpplain' entrypoint (http://host:8081). Traefik forwards
    X-Forwarded-Proto:http + X-Forwarded-Port:8081; Zitadel derives the
    expected access-token issuer from those and gets
    'http://host:8081' instead of the token's real iss
    (https://<ZITADEL_DOMAIN>) → PermissionDenied at userinfo → SMTP 535
    on XOAUTH2 submission. The zitadel-idp-host middleware must therefore
    set Host + X-Forwarded-Proto=https + X-Forwarded-Port=443.
    (Verified live 2026-10-26 from CI zitadel logs.)
    """
    if not ZITADEL_YML.exists():
        result.fail("idm/zitadel.yml missing — cannot check W5")
        return
    text = ZITADEL_YML.read_text()
    need = [
        "traefik.http.middlewares.zitadel-idp-host.headers.customrequestheaders."
        "Host=",
        "traefik.http.middlewares.zitadel-idp-host.headers.customrequestheaders."
        "X-Forwarded-Host=",
        "traefik.http.middlewares.zitadel-idp-host.headers.customrequestheaders."
        "X-Forwarded-Proto=https",
        "traefik.http.middlewares.zitadel-idp-host.headers.customrequestheaders."
        "X-Forwarded-Port=443",
    ]
    missing = [n for n in need if n not in text]
    if missing:
        result.fail(
            "idm/zitadel.yml (W5): idpplain userinfo would fail issuer "
            "matching — zitadel-idp-host middleware missing %s (SMTP 535 "
            "'issuer does not match' at userinfo)" % ", ".join(missing)
        )
        return
    result.ok(
        "zitadel-idp-host overrides Host + X-Forwarded-Proto/Port (issuer "
        "matches the token's https origin)"
    )


# ── Checks ─────────────────────────────────────────────────────

def main() -> bool:
    result = Result("sme-webmail-sso")
    result.header(
        "Layer 0: webmail (stalwart) SSO backchannel — oidc directory, "
        "idpplain userinfo hop, forwarded issuer override"
    )

    check_default_directory(result)
    check_userinfo_endpoint(result)
    check_idp_backchannel_pin(result)
    check_container_identity(result)
    check_issuer_override(result)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
