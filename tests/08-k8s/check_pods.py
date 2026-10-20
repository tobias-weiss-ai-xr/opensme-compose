#!/usr/bin/env python3
"""
tests/08-k8s/check_pods.py — Pod health across opensme namespaces.

Verifies that all pods in the opensme namespaces are Running (or Succeeded
for Jobs/CronJobs). Reports CrashLoopBackOff, Pending, Error, and high
restart counts.

Usage:
    python3 tests/08-k8s/check_pods.py

Exit codes:
    0 = all pods healthy
    1 = one or more pods are not Running
"""

import subprocess
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

# Namespaces to check (opensme excluded — known degraded)
NAMESPACES = [
    "opensme",
    "opensme-edu",
    "opensme-staff",
    "opensme-students",
    "home",
]

# Restart threshold: warn if a pod has more than this many restarts
RESTART_WARN_THRESHOLD = 5
RESTART_FAIL_THRESHOLD = 20


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


def main():
    result = Result("k8s-pods")
    result.header("Layer 8: Pod health")

    for ns in NAMESPACES:
        result.header(f"Namespace: {ns}")

        pods = kubectl_json(["-n", ns, "get", "pods"])
        if pods is None:
            result.fail(f"Cannot list pods in {ns}")
            continue

        items = pods.get("items", [])
        if not items:
            result.warn(f"No pods in {ns}")
            continue

        for pod in items:
            name = pod["metadata"]["name"]
            phase = pod.get("status", {}).get("phase", "Unknown")
            container_statuses = pod.get("status", {}).get("containerStatuses", [])

            # Determine the "worst" container status
            restarts = 0
            waiting_reason = None
            for cs in container_statuses:
                restarts += cs.get("restartCount", 0)
                if "waiting" in cs.get("state", {}):
                    waiting_reason = cs["state"]["waiting"].get("reason", "Unknown")

            # Classify pod health
            if phase == "Succeeded":
                result.ok(f"{name}: Succeeded (Job/CronJob)")
            elif phase == "Running" and not waiting_reason:
                if restarts >= RESTART_FAIL_THRESHOLD:
                    result.fail(f"{name}: Running but {restarts} restarts ( CrashLoopBackOff risk)")
                elif restarts >= RESTART_WARN_THRESHOLD:
                    result.warn(f"{name}: Running but {restarts} restarts")
                else:
                    result.ok(f"{name}: Running ({restarts} restarts)")
            elif phase == "Pending":
                result.fail(f"{name}: Pending ({waiting_reason or 'unknown reason'})")
            elif phase == "Failed":
                result.fail(f"{name}: Failed")
            elif phase == "Running" and waiting_reason:
                result.fail(f"{name}: Running but container waiting ({waiting_reason})")
            else:
                result.fail(f"{name}: phase={phase}, waiting={waiting_reason}")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
