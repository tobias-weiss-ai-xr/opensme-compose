#!/usr/bin/env python3
"""
tests/00-static/yaml_lint.py — YAML syntax and structure validation.

Validates that all compose files are valid YAML and have the expected
top-level structure (services:, volumes:, networks:).

Usage:
    python3 tests/00-static/yaml_lint.py

Exit codes:
    0 = all YAML valid
    1 = YAML errors found
"""

import sys
import yaml
from pathlib import Path

# Register Docker Compose extension tags
yaml.SafeLoader.add_constructor('!reset', lambda loader, node: None)
yaml.SafeLoader.add_constructor('!override', lambda loader, node: loader.construct_scalar(node))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import ComposeLoader, Result, ROOT  # noqa: E402 (compose tags registered first)


REQUIRED_TOP_KEYS = {"services"}


def main():
    result = Result("yaml-lint")
    result.header("Layer 0: YAML syntax validation")

    loader = ComposeLoader(ROOT)

    # Check each compose file
    for rel_path in loader.COMPOSE_FILES + loader.PROFILE_FILES:
        full_path = ROOT / rel_path
        if not full_path.exists():
            result.skip(f"{rel_path} not found")
            continue

        # Parse YAML
        try:
            with open(full_path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            result.fail(f"{rel_path}: YAML parse error: {e}")
            continue

        if not data:
            result.warn(f"{rel_path}: empty file")
            continue

        # Check top-level keys
        top_keys = set(data.keys())
        missing = REQUIRED_TOP_KEYS - top_keys
        if missing:
            # Profiles may only override, not define services
            if "profiles/" in rel_path:
                result.ok(f"{rel_path} (profile override)")
            else:
                result.fail(f"{rel_path}: missing top-level keys: {missing}")
        else:
            svc_count = len(data.get("services") or {})
            vol_count = len(data.get("volumes") or {})
            net_count = len(data.get("networks") or {})
            result.ok(f"{rel_path} ({svc_count} services, "
                      f"{vol_count} volumes, {net_count} networks)")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
