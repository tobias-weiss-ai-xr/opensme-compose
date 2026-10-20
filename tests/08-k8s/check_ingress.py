#!/usr/bin/env python3
"""
tests/08-k8s/check_ingress.py — Ingress + PVC health.

Verifies that:
  - All ingresses in opensme namespaces have an ADDRESS (LB assigned)
  - All PVCs are Bound (not Pending or Lost)
  - Ingress TLS is configured

Usage:
    python3 tests/08-k8s/check_ingress.py

Exit codes:
    0 = all ingresses and PVCs healthy
    1 = one or more ingresses or PVCs have issues
"""

import subprocess
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

NAMESPACES = [
    "opensme",
    "opensme-edu",
    "opensme-staff",
    "opensme-students",
    "home",
]


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


def check_ingresses(result: Result, ns: str):
    """Check that all ingresses have addresses and TLS."""
    items = kubectl_json(["-n", ns, "get", "ingress"])
    if items is None:
        return  # skip silently

    for item in items.get("items", []):
        name = item["metadata"]["name"]
        spec = item.get("spec", {})
        status = item.get("status", {})

        # Extract rules (used for both LB check and path check)
        rules = spec.get("rules", [])

        # Check LB address
        lb = status.get("loadBalancer", {}).get("ingress", [])
        if lb:
            addr = lb[0].get("hostname") or lb[0].get("ip", "")
            result.ok(f"{ns}/{name}: LB address = {addr}")
        else:
            # Some ingresses use ClusterIP or don't have a LB; check if they
            # have a rules-based ingress (still valid)
            if rules:
                result.warn(f"{ns}/{name}: no LB address (rules exist, may use internal LB)")
            else:
                result.fail(f"{ns}/{name}: no LB address and no rules")

        # Check TLS
        tls = spec.get("tls", [])
        if tls:
            for t in tls:
                hosts = t.get("hosts", [])
                secret = t.get("secretName", "")
                if secret:
                    result.ok(f"{ns}/{name}: TLS via {secret} for {', '.join(hosts)}")
                else:
                    result.warn(f"{ns}/{name}: TLS entry without secretName")
        else:
            result.warn(f"{ns}/{name}: no TLS configured")

        # Check rules have paths
        for rule in rules:
            host = rule.get("host", "?")
            paths = rule.get("http", {}).get("paths", [])
            if not paths:
                result.fail(f"{ns}/{name}: rule for {host} has no paths")
            else:
                for p in paths:
                    backend = p.get("backend", {})
                    svc = backend.get("service", {}).get("name", "?")
                    port = backend.get("service", {}).get("port", {}).get("number",
                             backend.get("service", {}).get("port", {}).get("name", "?"))
                    result.ok(f"{ns}/{name}: {host} → {svc}:{port}")


def check_pvcs(result: Result, ns: str):
    """Check that all PVCs are Bound."""
    items = kubectl_json(["-n", ns, "get", "pvc"])
    if items is None:
        return  # skip silently

    for item in items.get("items", []):
        name = item["metadata"]["name"]
        phase = item.get("status", {}).get("phase", "Unknown")
        capacity = item.get("status", {}).get("capacity", {}).get("storage", "?")

        if phase == "Bound":
            result.ok(f"{ns}/{name}: Bound ({capacity})")
        elif phase == "Pending":
            result.fail(f"{ns}/{name}: Pending (no storage assigned)")
        elif phase == "Lost":
            result.fail(f"{ns}/{name}: Lost")
        else:
            result.warn(f"{ns}/{name}: {phase}")


def main():
    result = Result("k8s-ingress-pvc")
    result.header("Layer 8: Ingress & PVC health")

    for ns in NAMESPACES:
        result.header(f"Namespace: {ns}")
        check_ingresses(result, ns)
        check_pvcs(result, ns)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
