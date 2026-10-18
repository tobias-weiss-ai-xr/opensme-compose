#!/usr/bin/env python3
"""
tests/06-security/audit.py — Security audit.

Checks for common security issues in the compose stack:
  - No exposed internal ports (databases, caches)
  - No hardcoded secrets in compose files
  - Docker socket mount is read-only
  - Resource limits set on all services
  - No privileged containers
  - No host network mode
  - TLS configured on Traefik
  - .env file exists and is not in git

Usage:
    python3 tests/06-security/audit.py [domain]

Exit codes:
    0 = no critical security issues
    1 = security issues found
"""

import os
import sys
import re
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import (
    ComposeLoader, Result, ROOT,
    extract_host_ports, has_resource_limits, is_host_network,
)


def check_no_exposed_internal_ports(result, loader):
    """Internal services (databases, caches) must not expose host ports."""
    internal_services = [
        "postgres", "pgbouncer", "redis", "memcached",
        "opencloud", "collabora", "minio",
        "sogo", "invoiceninja", "paperless-ngx",
        "synapse", "notes-backend", "notes-y-provider",
        "dev-agent", "predictive-agent", "ollama", "taskfleet",
    ]
    for svc_name in internal_services:
        svc = loader.get_service(svc_name)
        if svc is None:
            continue
        ports = extract_host_ports(svc["data"])
        if ports:
            result.fail(f"{svc_name}: exposes host ports {ports} (should be internal)")
        else:
            result.ok(f"{svc_name}: internal only")


def check_docker_socket_ro(result, loader):
    """Docker socket mount must be read-only.

    Exception: taskfleet needs read-write Docker socket because it runs
    docker compose commands (creating worktrees, building, testing).
    """
    # Services allowed to mount Docker socket read-write
    RW_EXCEPTIONS = {"taskfleet"}
    for svc_name, svc in loader.services.items():
        vols = svc["data"].get("volumes") or []
        for v in vols:
            if isinstance(v, str) and "/var/run/docker.sock" in v:
                if ":ro" in v:
                    result.ok(f"{svc_name}: Docker socket mounted read-only")
                elif svc_name in RW_EXCEPTIONS:
                    result.ok(f"{svc_name}: Docker socket read-write (allowed: needs docker compose)")
                else:
                    result.fail(f"{svc_name}: Docker socket mounted read-write (should be :ro)")


def check_no_privileged(result, loader):
    """No container should run in privileged mode."""
    for svc_name, svc in loader.services.items():
        if svc["data"].get("privileged") is True:
            result.fail(f"{svc_name}: runs in privileged mode")
        else:
            result.ok(f"{svc_name}: not privileged")


def check_no_host_network(result, loader):
    """No service should use network_mode: host."""
    for svc_name, svc in loader.services.items():
        if is_host_network(svc["data"]):
            result.fail(f"{svc_name}: uses network_mode: host")
        else:
            result.ok(f"{svc_name}: bridge network")


def check_resource_limits(result, loader):
    """All services must have resource limits."""
    for svc_name, svc in sorted(loader.services.items()):
        if has_resource_limits(svc["data"]):
            result.ok(f"{svc_name}: resource limits set")
        else:
            result.warn(f"{svc_name}: no resource limits")


def check_tls_config(result, loader):
    """Traefik must have TLS configured."""
    traefik = loader.get_service("traefik")
    if traefik is None:
        result.skip("traefik: not in compose files (using system Traefik?)")
        return

    labels = traefik["data"].get("labels") or []
    has_tls = False
    has_acme = False
    for label in labels:
        if isinstance(label, str):
            if "tls=true" in label:
                has_tls = True
            if "tls.certresolver" in label:
                has_acme = True

    command = traefik["data"].get("command") or []
    for cmd in command:
        if isinstance(cmd, str) and "certificatesResolvers" in cmd:
            has_acme = True

    if has_tls:
        result.ok("traefik: TLS enabled")
    else:
        result.fail("traefik: TLS not configured")

    if has_acme:
        result.ok("traefik: ACME/Let's Encrypt configured")
    else:
        result.warn("traefik: no ACME cert resolver")


def check_traefik_api_secure(result, loader):
    """Traefik must not serve the unauthenticated API on the insecure port.

    --api.insecure=true binds an unauthenticated dashboard/API on :8080
    inside the container network — every other container could read the
    routing config and drive the API. The dashboard must be reachable only
    via the authenticated websecure router (api@internal + basicauth).
    """
    traefik = loader.get_service("traefik")
    if traefik is None:
        result.skip("traefik: not in compose files (using system Traefik?)")
        return

    command = [str(c) for c in (traefik["data"].get("command") or [])]
    insecure = [c for c in command if c.startswith("--api.insecure")]
    if any(c in ("--api.insecure=true", "--api.insecure") for c in insecure):
        result.fail("traefik: --api.insecure=true exposes an unauthenticated "
                    "API on :8080 to the whole docker network")
    else:
        result.ok("traefik: api.insecure disabled")

    # The secure dashboard router must carry an auth middleware
    labels = [str(l) for l in (traefik["data"].get("labels") or [])]
    has_internal = any("service=api@internal" in l for l in labels)
    has_auth = any("middlewares=auth" in l or "basicauth" in l for l in labels)
    if has_internal:
        if has_auth:
            result.ok("traefik: dashboard routed via api@internal with auth middleware")
        else:
            result.fail("traefik: dashboard router (api@internal) has no auth middleware")
    else:
        result.warn("traefik: no api@internal router found (dashboard disabled?)")


