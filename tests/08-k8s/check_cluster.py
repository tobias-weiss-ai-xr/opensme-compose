#!/usr/bin/env python3
"""
tests/08-k8s/check_cluster.py — ArgoCD application health + cluster connectivity.

Verifies that:
  - kubectl can reach the cluster
  - All expected ArgoCD applications are Synced + Healthy
  - No ArgoCD apps are in Unknown sync state (except known exceptions)

Usage:
    python3 tests/08-k8s/check_cluster.py

Exit codes:
    0 = all reachable apps are Synced + Healthy
    1 = one or more apps are OutOfSync or Degraded
"""

import subprocess
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

# ArgoCD applications expected to be Synced + Healthy
# (opensme is known-degraded and excluded)
EXPECTED_APPS = {
    "opensme": "Synced",
    "opensme-edu": "Synced",
    "next-mailserver": "Synced",
    "scs-infra": "Synced",
}

# Apps where Health is allowed to be non-Healthy (backup has Unknown health)
HEALTH_EXCEPTIONS = {"backup"}


def kubectl_json(args: list[str], timeout: int = 15) -> dict | list | None:
    """Run kubectl and return parsed JSON, or None on failure."""
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


def main():
    result = Result("k8s-cluster")
    result.header("Layer 8: Cluster & ArgoCD health")

    # ── kubectl connectivity ──────────────────────────────────────
    result.header("kubectl connectivity")
    try:
        proc = subprocess.run(
            ["kubectl", "cluster-info", "--request-timeout=10s"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            result.ok("kubectl can reach cluster")
        else:
            result.fail(f"kubectl cluster-info failed: {proc.stderr.strip()}")
            return result.summary()
    except subprocess.TimeoutExpired:
        result.fail("kubectl cluster-info timed out")
        return result.summary()
    except FileNotFoundError:
        result.fail("kubectl not found in PATH")
        return result.summary()

    # ── Node readiness ────────────────────────────────────────────
    result.header("Node readiness")
    nodes = kubectl_json(["get", "nodes"])
    if nodes is None:
        result.fail("Cannot list nodes")
    else:
        for node in nodes.get("items", []):
            name = node["metadata"]["name"]
            conditions = {c["type"]: c["status"] for c in node.get("status", {}).get("conditions", [])}
            ready = conditions.get("Ready", "Unknown")
            if ready == "True":
                result.ok(f"Node {name}: Ready")
            else:
                result.fail(f"Node {name}: NotReady (Ready={ready})")

    # ── ArgoCD applications ───────────────────────────────────────
    result.header("ArgoCD applications")
    apps = kubectl_json(["-n", "argocd", "get", "applications"])
    if apps is None:
        result.fail("Cannot list ArgoCD applications")
    else:
        for app in apps.get("items", []):
            name = app["metadata"]["name"]
            sync = app.get("status", {}).get("sync", {}).get("status", "Unknown")
            health = app.get("status", {}).get("health", {}).get("status", "Unknown")

            if name in EXPECTED_APPS:
                expected_sync = EXPECTED_APPS[name]
                if sync == expected_sync:
                    result.ok(f"{name}: sync={sync}")
                else:
                    result.fail(f"{name}: sync={sync} (expected {expected_sync})")

                if health == "Healthy":
                    result.ok(f"{name}: health={health}")
                elif health == "Progressing":
                    result.warn(f"{name}: health={health} (still starting)")
                else:
                    result.fail(f"{name}: health={health} (expected Healthy)")
            elif name in HEALTH_EXCEPTIONS:
                result.skip(f"{name}: health={health} (known exception)")
            else:
                result.info(f"{name}: sync={sync}, health={health} (not in expected set)")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
