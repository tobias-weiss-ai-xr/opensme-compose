#!/usr/bin/env python3
"""
tests/run.py — Main test runner for the spec-contract-test scaffold.

Runs all test layers in order:
  Layer 0: Static validation (YAML lint, env check, secret scan)
  Layer 1: Spec compliance (compose files match specs/)
  Layer 2: Contract validation (contracts/ rules)
  Layer 3: Smoke tests (HTTP, container health — requires running stack)
  Layer 4: Integration tests (service interactions — requires running stack)
  Layer 5: E2E tests (SSO/OIDC flows — requires running stack)
  Layer 6: Security audit

Usage:
    python3 tests/run.py                    # Run all layers
    python3 tests/run.py --layer 0          # Run only layer 0
    python3 tests/run.py --layer 0,1,2      # Run layers 0, 1, 2
    python3 tests/run.py --static           # Layers 0-2 (no running stack needed)
    python3 tests/run.py --smoke            # Layer 3 only
    python3 tests/run.py --security         # Layer 6 only
    python3 tests/run.py --e2e              # Layer 5 only (requires running stack)
    python3 tests/run.py --domain example.com  # Domain for smoke tests

Exit codes:
    0 = all tests passed
    1 = one or more tests failed
"""

import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Colors
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
BOLD = "\033[1m"
NC = "\033[0m"

# Test layers
LAYERS = {
    0: {
        "name": "Static validation",
        "description": "YAML lint, env check, secret scan",
        "scripts": [
            ("YAML lint", "tests/00-static/yaml_lint.py"),
            ("Env completeness", "tests/00-static/check_env.py"),
            ("Secret scan", "tests/00-static/scan_secrets.py"),
            ("Committed-artifact hygiene", "tests/00-static/check_artifacts.py"),
            ("Perf & budgets", "tests/00-static/check_perf.py"),
            ("Boot contracts", "tests/00-static/check_boot.py"),
            ("Compose config gate", "tests/00-static/compose_config.py"),
            ("Platform versions (k3s/docker)", "tests/00-static/check_platform.py"),
            ("Bootstrap seed data (BPMN)", "tests/00-static/check_bootstrap.py"),
            ("Backup/restore contract", "tests/00-static/check_backup_restore.py"),
            ("Mail backend switch (XOR)", "tests/00-static/check_mail_backend.py"),
            ("Portal function catalog (tiles)", "tests/00-static/check_tiles.py"),
            ("Interoperability surfaces", "tests/00-static/check_interop.py"),
        ],
        "requires_stack": False,
    },
    1: {
        "name": "Spec compliance",
        "description": "Compose files match specs/",
        "scripts": [
            ("Spec validation", "tests/01-specs/validate_specs.py"),
        ],
        "requires_stack": False,
    },
    2: {
        "name": "Contract validation",
        "description": "contracts/ rules (env, ports, health, networks, security)",
        "scripts": [
            ("Contract validation", "tests/02-contracts/validate_contracts.py"),
        ],
        "requires_stack": False,
    },
    3: {
        "name": "Smoke tests",
        "description": "HTTP endpoints, container health",
        "scripts": [
            ("Smoke tests", "tests/03-smoke/run.py"),
        ],
        "requires_stack": True,
    },
    4: {
        "name": "Integration tests",
        "description": "Cross-service wiring: DB provisioning, pgbouncer, SSO issuer, live AI proxy",
        "scripts": [
            ("Cross-service integration", "tests/04-integration/run.py"),
        ],
        "requires_stack": True,
    },
    5: {
        "name": "E2E tests",
        "description": "SSO / OIDC flows over HTTP (Zitadel, portal, apps)",
        "scripts": [
            ("E2E tests (SSO etc.)", "tests/05-e2e/run.py"),
        ],
        "requires_stack": True,
    },
    6: {
        "name": "Security audit",
        "description": "Exposed ports, secrets, TLS, privileges",
        "scripts": [
            ("Security audit", "tests/06-security/audit.py"),
        ],
        "requires_stack": False,
    },
    8: {
        "name": "K8s deployment",
        "description": "ArgoCD, pods, deployments, images, ingress, PVC, services, Keycloak",
        "scripts": [
            ("K8s deployment", "tests/08-k8s/run.py"),
        ],
        "requires_stack": True,
    },
}


