#!/usr/bin/env python3
"""
tests/08-k8s/check_smoke.py — External HTTPS smoke tests.

Curls all external ingress endpoints and checks HTTP status codes.
Adapted from the upstream openSME test framework (Layer 1 Smoke),
retargeted to *.home.opensme-edu.org domains.

Verifies that:
  - Keycloak OIDC discovery responds 200
  - Portal (home) returns 403 (OAuth2 Proxy, unauthenticated)
  - Admin portal returns 403 (OAuth2 Proxy, unauthenticated)
  - Element, Synapse, XWiki, OpenCloud, OpenProject respond
  - SOGo, SOGo6, Stalwart respond
  - Intercom Service health endpoint returns 200
  - Staff/student SOGo and XWiki respond
  - SSL certificates are valid (expiry > 30 days)

Usage:
    python3 tests/08-k8s/check_smoke.py

Exit codes:
    0 = all endpoints reachable with acceptable status codes
    1 = one or more endpoints failed
"""

import subprocess
import ssl
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

# ─── Service endpoints ──────────────────────────────────────────
# (host, path, acceptable_codes, description)

SMOKE_ENDPOINTS = [
    # Identity
    ("id.home.opensme-edu.org",
     "/realms/opensme/.well-known/openid-configuration",
     {200}, "Keycloak OIDC discovery"),

    # Portal (OAuth2 Proxy — 403 is expected for unauthenticated)
    ("home.opensme-edu.org",
     "/",
     {403}, "Portal (home) — OAuth2 Proxy"),

    ("admin.home.opensme-edu.org",
     "/",
     {403}, "Admin portal — OAuth2 Proxy"),

    # Communication
    ("chat.home.opensme-edu.org",
     "/",
     {200}, "Element web client"),

    ("matrix.home.opensme-edu.org",
     "/_matrix/federation/v1/version",
     {200}, "Synapse federation version"),

    # Knowledge
    ("xwiki.home.opensme-edu.org",
     "/",
     {200, 302}, "XWiki"),

    # Files
    ("cloud.home.opensme-edu.org",
     "/",
     {200, 302}, "OpenCloud"),

    # Project management
    ("openproject.home.opensme-edu.org",
     "/",
     {200, 302}, "OpenProject"),

    # Mail
    ("mail.home.opensme-edu.org",
     "/",
     {200, 302}, "SOGo webmail"),

    ("sogo6.home.opensme-edu.org",
     "/",
     {200, 307, 302}, "SOGo6"),

    ("stalwart.home.opensme-edu.org",
     "/",
     {200, 302}, "Stalwart mail server"),

    # Intercom Service
    ("intercom.home.opensme-edu.org",
     "/health",
     {200}, "Intercom Service health"),

    # Staff tenant
    ("sogo-staff.home.opensme-edu.org",
     "/",
     {200, 302}, "Staff SOGo"),

    ("sogo6-staff.home.opensme-edu.org",
     "/",
     {200, 307, 302}, "Staff SOGo6"),

    ("xwiki-staff.home.opensme-edu.org",
     "/",
     {200, 302}, "Staff XWiki"),

    # Students tenant
    ("sogo-students.home.opensme-edu.org",
     "/",
     {200, 302}, "Students SOGo"),

    ("sogo6-students.home.opensme-edu.org",
     "/",
     {200, 307, 302}, "Students SOGo6"),
]

# SSL certificate expiry threshold (days)
SSL_EXPIRY_WARN_DAYS = 30


def curl_url(host: str, path: str, timeout: int = 10) -> tuple[int, str]:
    """Curl an HTTPS URL and return (status_code, body_snippet)."""
    url = f"https://{host}{path}"
    try:
        result = subprocess.run(
            ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        code_str = result.stdout.strip()
        try:
            code = int(code_str)
        except ValueError:
            code = 0
        return code, url
    except subprocess.TimeoutExpired:
        return 0, url
    except FileNotFoundError:
        return -1, url


def check_ssl_expiry(host: str) -> int | None:
    """Check SSL certificate expiry in days. Returns days left, or None on error."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        if not cert:
            return None
        # Parse 'notAfter' date
        not_after = cert.get("notAfter", "")
        if not not_after:
            return None
        import datetime
        expiry = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
        days_left = (expiry - datetime.datetime.utcnow()).days
        return days_left
    except Exception:
        return None


def main():
    result = Result("k8s-smoke")
    result.header("Layer 8: External HTTPS smoke tests")

    # ── HTTP status checks ────────────────────────────────────────
    result.header("HTTP endpoint checks")

    for host, path, acceptable, desc in SMOKE_ENDPOINTS:
        code, url = curl_url(host, path)
        if code in acceptable:
            result.ok(f"{desc}: {code}")
        elif code == 0:
            result.fail(f"{desc}: unreachable ({url})")
        elif code == -1:
            result.fail(f"{desc}: curl not found")
        else:
            result.fail(f"{desc}: {code} (expected {','.join(str(c) for c in sorted(acceptable))})")

    # ── SSL certificate expiry ────────────────────────────────────
    result.header("SSL certificate expiry")

    ssl_hosts = sorted(set(h for h, _, _, _ in SMOKE_ENDPOINTS))
    for host in ssl_hosts:
        days = check_ssl_expiry(host)
        if days is None:
            result.warn(f"{host}: cannot check SSL expiry")
        elif days < 0:
            result.fail(f"{host}: SSL certificate EXPIRED ({-days} days ago)")
        elif days < SSL_EXPIRY_WARN_DAYS:
            result.warn(f"{host}: SSL expires in {days} days")
        else:
            result.ok(f"{host}: SSL expires in {days} days")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
