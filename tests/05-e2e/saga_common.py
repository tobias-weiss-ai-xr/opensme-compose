#!/usr/bin/env python3
"""
tests/05-e2e/saga_common.py — shared precondition harness for saga journeys.

Extracts the portal-probe, service-deployed, and endpoint-probe logic that
every catalog saga journey shares (previously inlined in saga_smoke.py).

Public API:
    probe_portal(session, domain, localish, schema) -> (portal_base, services)
        Finds the portal, confirms /health, and returns the base URL plus the
        rendered tile list from /api/services. Returns (None, []) if the
        portal is unreachable.
    service_deployed(services, catalog_id) -> bool
        True when a catalog function's tile is rendered (its id appears in
        the /api/services payload).
    probe_endpoint(session, url, expect_status=(200, 401, 403)) -> (ok, detail)
        Probes a service endpoint. Returns (True, status) when the endpoint
        answers with an expected status (auth wall, redirect, or success);
        (False, reason) on 404 or connection failure.
"""

from __future__ import annotations

import requests

from run import T  # noqa: E402  (shared per-request timeout)

# Catalog functions that are NOT auto-visible: saga preconditions. A saga
# journey skips cleanly when a participating service's tile is absent.
OPTIONAL_CATALOG = (
    "support", "website", "shop", "invoice", "archive", "chat", "notes",
)

# Auth-wall + success statuses that count as "the endpoint answers".
# Redirects (301/302/303/307/308) mean the app redirected to a login page,
# which also counts as answering. 400 is the notes API's "no data, no
# redirect target" wall (see saga_smoke P5).
_OK_STATUSES = (200, 301, 302, 303, 307, 308, 400, 401, 403)


def probe_portal(session: requests.Session, domain: str, localish: bool,
                 schema: str, env_portal: str = "") -> tuple[str | None, list[dict]]:
    """Find the portal, confirm /health, and return (base_url, services).

    Probes candidate portal hosts (env override, bare domain, portal.<domain>,
    home.<domain>); the first that answers /health with 200 wins. Then fetches
    /api/services for the rendered tile list. Returns (None, []) if the portal
    is unreachable or /api/services fails.
    """
    candidates: list[str] = []
    if env_portal and env_portal != domain:
        candidates.append(env_portal)
    candidates += [domain, f"portal.{domain}", f"home.{domain}"]

    portal_base: str | None = None
    for cand in candidates:
        if localish and cand == f"portal.{domain}" and domain in ("localhost", "127.0.0.1"):
            base = "http://127.0.0.1:8080"
        else:
            base = f"{schema}{cand}"
        try:
            if session.get(base + "/health", timeout=T).status_code == 200:
                portal_base = base
                break
        except requests.RequestException:
            continue
    if portal_base is None:
        return None, []

    svc_resp = session.get(portal_base + "/api/services", timeout=T)
    if svc_resp.status_code != 200:
        return portal_base, []
    services = svc_resp.json().get("services") or []
    return portal_base, services


def service_deployed(services: list[dict], catalog_id: str) -> bool:
    """True when the catalog function's tile is rendered (id in /api/services)."""
    return any(s.get("id") == catalog_id for s in services)


def service_url(services: list[dict], catalog_id: str) -> str | None:
    """Return the URL for a rendered catalog function, or None if absent."""
    for s in services:
        if s.get("id") == catalog_id:
            return s.get("url", "")
    return None


def probe_endpoint(session: requests.Session, url: str,
                   expect_status: tuple[int, ...] = _OK_STATUSES,
                   allow_redirects: bool = False) -> tuple[bool, str]:
    """Probe a service endpoint.

    Returns (True, status_str) when the endpoint answers with an expected
    status (auth wall, redirect, or success); (False, reason) on 404 or
    connection failure. 404 or a network error means the service is absent or
    misrouted — callers decide whether that is a skip (not deployed) or a
    fail (deployed but broken).
    """
    try:
        r = session.get(url, allow_redirects=allow_redirects, timeout=T)
    except requests.RequestException as e:
        return False, f"unreachable ({e.__class__.__name__})"
    if r.status_code in expect_status:
        return True, str(r.status_code)
    if r.status_code == 404:
        return False, "404"
    return False, str(r.status_code)