def run_script(name: str, script_path: str, domain: str = "localhost",
               e2e: bool = False) -> tuple[bool, str]:
    """Run a test script and return (success, output)."""
    full_path = ROOT / script_path
    if not full_path.exists():
        return False, f"{script_path} not found"

    cmd = [sys.executable, str(full_path)]
    # Smoke always targets the local port. E2E, by contrast, resolves the
    # domain itself from .env (OPENSME_DOMAIN) unless the caller was
    # explicit — passing the default "localhost" would hide a demo-domain
    # .env, so only append a domain argument when --domain was given.
    if "smoke" in script_path:
        cmd.append(domain)
    elif "e2e" in script_path and e2e:
        cmd.append(domain)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=420 if "e2e" in script_path else 120,
            cwd=str(ROOT),
        )
        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"{script_path} timed out"
    except Exception as e:
        return False, f"{script_path}: {e}"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="openSME test runner")
    parser.add_argument("--layer", type=str, default=None,
                        help="Comma-separated layer numbers (e.g. 0,1,2)")
    parser.add_argument("--static", action="store_true",
                        help="Run only static layers (0-2, no running stack)")
    parser.add_argument("--smoke", action="store_true",
                        help="Run only smoke tests (layer 3)")
    parser.add_argument("--security", action="store_true",
                        help="Run only security audit (layer 6)")
    parser.add_argument("--e2e", action="store_true",
                        help="Run only e2e tests (layer 5, requires running stack)")
    parser.add_argument("--k8s", action="store_true",
                        help="Run only k8s deployment tests (layer 8)")
    parser.add_argument("--domain", type=str, default="localhost",
                        help="Domain for smoke tests (default: localhost)")
    args = parser.parse_args()

    # Determine which layers to run
    if args.static:
        layers_to_run = [0, 1, 2]
    elif args.smoke:
        layers_to_run = [3]
    elif args.security:
        layers_to_run = [6]
    elif args.e2e:
        layers_to_run = [5]
    elif args.k8s:
        layers_to_run = [8]
    elif args.layer:
        layers_to_run = [int(x) for x in args.layer.split(",")]
    else:
        layers_to_run = [0, 1, 2, 3, 6]  # Skip 4, 5, 8 (4 unimplemented; 5 & 8 need opt-in stack access)

    print(f"\n{BOLD}╔══════════════════════════════════════════════════════════════╗{NC}")
    print(f"{BOLD}║  openSME — Spec / Contract / Test Scaffold             ║{NC}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════╝{NC}")
    print(f"\n  Domain: {BLUE}{args.domain}{NC}")
    print(f"  Layers: {BLUE}{', '.join(str(l) for l in layers_to_run)}{NC}")

    total_pass = 0
    total_fail = 0
    layer_results = {}

    for layer_num in sorted(layers_to_run):
        layer = LAYERS.get(layer_num)
        if not layer:
            print(f"\n{YELLOW}Layer {layer_num}: unknown{NC}")
            continue

        print(f"\n{BOLD}═══ Layer {layer_num}: {layer['name']} ═══{NC}")
        print(f"{BLUE}    {layer['description']}{NC}")

        if layer["requires_stack"]:
            # Check if stack is running
            # Layer 8 checks kubectl, not docker compose
            if layer_num == 8:
                try:
                    ps = subprocess.run(
                        ["kubectl", "cluster-info", "--request-timeout=10s"],
                        capture_output=True, text=True, timeout=15,
                    )
                    if ps.returncode != 0:
                        print(f"  {YELLOW}⚠ kubectl not reachable — skipping layer {layer_num}{NC}")
                        layer_results[layer_num] = "skipped"
                        continue
                except Exception:
                    print(f"  {YELLOW}⚠ kubectl not available — skipping layer {layer_num}{NC}")
                    layer_results[layer_num] = "skipped"
                    continue
            else:
                try:
                    ps = subprocess.run(
                        ["docker", "compose", "ps", "--format", "{{.Name}}"],
                        capture_output=True, text=True, timeout=10,
                        cwd=str(ROOT)
                    )
                    if not ps.stdout.strip():
                        print(f"  {YELLOW}⚠ Stack not running — skipping layer {layer_num}{NC}")
                        layer_results[layer_num] = "skipped"
                        continue
                except Exception:
                    print(f"  {YELLOW}⚠ Cannot check Docker — skipping layer {layer_num}{NC}")
                    layer_results[layer_num] = "skipped"
                    continue

        if not layer["scripts"]:
            print(f"  {YELLOW}⚠ Layer {layer_num} not yet implemented{NC}")
            layer_results[layer_num] = "not-implemented"
            continue

        layer_pass = 0
        layer_fail = 0
        for script_name, script_path in layer["scripts"]:
            print(f"\n  {BOLD}── {script_name} ──{NC}")
            e2e_explicit = "--domain" in sys.argv or "-d" in sys.argv
            ok, output = run_script(script_name, script_path, args.domain,
                                    e2e=e2e_explicit)
            print(output.rstrip())
            if ok:
                layer_pass += 1
            else:
                layer_fail += 1

        total_pass += layer_pass
        total_fail += layer_fail
        layer_results[layer_num] = "pass" if layer_fail == 0 else "fail"

    # Summary
    print(f"\n{BOLD}╔══════════════════════════════════════════════════════════════╗{NC}")
    print(f"{BOLD}║  Summary                                                    ║{NC}")
    print(f"{BOLD}╠══════════════════════════════════════════════════════════════╣{NC}")
    for layer_num in sorted(layers_to_run):
        status = layer_results.get(layer_num, "not-run")
        icon = {"pass": f"{GREEN}✓{NC}", "fail": f"{RED}✗{NC}",
                "skipped": f"{YELLOW}○{NC}", "not-implemented": f"{YELLOW}○{NC}",
                "not-run": f"{BLUE}—{NC}"}.get(status, f"{BLUE}?{NC}")
        layer = LAYERS.get(layer_num, {})
        name = layer.get("name", f"Layer {layer_num}")
        print(f"  {icon} Layer {layer_num}: {name}")

    status_color = GREEN if total_fail == 0 else RED
    print(f"\n  {BOLD}Total: {GREEN}{total_pass} passed{NC}, {status_color}{total_fail} failed{NC}")
    print(f"{BOLD}╚══════════════════════════════════════════════════════════════╝{NC}")

    return total_fail == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
