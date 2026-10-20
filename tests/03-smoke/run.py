#!/usr/bin/env python3
"""
tests/03-smoke/run.py — HTTP smoke tests.

Checks that all expected endpoints respond (200, 301, 302, 307, 401, 403).
Requires a running stack. HTTP probes use requests with the same in-process
DNS fallback the e2e layers use, so *.local demo names resolve without
/etc/hosts edits; service liveness is probed inside the containers with the
same commands the compose healthchecks use.

Usage:
    python3 tests/03-smoke/run.py [domain]

    domain defaults to localhost (assumes ports published on host)

Exit codes:
    0 = all endpoints respond
    1 = endpoint failures
"""

import os
import re
import socket
import subprocess
import sys
import json
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Same local-suffix contract as the e2e layers' install_dns_fallback().
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")


def install_dns_fallback() -> None:
    """Map hostnames that fail real DNS resolution to loopback.

    Local demo deployments route *.local hosts through Traefik on :80/:443;
    without an /etc/hosts entry those names don't resolve. Names that DO
    resolve are untouched, so this is inert against public domains.
    """
    original = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        try:
            return original(host, *args, **kwargs)
        except socket.gaierror:
            if (
                isinstance(host, str)
                and re.fullmatch(r"[a-zA-Z0-9.-]+", host)
                and any(host.endswith(s) for s in LOCALISH_SUFFIXES)
            ):
                return original("127.0.0.1", *args, **kwargs)
            raise

    socket.getaddrinfo = patched


# Service-specific endpoints (checked if the service is running).
# The in-container checks mirror the services' own compose healthchecks —
# a smoke check must not demand tools the image does not ship (the alpine
# memcached image has no memcached-tool; its health contract is nc -z).
SERVICE_ENDPOINTS = {
    "portal": {"path": "/", "port": 8080, "codes": [200], "name": "Portal"},
    "postgres": {"check": "pg_isready", "name": "PostgreSQL"},
    "redis": {"check": "redis-cli ping", "name": "Redis"},
    "memcached": {"check": "nc -z 127.0.0.1 11211", "name": "Memcached"},
}


def load_dotenv() -> dict[str, str]:
    """ROOT/.env as a dict; process env still wins (same as the e2e layer)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return {}
    out = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


_ENV = load_dotenv()


def cfg(key: str, default: str = "") -> str:
    return os.environ.get(key) or _ENV.get(key) or default


def endpoint_checks(domain: str) -> list[tuple[str, list[int], str]]:
    """(url, expected codes, description) — one probe per service HOST.

    Host names follow the e2e layer's rules (PORTAL_DOMAIN env else
    portal.<domain>, …). The previous table probed everything on the main
    domain — where the portal and the dashboard are NOT routed — so those
    checks could only pass by tolerating 404, or never resolved at all on
    hosts without /etc/hosts entries. Health endpoints are now expected to
    answer 200: a profile that does not deploy the service should fail
    loudly here, not excuse itself.
    """
    portal = cfg("PORTAL_DOMAIN", f"portal.{domain}")
    zitadel = cfg("ZITADEL_DOMAIN", f"auth.{domain}")
    cloud = cfg("OPENCLOUD_DOMAIN", f"cloud.{domain}")
    schema = "https" if domain != "localhost" else "http"
    s = f"{schema}://"
    return [
        (f"{s}{portal}/", [200, 301, 302, 307], "Portal landing page"),
        (f"{s}{zitadel}/healthz", [200], "Zitadel health"),
        (f"{s}{cloud}/healthz", [200], "OpenCloud health"),
        # api.insecure is disabled; the dashboard is served ONLY through the
        # authenticated api@internal router on traefik.<domain>. Without
        # credentials the auth middleware must answer 401 (403 if the
        # deployment narrows it further, 200 if it allows anonymous).
        (f"{s}traefik.{domain}/dashboard/", [200, 401, 403], "Traefik dashboard"),
    ]


def http_status(url: str, timeout: int = 10) -> tuple[int, str]:
    """GET the URL and return (status_code, error_msg); 0 = no HTTP answer.

    verify=False is the smoke equivalent of `curl -k`: demo stacks present
    the self-signed edge certificate. Redirects are followed, like `-L`, so
    a 301-to-200 landing page reports the final 200.
    """
    try:
        r = requests.get(url, timeout=timeout, verify=False, allow_redirects=True)
        return r.status_code, ""
    except requests.RequestException as e:
        return 0, f"{type(e).__name__}: {e}"


def docker_exec(service: str, command: str) -> tuple[bool, str]:
    """Run a command inside a Docker container."""
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", service, "sh", "-c", command],
            capture_output=True, text=True, timeout=15,
            cwd=str(ROOT)
        )
        return result.returncode == 0, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except FileNotFoundError:
        return False, "docker not found"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def docker_ps() -> list[str]:
    """Get list of running container names."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "{{.Name}}"],
            capture_output=True, text=True, timeout=10,
            cwd=str(ROOT)
        )
        return [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]
    except Exception:
        return []


def main():
    result = Result("smoke-tests")
    result.header("Layer 3: Smoke tests")

    install_dns_fallback()

    domain = sys.argv[1] if len(sys.argv) > 1 else "localhost"

    # Check if stack is running
    containers = docker_ps()
    if not containers:
        result.fail("No containers running. Start the stack first: make up PROFILE=soho")
        return result.summary()

    result.info(f"Found {len(containers)} running containers: {', '.join(containers)}")

    # Check HTTP endpoints — one probe per service host
    for url, codes, desc in endpoint_checks(domain):
        status, err = http_status(url)
        if status == 0:
            result.fail(f"{desc} ({url}): {err}")
        elif status in codes:
            result.ok(f"{desc} ({url}): {status}")
        else:
            result.fail(f"{desc} ({url}): got {status}, expected {codes}")

    # Check service health via docker exec
    for svc_name, check in SERVICE_ENDPOINTS.items():
        if "check" in check:
            ok, msg = docker_exec(svc_name, check["check"])
            if ok:
                result.ok(f"{check['name']}: healthy")
            else:
                result.fail(f"{check['name']}: unhealthy ({msg})")
        elif "path" in check:
            port = check.get("port", 80)
            url = f"http://{domain}:{port}{check['path']}"
            status, err = http_status(url)
            if status in check["codes"]:
                result.ok(f"{check['name']} ({url}): {status}")
            elif status == 0:
                result.fail(f"{check['name']} ({url}): {err}")
            else:
                result.fail(f"{check['name']} ({url}): got {status}, expected {check['codes']}")

    # Check container health status
    try:
        result_exec = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True, text=True, timeout=10,
            cwd=str(ROOT)
        )
        if result_exec.stdout.strip():
            for line in result_exec.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    c = json.loads(line)
                    name = c.get("Name", c.get("name", "?"))
                    _health = c.get("Health", c.get("health", "unknown"))
                    status = c.get("Status", c.get("status", ""))
                    if "healthy" in status.lower():
                        result.ok(f"{name}: healthy")
                    elif "unhealthy" in status.lower():
                        result.fail(f"{name}: unhealthy")
                    elif "starting" in status.lower():
                        result.skip(f"{name}: starting")
                    else:
                        result.skip(f"{name}: {status}")
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
