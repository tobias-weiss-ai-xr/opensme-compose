#!/usr/bin/env python3
"""
tests/05-e2e/runtime_truth.py — what runs is what's declared (Epic N).

Configuration that drifts from reality is a source of outages. This
journey compares the RUNNING containers against the compose declaration:

  N1  every running opensme container uses exactly the image that
      `docker compose config` declares for its service (no drift)
  N2  no running container is on a mutable `:latest` tag — a stack that
      can change its own bits on a re-pull is not a stack you can audit

Read-only, requires docker and a running stack.

Usage:
    python3 tests/05-e2e/runtime_truth.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"


def compose_args() -> list[str]:
    cf = os.environ.get("COMPOSE_FILE") or DEMO_SET
    args: list[str] = []
    for f in cf.split(os.pathsep):
        if f:
            args += ["-f", f]
    return args


def main() -> int:
    result = Result("runtime-truth")
    result.header("openSME e2e runtime truth — what runs is what's declared")

    try:
        probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                               capture_output=True, text=True, timeout=60)
    except subprocess.SubprocessError:
        probe = None
    if not probe or probe.returncode != 0 or \
            not any("opensme" in n for n in probe.stdout.split()):
        result.skip("stack not running")
        print()
        return 0

    # what compose declares: service → image(+digest). COMPOSE_PROFILES
    # matches the demo stack's profile set (scripts/demo.sh: standalone)
    # so profile-gated services like traefik are included.
    env = dict(os.environ, COMPOSE_PROFILES="standalone")
    cfg = subprocess.run(
        ["docker", "compose"] + compose_args() + ["config", "--format", "json"],
        env=env, capture_output=True, text=True, timeout=120)
    if cfg.returncode != 0:
        result.fail(f"compose config failed: {cfg.stderr[:200]}")
        return 1
    declared = json.loads(cfg.stdout).get("services", {})

    # what actually runs: container (service label) → image(+digest)
    ps = subprocess.run(
        ["docker", "compose"] + compose_args() +
        ["ps", "--format", "json", "--filter", "status=running"],
        capture_output=True, text=True, timeout=120)
    running: dict[str, str] = {}
    for line in (ps.stdout or "").splitlines():
        try:
            j = json.loads(line)
            svc = j.get("Service", "")
            img = j.get("Image", "")
            if svc:
                running[svc] = img
        except json.JSONDecodeError:
            continue

    if not running:
        result.skip("no running services found")
        print()
        return 0

    # N1: no drift between declaration and reality. Locally built services
    # (compose `build:` without `image:`) are their own source of truth —
    # the build context is the declaration.
    drift = []
    for svc, img in running.items():
        decl = declared.get(svc)
        if decl is None:
            drift.append(f"{svc}: running but not declared")
            continue
        want = decl.get("image", "")
        if not want:
            if decl.get("build"):
                continue  # built from source in this repo — no drift concept
            drift.append(f"{svc}: declared without image or build")
        elif img.split("@")[0] != want.split("@")[0]:
            drift.append(f"{svc}: declared {want.split('@')[0]}, running {img.split('@')[0]}")
    unknown = [s for s in declared
               if s not in running and (declared[s].get("image") or declared[s].get("build"))]
    (result.ok if not drift else result.fail)(
        f"no image drift across {len(running)} running service(s)"
        if not drift else f"runtime drifts from declaration: {'; '.join(drift)}"
    )
    if unknown:
        result.warn(f"declared but not running (profiles off?): {', '.join(unknown)}")

    # N2: mutable tags have no place in an auditable stack
    floating = [f"{svc}:{img}" for svc, img in running.items()
                if img.split("@")[0].endswith(":latest")]
    (result.ok if not floating else result.fail)(
        "no container runs a mutable :latest tag" if not floating
        else f":latest in production bits: {'; '.join(floating)}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
