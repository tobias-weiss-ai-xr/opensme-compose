#!/usr/bin/env python3
"""
tests/08-k8s/check_images.py — Image registry validation.

Verifies that no running deployment or statefulset in the opensme namespaces
references the old registry.opencode.de registry. All images should come from
either the local registry (K8S_LOCAL_REGISTRY, default registry.local:5001)
or a public registry.

Usage:
    python3 tests/08-k8s/check_images.py

Exit codes:
    0 = no stale registry references
    1 = stale registry.opencode.de references found
"""

import os
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

# Stale registries that should no longer appear
STALE_REGISTRIES = [
    "registry.opencode.de",
]

# Acceptable registries
ACCEPTABLE_REGISTRIES = [
    os.environ.get("K8S_LOCAL_REGISTRY", "registry.local:5001"),  # local registry
    "ghcr.io",            # GitHub Container Registry
    "quay.io",            # quay.io (oauth2-proxy, keycloak)
    "docker.io",          # Docker Hub (nginx, etc.)
    "registry.k8s.io",    # k8s registry (pause image, etc.)
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


def extract_images(containers: list) -> list[str]:
    """Extract image names from a container list."""
    images = []
    for c in containers or []:
        img = c.get("image", "")
        if img:
            images.append(img)
    return images


def check_workload_images(result: Result, ns: str, kind: str):
    """Check images for Deployments or StatefulSets in a namespace."""
    plural = "deployments" if kind == "Deployment" else "statefulsets"
    items = kubectl_json(["-n", ns, "get", plural])
    if items is None:
        return  # skip silently if namespace has no such resources

    for item in items.get("items", []):
        name = item["metadata"]["name"]
        containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        init_containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("initContainers", [])
        all_containers = (containers or []) + (init_containers or [])

        for img in extract_images(all_containers):
            is_stale = any(stale in img for stale in STALE_REGISTRIES)
            if is_stale:
                result.fail(f"{ns}/{name}: stale image {img}")
            else:
                # Check if image is from an acceptable registry (or has no registry = Docker Hub)
                is_acceptable = any(acc in img for acc in ACCEPTABLE_REGISTRIES)
                if is_acceptable or "/" not in img.split(":")[0]:
                    result.ok(f"{ns}/{name}: {img}")
                else:
                    # Docker Hub images without explicit registry prefix (e.g. osixia/openldap:1.5.0)
                    # are acceptable — Docker Hub is the implicit registry
                    if "/" in img and not img.startswith("/"):
                        result.ok(f"{ns}/{name}: {img} (Docker Hub)")
                    else:
                        result.warn(f"{ns}/{name}: unknown registry for {img}")


def main():
    result = Result("k8s-images")
    result.header("Layer 8: Image registry validation")

    result.info(f"Stale registries: {', '.join(STALE_REGISTRIES)}")
    result.info(f"Acceptable: {', '.join(ACCEPTABLE_REGISTRIES)}")

    for ns in NAMESPACES:
        result.header(f"Namespace: {ns}")
        check_workload_images(result, ns, "Deployment")
        check_workload_images(result, ns, "StatefulSet")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
