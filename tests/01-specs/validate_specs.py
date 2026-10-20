#!/usr/bin/env python3
"""
tests/01-specs/validate_specs.py — Spec compliance checker.

Loads all spec files from specs/ and verifies that the compose files
match: correct image, correct ports, healthcheck present, correct networks,
resource limits set, Traefik labels present, env vars defined.

Usage:
    python3 tests/01-specs/validate_specs.py

Exit codes:
    0 = all specs pass
    1 = spec violations found
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import (
    ComposeLoader, SpecLoader, Result, ROOT,
    extract_host_ports, has_healthcheck, has_traefik_labels,
    get_image, has_resource_limits, get_networks, get_volumes,
)


def check_image(result, spec_name, spec, svc_data):
    """Check that the service image matches the spec."""
    expected_image = spec.get("image")
    if expected_image is None:
        # image: null means it uses build context
        if "build" in svc_data:
            result.ok(f"{spec_name}: image (build context)")
        else:
            result.fail(f"{spec_name}: no image and no build context")
        return

    actual_image = get_image(svc_data)
    if actual_image is None:
        result.fail(f"{spec_name}: expected image {expected_image}, got none")
    elif actual_image == expected_image:
        result.ok(f"{spec_name}: image matches")
    elif expected_image in actual_image or actual_image.startswith(expected_image.split(":")[0]):
        # Allow image tag variations (e.g. ${VAR} expands)
        result.ok(f"{spec_name}: image matches (with variable)")
    else:
        result.fail(f"{spec_name}: expected image {expected_image}, got {actual_image}")


def check_host_ports(result, spec_name, spec, svc_data):
    """Check that host ports match the spec."""
    expected_ports = set(spec.get("host_ports") or [])
    actual_ports = set(extract_host_ports(svc_data))

    if expected_ports == actual_ports:
        if expected_ports:
            result.ok(f"{spec_name}: ports {sorted(expected_ports)}")
        else:
            result.ok(f"{spec_name}: no host ports (internal)")
    else:
        if expected_ports and not actual_ports:
            result.fail(f"{spec_name}: expected host ports {sorted(expected_ports)}, "
                       f"got none")
        elif actual_ports and not expected_ports:
            result.fail(f"{spec_name}: expected no host ports, "
                       f"got {sorted(actual_ports)}")
        else:
            result.fail(f"{spec_name}: expected ports {sorted(expected_ports)}, "
                       f"got {sorted(actual_ports)}")


def check_healthcheck(result, spec_name, spec, svc_data):
    """Check healthcheck presence per spec."""
    expected = spec.get("healthcheck", False)
    actual = has_healthcheck(svc_data)

    if expected and actual:
        result.ok(f"{spec_name}: healthcheck defined")
    elif expected and not actual:
        result.fail(f"{spec_name}: healthcheck required but not defined")
    elif not expected and actual:
        result.warn(f"{spec_name}: healthcheck defined but not required (OK)")
    else:
        result.ok(f"{spec_name}: no healthcheck (as expected)")


def check_networks(result, spec_name, spec, svc_data):
    """Check that the service is on the expected networks."""
    expected_networks = set(spec.get("networks") or [])
    actual_networks = set(get_networks(svc_data))

    if not expected_networks:
        return  # spec doesn't require specific networks

    missing = expected_networks - actual_networks
    if missing:
        result.fail(f"{spec_name}: missing networks: {missing}")
    else:
        result.ok(f"{spec_name}: on network(s) {sorted(expected_networks)}")


def check_resource_limits(result, spec_name, spec, svc_data):
    """Check that resource limits are set."""
    expected_memory = (spec.get("resource_limits") or {}).get("memory")
    has_limits = has_resource_limits(svc_data)

    if expected_memory and has_limits:
        result.ok(f"{spec_name}: resource limits set")
    elif expected_memory and not has_limits:
        result.fail(f"{spec_name}: resource limits required but not set")
    elif not expected_memory and has_limits:
        result.ok(f"{spec_name}: resource limits set (not required)")
    else:
        result.skip(f"{spec_name}: no resource limit requirement")


def check_traefik_labels(result, spec_name, spec, svc_data):
    """Check Traefik labels per spec."""
    expected = spec.get("traefik_labels", False)
    actual = has_traefik_labels(svc_data)

    if expected and actual:
        result.ok(f"{spec_name}: Traefik labels present")
    elif expected and not actual:
        result.fail(f"{spec_name}: Traefik labels required but not found")
    elif not expected and actual:
        result.warn(f"{spec_name}: Traefik labels found but not required")
    else:
        result.ok(f"{spec_name}: no Traefik labels (as expected)")


def check_volumes(result, spec_name, spec, svc_data):
    """Check that expected volumes are present."""
    expected_volumes = spec.get("volumes") or []
    if not expected_volumes:
        return

    actual_volumes = get_volumes(svc_data)
    for ev in expected_volumes:
        ev_source = ev.split(":")[0] if ":" in ev else ev
        found = False
        for av in actual_volumes:
            av_source = av.split(":")[0] if ":" in av else av
            if ev_source == av_source:
                found = True
                break
        if not found:
            result.fail(f"{spec_name}: missing volume {ev_source}")
        else:
            result.ok(f"{spec_name}: volume {ev_source} present")


def main():
    result = Result("spec-validation")
    result.header("Layer 1: Spec compliance")

    loader = ComposeLoader(ROOT)
    loader.load()
    spec_loader = SpecLoader()
    specs = spec_loader.load()

    result.info(f"Loaded {len(specs)} service specs from {len(list(spec_loader.SPEC_DIR.glob('*.yml')))} spec files")
    result.info(f"Loaded {len(loader.services)} services from compose files")

    for svc_name, spec in sorted(specs.items()):
        _spec_file = spec.get("_spec_file", "?")
        compose_file = spec.get("compose_file", "?")
        required = spec.get("required", False)

        svc = loader.get_service(svc_name)

        if svc is None:
            if required:
                result.fail(f"{svc_name}: required service not found in {compose_file}")
            else:
                result.skip(f"{svc_name}: optional (not in compose files)")
            continue

        # Check the service is in the expected compose file
        if svc["file"] != compose_file:
            result.warn(f"{svc_name}: found in {svc['file']}, spec says {compose_file}")

        svc_data = svc["data"]

        # Run all checks
        check_image(result, svc_name, spec, svc_data)
        check_host_ports(result, svc_name, spec, svc_data)
        check_healthcheck(result, svc_name, spec, svc_data)
        check_networks(result, svc_name, spec, svc_data)
        check_resource_limits(result, svc_name, spec, svc_data)
        check_traefik_labels(result, svc_name, spec, svc_data)
        check_volumes(result, svc_name, spec, svc_data)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
