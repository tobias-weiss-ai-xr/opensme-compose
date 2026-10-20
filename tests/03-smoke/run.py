#!/usr/bin/env python3
"""
tests/03-smoke/run.py — HTTP smoke tests.

Checks that all expected endpoints respond (200, 301, 302, 307, 401, 403).
Requires a running stack. Uses curl for HTTP checks.

Usage:
    python3 tests/03-smoke/run.py [domain]

    domain defaults to localhost (assumes ports published on host)

Exit codes:
    0 = all endpoints respond
    1 = endpoint failures
"""

import sys
import subprocess
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT


# Endpoints to check (path, expected_status_codes, description)
SMOKE_ENDPOINTS = [
    # Core
    ("/", [200, 301, 302, 307], "Portal landing page"),
    # IDM
    ("/healthz", [200, 404], "Zitadel health (if running)"),
    # OpenCloud
    ("/healthz", [200, 404], "OpenCloud health"),
    # Traefik (if standalone)
    ("/dashboard/", [200, 301, 302, 401, 403], "Traefik dashboard"),
]

# Service-specific endpoints (checked if the service is running)
SERVICE_ENDPOINTS = {
    "portal": {"path": "/", "port": 8080, "codes": [200], "name": "Portal"},
    "postgres": {"check": "pg_isready", "name": "PostgreSQL"},
    "redis": {"check": "redis-cli ping", "name": "Redis"},
    "memcached": {"check": "memcached-tool localhost:11211 stats", "name": "Memcached"},
}


def curl_endpoint(url: str, timeout: int = 10) -> tuple[int, str]:
    """Curl an endpoint and return (status_code, error_msg)."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout), "-k", "-L", url],
            capture_output=True, text=True, timeout=timeout + 5
        )
        code = result.stdout.strip()
        if code.isdigit():
            return int(code), ""
        return 0, f"curl returned: {code}"
    except subprocess.TimeoutExpired:
        return 0, "timeout"
    except FileNotFoundError:
        return 0, "curl not found"
    except Exception as e:
        return 0, str(e)


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
        return False, str(e)


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

    domain = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    use_https = domain != "localhost"
    protocol = "https" if use_https else "http"

    # Check if stack is running
    containers = docker_ps()
    if not containers:
        result.fail("No containers running. Start the stack first: make up PROFILE=soho")
        return result.summary()

    result.info(f"Found {len(containers)} running containers: {', '.join(containers)}")

    # Check HTTP endpoints
    for path, codes, desc in SMOKE_ENDPOINTS:
        url = f"{protocol}://{domain}{path}"
        status, err = curl_endpoint(url)
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
            status, err = curl_endpoint(url)
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
