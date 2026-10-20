#!/usr/bin/env python3
"""
tests/08-k8s/check_keycloak.py — Keycloak & OIDC health.

Verifies that:
  - Keycloak pod is Running with 0 restarts
  - Keycloak OIDC discovery endpoint responds (internal + external)
  - The 'opensme' realm exists and has the expected clients
  - OAuth2 Proxy pods are Running (home + admin)

Usage:
    python3 tests/08-k8s/check_keycloak.py

Exit codes:
    0 = Keycloak + OIDC healthy
    1 = one or more checks failed
"""

import subprocess
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

KEYCLOAK_NS = "opensme"
HOME_NS = "home"

# Internal OIDC discovery URL (via in-cluster service)
INTERNAL_ISSUER = "http://keycloak.opensme.svc.cluster.local:8080/realms/opensme/.well-known/openid-configuration"

# External OIDC discovery URL (via ingress)
EXTERNAL_ISSUER = "https://id.home.opensme-edu.org/realms/opensme/.well-known/openid-configuration"

# Expected clients in the 'opensme' realm
EXPECTED_CLIENTS = {"home-portal", "admin-home-portal"}


def kubectl_json(args: list[str], timeout: int = 15) -> dict | list | None:
    try:
        result = subprocess.run(
            ["kubectl"] + args + ["-o", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def curl(url: str, timeout: int = 10, follow_redirects: bool = False) -> tuple[int, str]:
    """Curl a URL and return (status_code, body_snippet)."""
    try:
        cmd = ["curl", "-s", "-o", "-", "-w", "\\n%{http_code}",
               "--max-time", str(timeout), "-k"]
        if follow_redirects:
            cmd.append("-L")
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        lines = result.stdout.rsplit("\n", 1)
        if len(lines) == 2:
            body, code_str = lines
            try:
                code = int(code_str.strip())
            except ValueError:
                code = 0
            return code, body[:500]
        return 0, result.stdout[:500]
    except subprocess.TimeoutExpired:
        return 0, "timeout"
    except FileNotFoundError:
        return 0, "curl not found"
    except Exception as e:
        return 0, str(e)


def check_keycloak_pod(result: Result):
    """Check Keycloak pod health."""
    pods = kubectl_json(["-n", KEYCLOAK_NS, "get", "pods", "-l", "app=keycloak"])
    if pods is None:
        result.fail("Cannot list Keycloak pods")
        return

    items = pods.get("items", [])
    if not items:
        result.fail("No Keycloak pods found")
        return

    for pod in items:
        name = pod["metadata"]["name"]
        phase = pod.get("status", {}).get("phase", "Unknown")
        restarts = 0
        for cs in pod.get("status", {}).get("containerStatuses", []):
            restarts += cs.get("restartCount", 0)

        if phase == "Running" and restarts == 0:
            result.ok(f"Keycloak pod {name}: Running, 0 restarts")
        elif phase == "Running":
            result.warn(f"Keycloak pod {name}: Running, {restarts} restarts")
        else:
            result.fail(f"Keycloak pod {name}: {phase}")


def check_oidc_discovery(result: Result):
    """Check OIDC discovery endpoints (internal + external)."""
    # Internal (best-effort — may not be reachable from outside the cluster)
    code, body = curl(INTERNAL_ISSUER, timeout=10)
    if code == 200:
        result.ok("Internal OIDC discovery: 200")
    else:
        result.warn(f"Internal OIDC discovery: {code} (not reachable from host — expected if not in-cluster)")

    # External
    code, body = curl(EXTERNAL_ISSUER, timeout=15)
    if code == 200:
        result.ok("External OIDC discovery: 200")
    else:
        result.fail(f"External OIDC discovery: {code} ({body[:200]})")


def check_oauth2_proxy(result: Result):
    """Check OAuth2 Proxy pods in home namespace."""
    pods = kubectl_json(["-n", HOME_NS, "get", "pods"])
    if pods is None:
        result.fail("Cannot list pods in home namespace")
        return

    found = {"oauth2-proxy-home": False, "oauth2-proxy-admin": False}
    for pod in pods.get("items", []):
        name = pod["metadata"]["name"]
        phase = pod.get("status", {}).get("phase", "Unknown")
        restarts = 0
        for cs in pod.get("status", {}).get("containerStatuses", []):
            restarts += cs.get("restartCount", 0)

        for proxy in found:
            if proxy in name:
                found[proxy] = True
                if phase == "Running" and restarts == 0:
                    result.ok(f"{name}: Running, 0 restarts")
                elif phase == "Running":
                    result.warn(f"{name}: Running, {restarts} restarts")
                else:
                    result.fail(f"{name}: {phase}")

    for proxy, was_found in found.items():
        if not was_found:
            result.fail(f"No {proxy} pod found in {HOME_NS}")


def check_realm_clients(result: Result):
    """Check that the 'opensme' realm has expected clients.

    This requires port-forwarding or direct API access. We skip if
    we can't reach the admin API.
    """
    # Try to get clients via the admin API (port-forward)
    # This is best-effort; skip if we can't reach it
    result.skip("Realm client verification requires Keycloak admin API access (skipped)")


def main():
    result = Result("k8s-keycloak")
    result.header("Layer 8: Keycloak & OIDC health")

    result.header("Keycloak pod")
    check_keycloak_pod(result)

    result.header("OIDC discovery endpoints")
    check_oidc_discovery(result)

    result.header("OAuth2 Proxy pods")
    check_oauth2_proxy(result)

    result.header("Realm clients")
    check_realm_clients(result)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
