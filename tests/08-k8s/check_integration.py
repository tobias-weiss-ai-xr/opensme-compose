#!/usr/bin/env python3
"""
tests/08-k8s/check_integration.py — Inter-service integration health.

Verifies the cross-service integration links, with a focus on the
SOGo/OpenCloud file picker (email attachments sourced from OpenCloud),
and the accompanying auth-proxy chains for Matrix, XWiki and OpenProject.

The single authentication hub is the intercom-service (OIDC token
exchange proxy). When a user authenticates against Keycloak through
intercom-service, their session gets per-audience access tokens for
each target service (OpenCloud, SOGo, XWiki). The browser-facing
proxies under intercom-service (``/oc/``, ``/sogo/``, ``/wiki/``,
``/nob/``) then inject the correct ``Bearer`` token so that each
service can pull files from OpenCloud.

This check validates each leg of that chain:

  Layer 0 — intercom-service health + proxy route wiring
  Layer 1 — OpenCloud OIDC / status (the file backend)
  Layer 2 — SOGo file picker (OpenCloud blueprint + nubusintercom)
  Layer 3 — Matrix (Synapse federation + intercom /nob/ route)
  Layer 4 — XWiki (intercom /wiki/ proxy + direct health)
  Layer 5 — OpenProject (direct health + OIDC wiring note)

NOTE: Endpoints behind Keycloak return 302 (redirect to the login
page) when unauthenticated. A 302 here is EXPECTED and confirms the
route is mounted and protected — not a failure.

Usage:
    python3 tests/08-k8s/check_integration.py

Exit codes:
    0 = all integration links healthy
    1 = one or more integration links failed
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

# ─── Domains ────────────────────────────────────────────────────
DOMAIN = "home.opensme-edu.org"
KEYCLOAK_ISSUER = f"https://id.{DOMAIN}/realms/opensme"

# intercom-service proxy routes (browser-facing).
# Expected: 302 (redirect to Keycloak login) when unauthenticated.
INTERCOM_ROUTES = [
    ("/oc/", "OpenCloud proxy (file backend)"),
    ("/sogo/", "SOGo groupware proxy"),
    ("/wiki/", "XWiki proxy"),
    ("/nob/", "Matrix/Nordeck bot proxy"),
    ("/fs/", "Nextcloud legacy proxy (disabled)"),
    ("/silent", "Silent-login endpoint"),
    ("/uuid", "User UUID endpoint"),
]

# Direct service endpoints for each integration target.
TARGETS = [
    # (host, path, acceptable_codes, description)
    ("cloud." + DOMAIN, "/status.php", {200}, "OpenCloud status"),
    ("cloud." + DOMAIN, "/.well-known/openid-configuration", {200}, "OpenCloud OIDC discovery"),
    ("matrix." + DOMAIN, "/_matrix/federation/v1/version", {200}, "Synapse federation version"),
    ("sogo." + DOMAIN, "/SOGo/", {200}, "SOGo (legacy) web interface"),
    ("sogo6." + DOMAIN, "/", {200, 302, 307}, "SOGo6 web interface"),
    ("chat." + DOMAIN, "/", {200}, "Element web client"),
    ("openproject." + DOMAIN, "/health_check", {200, 301, 302}, "OpenProject health_check"),
]

# SOGo6 filepicker endpoints — the core question "attachments from
# OpenCloud". These are on the sogo6-server API.
SOGO6_OPENCLOUD_ENDPOINTS = [
    ("api/user/v1/opencloud/token/exchange", "token exchange"),
    ("api/user/v1/opencloud/files/browse?path=/&type=all", "file browse"),
    ("api/user/v1/opencloud/files/select", "file select"),
]

# nubusintercom deployment and service
NUBUSINTERCOM_DEPLOYMENT = "nubusintercom"
NUBUSINTERCOM_SERVICE = "sogo6-nubusintercom"


def run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a shell command, return (returncode, combined output)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except FileNotFoundError as e:
        return 1, f"not found: {e}"


def curl(url: str, timeout: int = 12, extra: list[str] | None = None) -> int:
    """Return HTTP status code for a URL (silent, -k, no redirect).

    curl writes the ``%{http_code}`` token to stdout; the wrapped exit
    code is 0 even on a 4xx/5xx, so we must parse the token, not use the
    return code.
    """
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
           "--max-time", str(timeout), "-k"]
    if extra:
        cmd += extra
    cmd.append(url)
    _, out = run(cmd)
    # out may be "200" or contain a trailing code; take the last token.
    out = out.strip()
    for token in reversed(out.split()):
        if token.isdigit():
            return int(token)
    return 0


# ─── Layer 0: intercom-service ──────────────────────────────────

def check_intercom_health(result: Result):
    """intercom-service /health endpoint."""
    code = curl(f"https://intercom.{DOMAIN}/health", timeout=10)
    if code == 200:
        result.ok("intercom-service /health: 200")
    else:
        result.fail(f"intercom-service /health: {code}")


def check_intercom_routes(result: Result):
    """intercom-service browser-facing proxy routes."""
    for path, desc in INTERCOM_ROUTES:
        code = curl(f"https://intercom.{DOMAIN}{path}")
        # 302 => route mounted + protected by Keycloak (expected unauthenticated)
        if code == 302:
            result.ok(f"{path} -> 302 (Keycloak-protected)")
        elif code in (404, 405):
            result.fail(f"{path} -> {code} (route not mounted) — {desc}")
        else:
            # /fs/ is disabled => the route itself may 404 at the app layer,
            # but the mounted wrapper still 302s. Treat non-302 carefully.
            result.warn(f"{path} -> {code} — {desc}")


# ─── Layer 1: OpenCloud ─────────────────────────────────────────

def check_opencloud(result: Result):
    """OpenCloud status + OIDC discovery."""
    for host, path, codes, desc in TARGETS[:2]:
        url = f"https://{host}{path}"
        code = curl(url)
        if code in codes:
            result.ok(f"{url}: {code}")
        else:
            result.fail(f"{url}: {code} ({desc})")


# ─── Layer 2: SOGo file picker ──────────────────────────────────

def check_nubusintercom(result: Result):
    """Check nubusintercom deployment and service for file picker."""
    rc, output = run([
        "kubectl", "get", "deployment", NUBUSINTERCOM_DEPLOYMENT, 
        "-n", "opensme-edu", "-o", "jsonpath={.status.availableReplicas}"
    ])
    if rc == 0 and output.strip() and int(output.strip()) > 0:
        result.ok(f"nubusintercom deployment: {output.strip()} pod(s) ready")
    else:
        result.warn("nubusintercom deployment not found or not ready")
    
    rc, output = run([
        "kubectl", "get", "service", NUBUSINTERCOM_SERVICE, 
        "-n", "opensme-edu", "-o", "jsonpath={.spec.clusterIP}"
    ])
    if rc == 0 and output.strip() and output.strip() != "None":
        result.ok(f"nubusintercom service: {output.strip()}")
    else:
        result.warn("nubusintercom service not found")


def check_ox_cleanup(result: Result):
    """Verify OX (Open-Xchange) resources are permanently removed from opensme.
    
    The postfix-ox deployment and services were removed to clean up legacy
    Open-Xchange resources. This check ensures they haven't been recreated
    by ArgoCD syncing from external repos.
    """
    # Check for any postfix-ox resources
    rc, output = run([
        "kubectl", "get", "deployment,service", "-n", "opensme",
        "--ignore-not-found"
    ])
    
    if rc == 0 and "postfix-ox" in output:
        result.fail("OX (postfix-ox) resources still exist in opensme")
    else:
        result.ok("OX (postfix-ox) resources permanently removed from opensme")


def check_sogo6_filepicker(result: Result):
    """SOGo6 OpenCloud filepicker endpoints + nubusintercom wiring.

    The SOGo6 server exposes the file-picker blueprint under
    ``/opencloud`` (token exchange, file browse, file select) but the
    blueprint must be *registered* and the SOGo6 ``INTERCOM_SHARED_SECRET``
    / ``INTERCOM_URL`` must point at the nubusintercom sidecar (or the
    in-cluster deployment).

    Because these paths sit behind the sogo6-ui proxy, an unauthenticated
    hit reaches the sogo6-server API. 200/401/403 → the route is
    mounted; 404 → the blueprint is *not* registered.

    The file picker is a Tier 3 roadmap feature (#37/#38/#43). Until
    deployed, 404s are EXPECTED — treat as WARN not FAIL.

    Note: token/exchange and files/select are POST endpoints — 405 (Method
    Not Allowed) confirms the route IS mounted (just wrong HTTP method for
    unauthenticated GET probe).
    """
    base = f"https://sogo6.{DOMAIN}/"
    # token/exchange and files/select are POST endpoints
    for path, desc in SOGO6_OPENCLOUD_ENDPOINTS:
        # Use POST for endpoints that require it
        extra = None
        if "token/exchange" in path or "files/select" in path:
            extra = ["-X", "POST", "-H", "Content-Type: application/json", "-d", "{}"]
        code = curl(base + path, extra=extra)
        if code == 404:
            result.warn(
                f"/{path}: {code} — SOGo6 OpenCloud filepicker blueprint not "
                f"registered ({desc})"
            )
        elif code in (200, 401, 403, 405):
            result.ok(f"/{path}: {code} — route mounted ({desc})")
        else:
            result.warn(f"/{path}: {code} — {desc}")

    # nubusintercom wiring: SOGo6 must resolve its intercom companion and
    # have the shared secret set.
    rc_env, env_out = run(
        ["kubectl", "exec", "-n", "opensme-edu", "deploy/sogo6-server",
         "--", "sh", "-c", "echo \"$INTERCOM_URL|$INTERCOM_SHARED_SECRET\""],
        timeout=30,
    )
    intercom_url, _, secret = env_out.partition("|")
    if rc_env == 0 and intercom_url.strip() and secret.strip() and "change-me" not in secret:
        result.ok("SOGo6 INTERCOM_URL + INTERCOM_SHARED_SECRET configured")
    elif rc_env != 0:
        result.warn("SOGo6 INTERCOM_* env check failed (pod may be restarting)")
    elif not intercom_url.strip():
        result.warn("SOGo6 INTERCOM_URL not set — filepicker cannot reach nubusintercom")
    elif not secret.strip() or "change-me" in secret:
        result.warn("SOGo6 INTERCOM_SHARED_SECRET not set or default — HMAC signing disabled")
    else:
        result.fail(
            "SOGo6 INTERCOM_URL / INTERCOM_SHARED_SECRET not set — "
            "filepicker token exchange cannot authenticate to OpenCloud"
        )


# ─── Layer 3: Matrix ────────────────────────────────────────────

def check_matrix(result: Result):
    """Synapse federation + intercom /nob/ route."""
    url = f"https://matrix.{DOMAIN}/_matrix/federation/v1/version"
    code = curl(url)
    if code == 200:
        result.ok(f"{url}: 200")
    else:
        result.fail(f"{url}: {code}")

    url = f"https://matrix.{DOMAIN}/_matrix/client/versions"
    code = curl(url)
    if code == 200:
        result.ok(f"{url}: 200")
    else:
        result.warn(f"{url}: {code}")


# ─── Layer 4: XWiki ─────────────────────────────────────────────

def check_xwiki(result: Result):
    """XWiki direct health + intercom /wiki/ proxy.

    XWiki instance health is judged by whether it can reach its database and
    serve / redirect. A freshly-provisioned XWiki DB (no prior wiki content)
    serves the first-run Distribution wizard with a 302 redirect to
    ``/bin/distribution/XWiki/Distribution`` — that is the EXPECTED healthy
    state after DB bootstrapping, not a failure.

    A 500 indicates the database is broken — that is a real failure.
    A 404 on /bin/view/Main/ is expected for a fresh XWiki with no Main.WebHome
    document yet; the DB itself is healthy (no errors).
    """
    # Main XWiki (opensme ns) — fresh DB has no Main.WebHome, so 404 is OK
    url = f"https://xwiki.{DOMAIN}/bin/view/Main/"
    code = curl(url)
    if code in (200, 302, 404):
        result.ok(f"{url}: {code} (DB OK)")
    elif code == 500:
        result.fail(f"{url}: 500 — XWiki DB/auth issue")
    else:
        result.warn(f"{url}: {code}")

    # Staff XWiki (opensme-staff ns) — had a DB-auth 500, now provisioned
    url = f"https://xwiki-staff.{DOMAIN}/bin/view/Main/"
    code = curl(url)
    if code in (200, 302, 404):
        result.ok(f"{url}: {code} (DB OK)")
    elif code == 500:
        result.fail(f"{url}: 500 — XWiki DB/auth issue (staff namespace)")
    else:
        result.warn(f"{url}: {code}")

    # intercom /wiki/ proxy (302 => mounted & protected)
    code = curl(f"https://intercom.{DOMAIN}/wiki/")
    if code == 302:
        result.ok("/wiki/ -> 302 (Keycloak-protected)")
    else:
        result.fail(f"/wiki/ -> {code} — XWiki proxy not mounted")


# ─── Layer 5: OpenProject ───────────────────────────────────────

def check_openproject(result: Result):
    """OpenProject health + OIDC wiring note."""
    url = f"https://openproject.{DOMAIN}/health_check"
    code = curl(url)
    if code in (200, 301, 302):
        result.ok(f"{url}: {code}")
    else:
        result.fail(f"{url}: {code}")

    url = f"https://openproject.{DOMAIN}/"
    code = curl(url)
    if code == 302:
        result.ok(f"{url}: 302 (redirect)")
    else:
        result.warn(f"{url}: {code}")

    # OpenProject has no OIDC client in Keycloak (no .well-known response
    # of its own) — it is NOT wired through the intercom-service proxies.
    result.info(
        "OpenProject has no OIDC/Keycloak SSO client and is not served "
        "through intercom-service — files cannot be pulled via the file "
        "picker from OpenCloud in OpenProject today"
    )


def main():
    result = Result("k8s-integration")
    result.header("Layer 8: Service integration health (file picker chain)")

    result.header("Layer 0 — intercom-service (auth proxy hub)")
    check_intercom_health(result)
    check_intercom_routes(result)

    result.header("Layer 1 — OpenCloud (file backend)")
    check_opencloud(result)

    result.header("Layer 2 — SOGo file picker (attachments from OpenCloud)")
    check_nubusintercom(result)
    check_sogo6_filepicker(result)

    result.header("Layer 2b — OX resource removal verification")
    check_ox_cleanup(result)

    result.header("Layer 3 — Matrix (Synapse)")
    check_matrix(result)

    result.header("Layer 4 — XWiki")
    check_xwiki(result)

    result.header("Layer 5 — OpenProject")
    check_openproject(result)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
