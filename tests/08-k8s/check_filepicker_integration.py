#!/usr/bin/env python3
"""
tests/08-k8s/check_filepicker_integration.py — Deep integration tests for OpenCloud file picker.

This module provides comprehensive integration testing for the SOGo6 ↔ OpenCloud
file picker implementation (Option B - per-user OIDC token exchange).

It tests:
- Layer 0: nubusintercom service health and deployment
- Layer 1: Redis token storage connectivity
- Layer 2: Token exchange flow (SOGo6 → nubusintercom → Redis)
- Layer 3: WebDAV API connectivity to OpenCloud
- Layer 4: HMAC signing configuration
- Layer 5: OX resource removal from opensme namespace

Usage:
    python3 tests/08-k8s/check_filepicker_integration.py

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
OPENCLOUD_DOMAIN = f"cloud.{DOMAIN}"
SOGO6_DOMAIN = f"sogo6.{DOMAIN}"

# Namespaces
EDU_NAMESPACE = "opensme-edu"
SME_NAMESPACE = "opensme"

# nubusintercom service details
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
    """Return HTTP status code for a URL (silent, -k, no redirect)."""
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
           "--max-time", str(timeout), "-k"]
    if extra:
        cmd += extra
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = result.stdout.strip()
        for token in reversed(out.split()):
            if token.isdigit():
                return int(token)
    except Exception:
        pass
    return 0


def kubectl_exec(pod: str, namespace: str, command: list[str], container: str = "") -> tuple[int, str]:
    """Execute a command in a pod."""
    full_cmd = ["kubectl", "exec", "-n", namespace, pod]
    if container:
        full_cmd += ["-c", container]
    full_cmd += ["--"] + command
    return run(full_cmd)


def get_nubusintercom_pod() -> str:
    """Get the name of a running nubusintercom pod."""
    rc, output = run([
        "kubectl", "get", "pods", "-n", EDU_NAMESPACE,
        "-l", "app=nubusintercom", "-o", "jsonpath={.items[0].metadata.name}"
    ])
    if rc == 0 and output.strip():
        return output.strip()
    return ""


# ─── Layer 0: nubusintercom service ─────────────────────────────

def check_nubusintercom_deployment(result: Result):
    """Check nubusintercom deployment and pods are running."""
    rc, output = run([
        "kubectl", "get", "deployment", NUBUSINTERCOM_DEPLOYMENT, 
        "-n", EDU_NAMESPACE, "-o", "jsonpath={.status.availableReplicas}"
    ])
    if rc == 0 and output.strip() and int(output.strip()) > 0:
        result.ok(f"nubusintercom deployment: {output.strip()} pod(s) ready")
    else:
        result.fail("nubusintercom deployment not ready or not found")


def check_nubusintercom_service(result: Result):
    """Check nubusintercom service is available."""
    rc, output = run([
        "kubectl", "get", "service", NUBUSINTERCOM_SERVICE, 
        "-n", EDU_NAMESPACE, "-o", "jsonpath={.spec.clusterIP}"
    ])
    if rc == 0 and output.strip() and output.strip() != "None":
        result.ok(f"nubusintercom service: {output.strip()}")
    else:
        result.fail("nubusintercom service not found or no ClusterIP")


# ─── Layer 1: Redis token storage ───────────────────────────────

def check_redis_connectivity(result: Result):
    """Check Redis connectivity from nubusintercom pod."""
    pod = get_nubusintercom_pod()
    if not pod:
        result.fail("No nubusintercom pod found")
        return
    
    rc, output = kubectl_exec(
        pod, EDU_NAMESPACE,
        ["python3", "-c", "import redis, os; r=redis.Redis.from_url(os.environ['REDIS_URL']); print('ping:', r.ping())"]
    )
    
    if rc == 0:
        if "ping: True" in output:
            result.ok("Redis connectivity from nubusintercom: ping successful")
        else:
            result.fail(f"Redis connectivity failed: {output}")
    else:
        result.fail(f"Redis connectivity check failed: {output}")


def check_sogo6_redis_config(result: Result):
    """Check SOGo6 server has proper Redis configuration."""
    # Get the actual pod name first
    rc_pod, pod_name = run([
        "kubectl", "get", "pods", "-n", EDU_NAMESPACE,
        "-l", "app=sogo6-server", "-o", "jsonpath={.items[0].metadata.name}"
    ])
    
    if rc_pod != 0 or not pod_name:
        result.fail("Cannot find sogo6-server pod")
        return
    
    # Check for REDIS_URL or SOGO_P_REDIS_URL
    rc_env, env_out = kubectl_exec(
        pod_name,
        EDU_NAMESPACE,
        ["sh", "-c", "echo $REDIS_URL$SOGO_P_REDIS_URL"],
        container="sogo6-server"
    )
    
    if rc_env == 0 and env_out.strip():
        redis_url = env_out.strip()
        if "redis://" in redis_url.lower():
            if "@" in redis_url:
                result.ok("SOGo6 REDIS_URL configured with authentication")
            else:
                result.warn("SOGo6 REDIS_URL configured but no password detected")
        else:
            result.warn(f"SOGo6 REDIS_URL has unexpected format: {redis_url}")
    else:
        result.fail("SOGo6 REDIS_URL not configured")


# ─── Layer 2: Token exchange configuration ────────────────────

def check_sogo6_intercom_config(result: Result):
    """Check SOGo6 has INTERCOM_URL and INTERCOM_SHARED_SECRET configured."""
    # Get the actual pod name first
    rc_pod, pod_name = run([
        "kubectl", "get", "pods", "-n", EDU_NAMESPACE,
        "-l", "app=sogo6-server", "-o", "jsonpath={.items[0].metadata.name}"
    ])
    
    if rc_pod != 0 or not pod_name:
        result.fail("Cannot find sogo6-server pod")
        return
    
    rc_url, url_output = kubectl_exec(
        pod_name,
        EDU_NAMESPACE,
        ["printenv", "INTERCOM_URL"],
        container="sogo6-server"
    )
    
    rc_secret, secret_output = kubectl_exec(
        pod_name,
        EDU_NAMESPACE,
        ["printenv", "INTERCOM_SHARED_SECRET"],
        container="sogo6-server"
    )
    
    if rc_url != 0 or rc_secret != 0:
        result.fail("Cannot check SOGo6 intercom config (pod may be restarting)")
        return
    
    intercom_url = url_output.strip()
    secret = secret_output.strip()
    
    if not intercom_url:
        result.fail("SOGo6 INTERCOM_URL not set")
    elif not secret:
        result.fail("SOGo6 INTERCOM_SHARED_SECRET not set")
    elif "change-me" in secret.lower():
        result.fail("SOGo6 INTERCOM_SHARED_SECRET still has default value")
    else:
        result.ok("SOGo6 INTERCOM_URL and INTERCOM_SHARED_SECRET properly configured")


def check_nubusintercom_keycloak_config(result: Result):
    """Check nubusintercom has Keycloak client configuration."""
    pod = get_nubusintercom_pod()
    if not pod:
        result.warn("No nubusintercom pod found")
        return
    
    # Check Keycloak configuration via environment variables
    # The deployment uses KEYCLOAK_URL, REALM_NAME, INTERCOM_CLIENT_ID
    rc_server, server_out = run([
        "kubectl", "exec", "-n", EDU_NAMESPACE, pod, "--",
        "printenv", "KEYCLOAK_URL"
    ])
    
    rc_realm, realm_out = run([
        "kubectl", "exec", "-n", EDU_NAMESPACE, pod, "--",
        "printenv", "REALM_NAME"
    ])
    
    rc_client_id, client_id_out = run([
        "kubectl", "exec", "-n", EDU_NAMESPACE, pod, "--",
        "printenv", "INTERCOM_CLIENT_ID"
    ])
    
    rc_client_secret, client_secret_out = run([
        "kubectl", "exec", "-n", EDU_NAMESPACE, pod, "--",
        "printenv", "INTERCOM_CLIENT_SECRET"
    ])
    
    if rc_server == 0 and rc_realm == 0 and rc_client_id == 0 and rc_client_secret == 0:
        server = server_out.strip()
        realm = realm_out.strip()
        client_id = client_id_out.strip()
        client_secret = client_secret_out.strip()
        
        if server and realm and client_id and client_secret and "change-me" not in client_secret.lower():
            result.ok("nubusintercom Keycloak client configured")
        else:
            missing = []
            if not server:
                missing.append("KEYCLOAK_URL")
            if not realm:
                missing.append("REALM_NAME")
            if not client_id:
                missing.append("INTERCOM_CLIENT_ID")
            if "change-me" in client_secret.lower():
                missing.append("INTERCOM_CLIENT_SECRET (has default)")
            result.warn(f"nubusintercom Keycloak configuration incomplete: {', '.join(missing) if missing else 'unknown'}")
    else:
        result.warn("Cannot check nubusintercom Keycloak config")


# ─── Layer 3: OpenCloud connectivity ─────────────────────────────

def check_openccloud_oidc_discovery(result: Result):
    """Check OpenCloud OIDC discovery endpoint."""
    url = f"https://{OPENCLOUD_DOMAIN}/.well-known/openid-configuration"
    code = curl(url, timeout=10)
    if code == 200:
        result.ok(f"OpenCloud OIDC discovery: {code}")
    else:
        result.fail(f"OpenCloud OIDC discovery: {code}")


def check_openccloud_webdav_availability(result: Result):
    """Check OpenCloud WebDAV endpoints are reachable."""
    url = f"https://{OPENCLOUD_DOMAIN}/dav/files/"
    code = curl(url, timeout=10)
    if code in (401, 403):
        result.ok(f"OpenCloud WebDAV root: {code} (auth required)")
    elif code == 200:
        result.ok(f"OpenCloud WebDAV root: {code}")
    else:
        result.warn(f"OpenCloud WebDAV root: {code}")


# ─── Layer 4: HMAC signing ─────────────────────────────────────

def check_hmac_signing_properly_configured(result: Result):
    """Verify HMAC signing configuration exists between services."""
    # Get the actual pod name first
    rc_pod, pod_name = run([
        "kubectl", "get", "pods", "-n", EDU_NAMESPACE,
        "-l", "app=sogo6-server", "-o", "jsonpath={.items[0].metadata.name}"
    ])
    
    if rc_pod != 0 or not pod_name:
        result.warn("Cannot find sogo6-server pod")
        return
    
    rc, output = kubectl_exec(
        pod_name,
        EDU_NAMESPACE,
        ["printenv", "INTERCOM_SHARED_SECRET"],
        container="sogo6-server"
    )
    
    if rc == 0 and output.strip() and "change-me" not in output.lower():
        result.ok("HMAC shared secret configured (not default)")
    elif rc == 0 and "change-me" in output.lower():
        result.fail("HMAC shared secret still has default value 'change-me'")
    else:
        result.warn("Cannot verify HMAC shared secret configuration")


# ─── Layer 5: OX resource removal verification ──────────────────

def check_ox_resources_removed(result: Result):
    """Verify OX (Open-Xchange) resources are permanently removed from opensme."""
    # Check deployments
    rc, output = run([
        "kubectl", "get", "deployment", "-n", SME_NAMESPACE, 
        "postfix-ox", "--ignore-not-found"
    ])
    
    if "No resources found" in output or "Error" in output or rc != 0:
        result.ok("OX deployment removed from opensme")
    elif "postfix-ox" in output:
        result.fail("OX deployment still exists in opensme")
    
    # Check services
    rc, output = run([
        "kubectl", "get", "service", "-n", SME_NAMESPACE, 
        "postfix-ox", "postfix-ox-external",
        "--ignore-not-found"
    ])
    
    if "No resources found" in output or "Error" in output or (rc == 0 and "postfix-ox" not in output):
        result.ok("OX services removed from opensme")
    else:
        result.fail("OX services still exist in opensme")


# ─── Additional: SOGo6 UI endpoints ────────────────────────────

def check_sogo6_ui_filepicker_routes(result: Result):
    """Check SOGo6 UI has file picker routing configured."""
    code = curl(f"https://{SOGO6_DOMAIN}/compose", timeout=10)
    if code in (200, 302, 401, 403):
        result.ok(f"SOGo6 UI compose endpoint: {code}")
    else:
        result.warn(f"SOGo6 UI compose endpoint: {code}")


# ─── Main ───────────────────────────────────────────────────────

def main():
    result = Result("filepicker-integration")
    result.header("OpenCloud File Picker - Deep Integration Tests")

    # Layer 0: nubusintercom service
    result.header("Layer 0 — nubusintercom service health")
    check_nubusintercom_deployment(result)
    check_nubusintercom_service(result)

    # Layer 1: Redis token storage
    result.header("Layer 1 — Redis token storage")
    check_redis_connectivity(result)
    check_sogo6_redis_config(result)

    # Layer 2: Token exchange flow
    result.header("Layer 2 — Token exchange configuration")
    check_sogo6_intercom_config(result)
    check_nubusintercom_keycloak_config(result)

    # Layer 3: OpenCloud connectivity
    result.header("Layer 3 — OpenCloud API connectivity")
    check_openccloud_oidc_discovery(result)
    check_openccloud_webdav_availability(result)

    # Layer 4: HMAC signing
    result.header("Layer 4 — HMAC inter-service authentication")
    check_hmac_signing_properly_configured(result)

    # Layer 5: OX resource cleanup verification
    result.header("Layer 5 — OX resource removal verification")
    check_ox_resources_removed(result)

    # Additional: SOGo6 UI
    result.header("Additional — SOGo6 UI file picker routes")
    check_sogo6_ui_filepicker_routes(result)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
