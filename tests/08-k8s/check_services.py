#!/usr/bin/env python3
"""
tests/08-k8s/check_services.py — Service endpoint health.

Verifies that:
  - All Services have endpoints (backing pods exist)
  - ClusterIP services have a valid IP
  - LoadBalancer services have an external IP

Usage:
    python3 tests/08-k8s/check_services.py

Exit codes:
    0 = all services have endpoints
    1 = one or more services have no endpoints
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

# Services that are headless (ClusterIP: None) — they have no endpoints
# unless pods exist; skip endpoint check for these
HEADLESS_SKIP = set()


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


def check_services(result: Result, ns: str):
    """Check services in a namespace."""
    items = kubectl_json(["-n", ns, "get", "svc"])
    if items is None:
        return

    for item in items.get("items", []):
        name = item["metadata"]["name"]
        spec = item.get("spec", {})
        svc_type = spec.get("type", "ClusterIP")
        cluster_ip = spec.get("clusterIP", "")

        # Check cluster IP
        if cluster_ip == "None":
            # Headless service — check endpoints directly
            pass
        elif cluster_ip:
            result.ok(f"{ns}/{name}: {svc_type} clusterIP={cluster_ip}")
        else:
            result.fail(f"{ns}/{name}: no clusterIP")

        # Check LB external IP
        if svc_type == "LoadBalancer":
            lb = item.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            if lb:
                ext = lb[0].get("hostname") or lb[0].get("ip", "")
                result.ok(f"{ns}/{name}: LB external = {ext}")
            else:
                result.warn(f"{ns}/{name}: LoadBalancer has no external IP (may be pending)")

        # Check endpoints
        endpoints = kubectl_json(["-n", ns, "get", "endpoints", name])
        if endpoints is not None:
            subsets = endpoints.get("subsets", [])
            if subsets:
                total_addrs = sum(len(s.get("addresses", [])) + len(s.get("notReadyAddresses", []))
                                   for s in subsets)
                if total_addrs > 0:
                    result.ok(f"{ns}/{name}: {total_addrs} endpoint(s)")
                else:
                    result.fail(f"{ns}/{name}: no endpoint addresses")
            else:
                # Headless services without endpoints are OK if no pods
                if cluster_ip == "None":
                    result.warn(f"{ns}/{name}: headless service with no endpoints")
                else:
                    result.fail(f"{ns}/{name}: no endpoints")


def main():
    result = Result("k8s-services")
    result.header("Layer 8: Service endpoints")

    for ns in NAMESPACES:
        result.header(f"Namespace: {ns}")
        check_services(result, ns)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