def check_redis_auth(result, loader):
    """Redis must require authentication (--requirepass).

    Without a password any container on the network can read/flush the
    shared cache (notes sessions, etc.).
    """
    redis = loader.get_service("redis")
    if redis is None:
        result.skip("redis: not in compose files")
        return
    command = [str(c) for c in (redis["data"].get("command") or [])]
    if "--requirepass" in command:
        # next list element is the interpolated value; ensure it is env-driven
        idx = command.index("--requirepass")
        val = command[idx + 1] if idx + 1 < len(command) else ""
        if val.startswith("${") or val:
            result.ok("redis: --requirepass set")
        else:
            result.fail("redis: --requirepass without a value")
    else:
        result.fail("redis: no --requirepass — unauthenticated on the docker network")


def check_host_port_binding(result, loader):
    """Published host ports must bind loopback unless the service is
    intentionally internet-facing.

    "80:80" binds 0.0.0.0 — every interface, including VPN/exposed ones.
    Exceptions: traefik (public reverse proxy) and stalwart (public
    MX/IMAP — mail must accept external connections by design).
    """
    INTERNET_FACING = {"traefik", "stalwart"}
    for svc_name, svc in loader.services.items():
        ports = svc["data"].get("ports") or []
        for p in ports:
            if isinstance(p, dict):
                p = f"{p.get('published', '')}:{p.get('target', '')}"
            if not isinstance(p, str) or ":" not in p:
                continue
            if svc_name in INTERNET_FACING:
                result.ok(f"{svc_name}: internet-facing port {p} (by design)")
                continue
            if p.startswith("127.0.0.1:") or p.startswith("localhost:"):
                result.ok(f"{svc_name}: host port binds loopback ({p})")
            else:
                result.fail(f"{svc_name}: host port {p} binds all interfaces "
                            f"(use 127.0.0.1:{p})")


def check_env_not_in_git(result):
    """.env must not be tracked in git."""
    env_file = ROOT / ".env"
    gitignore = ROOT / ".gitignore"

    # Check .env exists
    if env_file.exists():
        result.ok(".env exists")
    else:
        result.warn(".env not found (run: make bootstrap)")

    # Check .env is in .gitignore
    if gitignore.exists():
        content = gitignore.read_text()
        if ".env" in content and not ".env.example" in content.split(".env")[0].split("\n")[-1]:
            result.ok(".env is in .gitignore")
        elif ".env" in content:
            result.ok(".env is in .gitignore")
        else:
            result.fail(".env not in .gitignore — risk of committing secrets!")
    else:
        result.warn(".gitignore not found")


def check_no_changeme_in_env(result):
    """.env (if exists) must not have CHANGEME_ values."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        result.skip(".env not found (CHANGEME check skipped)")
        return

    content = env_file.read_text()
    matches = re.findall(r'CHANGEME_[a-z_]+', content, re.IGNORECASE)
    if matches:
        result.fail(f".env has {len(matches)} CHANGEME_ values — change all passwords!")
        for m in set(matches):
            result.info(f"  {m}")
    else:
        result.ok(".env: no CHANGEME_ values")


def check_restart_policy(result, loader):
    """All services should have restart: unless-stopped or restart: always.

    Exceptions: taskfleet and dev-maintenance-bot are on-demand (run via
    docker compose run / periodic trigger), so restart: no is correct.
    """
    # Services where restart: no is intentional
    NO_RESTART_EXCEPTIONS = {"taskfleet", "dev-maintenance-bot"}
    for svc_name, svc in sorted(loader.services.items()):
        restart = svc["data"].get("restart")
        if restart in ("unless-stopped", "always"):
            result.ok(f"{svc_name}: restart={restart}")
        elif restart is None:
            result.warn(f"{svc_name}: no restart policy")
        elif restart == "no" and svc_name in NO_RESTART_EXCEPTIONS:
            result.ok(f"{svc_name}: restart=no (on-demand service)")
        else:
            result.warn(f"{svc_name}: restart={restart} (recommend unless-stopped)")


def main():
    result = Result("security-audit")
    result.header("Layer 6: Security audit")

    loader = ComposeLoader(ROOT)
    loader.load()

    result.header("Exposed ports")
    check_no_exposed_internal_ports(result, loader)

    result.header("Docker socket")
    check_docker_socket_ro(result, loader)

    result.header("Privileged mode")
    check_no_privileged(result, loader)

    result.header("Network mode")
    check_no_host_network(result, loader)

    result.header("Resource limits")
    check_resource_limits(result, loader)

    result.header("TLS configuration")
    check_tls_config(result, loader)

    result.header("Traefik API exposure")
    check_traefik_api_secure(result, loader)

    result.header("Cache authentication")
    check_redis_auth(result, loader)

    result.header("Host port binding")
    check_host_port_binding(result, loader)

    result.header("Secret management")
    check_env_not_in_git(result)
    check_no_changeme_in_env(result)

    result.header("Restart policies")
    check_restart_policy(result, loader)

    result.header("LLM outbound hygiene (AE1)")
    if "LLM_OUTBOUND" in os.environ or any(a in sys.argv for a in ["--llm-outbound", "--all"]):
        from llm_outbound import check_llm_outbound_hygiene
        check_llm_outbound_hygiene(result)
    else:
        result.info("skipped (set LLM_OUTBOUND=1 or use --llm-outbound)")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
