#!/usr/bin/env python3
"""
tests/08-k8s/run.py — Layer 8: Kubernetes deployment health.

Runs all k8s check scripts in sequence:
  - check_cluster:    ArgoCD apps Synced + Healthy, node readiness
  - check_pods:       All pods Running, no CrashLoopBackOff
  - check_deployments: Deployments + StatefulSets ready
  - check_images:     No stale registry.opencode.de references
  - check_ingress:    Ingresses have LB addresses + TLS, PVCs Bound
  - check_services:   Services have endpoints
  - check_keycloak:   Keycloak + OIDC discovery + OAuth2 Proxy

Usage:
    python3 tests/08-k8s/run.py

Exit codes:
    0 = all k8s checks passed
    1 = one or more checks failed
"""

import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

K8S_TESTS = [
    ("Cluster & ArgoCD", "tests/08-k8s/check_cluster.py"),
    ("Pod health", "tests/08-k8s/check_pods.py"),
    ("Deployment readiness", "tests/08-k8s/check_deployments.py"),
    ("Image registry", "tests/08-k8s/check_images.py"),
    ("Ingress & PVC", "tests/08-k8s/check_ingress.py"),
    ("Service endpoints", "tests/08-k8s/check_services.py"),
    ("Keycloak & OIDC", "tests/08-k8s/check_keycloak.py"),
    ("Service integration", "tests/08-k8s/check_integration.py"),
    ("File picker deep integration", "tests/08-k8s/check_filepicker_integration.py"),
]


def main():
    result = Result("k8s-deployment")
    result.header("Layer 8: Kubernetes deployment health")

    total_pass = 0
    total_fail = 0

    for name, script in K8S_TESTS:
        full_path = ROOT / script
        if not full_path.exists():
            result.warn(f"{name}: {script} not found")
            continue

        print(f"\n  {result.C['BOLD']}── {name} ──{result.C['NC']}")
        try:
            proc = subprocess.run(
                [sys.executable, str(full_path)],
                capture_output=True, text=True, timeout=120,
                cwd=str(ROOT),
            )
            print(proc.stdout.rstrip())
            if proc.stderr:
                print(proc.stderr.rstrip())
            if proc.returncode == 0:
                total_pass += 1
            else:
                total_fail += 1
        except subprocess.TimeoutExpired:
            print(f"  {result.C['FAIL']}✗{result.C['NC']} {name}: timed out")
            total_fail += 1
        except Exception as e:
            print(f"  {result.C['FAIL']}✗{result.C['NC']} {name}: {e}")
            total_fail += 1

    print(f"\n{result.C['BOLD']}════════════════════════════════════════════════════════════{result.C['NC']}")
    status = result.C["PASS"] + "PASS" if total_fail == 0 else result.C["FAIL"] + "FAIL"
    print(f"  {result.C['BOLD']}K8s tests: {result.C['PASS'] if total_fail == 0 else result.C['FAIL']}{total_pass} passed{result.C['NC']}, {result.C['FAIL'] if total_fail else result.C['PASS']}{total_fail} failed{result.C['NC']} {status}{result.C['NC']}")
    print(f"{result.C['BOLD']}════════════════════════════════════════════════════════════{result.C['NC']}")

    return total_fail == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
