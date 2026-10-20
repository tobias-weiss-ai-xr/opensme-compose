#!/usr/bin/env python3
"""
tests/08-k8s/check_deployments.py — Deployment + StatefulSet readiness.

Verifies that all Deployments and StatefulSets in the opensme namespaces
have their desired replica count met (readyReplicas == replicas).

Usage:
    python3 tests/08-k8s/check_deployments.py

Exit codes:
    0 = all workloads ready
    1 = one or more workloads not ready
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


def check_workloads(result: Result, ns: str, kind: str):
    """Check Deployments or StatefulSets in a namespace."""
    plural = "deployments" if kind == "Deployment" else "statefulsets"
    items = kubectl_json(["-n", ns, "get", plural])
    if items is None:
        result.fail(f"Cannot list {plural} in {ns}")
        return

    for item in items.get("items", []):
        name = item["metadata"]["name"]
        spec_replicas = item.get("spec", {}).get("replicas", 1)
        status = item.get("status", {})

        if kind == "Deployment":
            ready = status.get("readyReplicas", 0)
            updated = status.get("updatedReplicas", 0)
            _available = status.get("availableReplicas", 0)
        else:  # StatefulSet
            ready = status.get("readyReplicas", 0)
            updated = status.get("updatedReplicas", 0)
            _available = ready  # StatefulSets don't have availableReplicas

        if ready == spec_replicas and updated == spec_replicas:
            result.ok(f"{ns}/{name}: {ready}/{spec_replicas} ready")
        elif ready < spec_replicas:
            result.fail(f"{ns}/{name}: {ready}/{spec_replicas} ready (not enough replicas)")
        else:
            result.warn(f"{ns}/{name}: {ready}/{spec_replicas} ready (more than desired?)")


def main():
    result = Result("k8s-deployments")
    result.header("Layer 8: Deployment & StatefulSet readiness")

    for ns in NAMESPACES:
        result.header(f"Namespace: {ns}")
        check_workloads(result, ns, "Deployment")
        check_workloads(result, ns, "StatefulSet")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
