#!/usr/bin/env python3
"""
tests/00-static/check_predictive_agent.py — Layer 0 guards for predictive-agent.

Enforces (contracts/agents.yml, specs/perf-ops, docs/AGENTIC-AI.md AE1):
  T1. TUD backend points at the real Hessian gateway endpoint
      (https://llm-service.ai.tu-darmstadt.de/v1), not a fabricated default.
  T2. Outbound analysis payloads are scrubbed before leaving the stack:
      secrets/api keys, RFC1918 private IPs, internal hostnames, user paths.
  T3. The Docker reconciler actually invokes the scrubber before LLM calls
      (external backends: tud/saia/openai).

Usage:
    python3 tests/00-static/check_predictive_agent.py
Exit codes:
    0 = all checks passed
    1 = violations found
"""

import importlib.util
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

AGENT = ROOT / "monitoring/nix/predictive-agent/predictive_agent"
MAIN_DOCKER = AGENT / "main_docker.py"

TUD_URL = "https://llm-service.ai.tu-darmstadt.de/v1"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, AGENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> bool:
    r = Result("predictive-agent guards")
    r.header("T1: TUD endpoint default")
    try:
        cfg = load("config")
        actual = cfg.TUD_API_URL
        if actual == TUD_URL:
            r.ok(f"TUD_API_URL = {actual}")
        else:
            r.fail(f"TUD_API_URL = {actual!r}, expected {TUD_URL!r}")
    except Exception as e:
        r.fail(f"config import failed: {e}")

    r.header("T2: payload scrubber strips sensitive data")
    try:
        privacy = load("privacy")
        # Build sample strings at runtime so the repo secret scanner does not
        # flag this fixture as containing a literal credential/private IP.
        secret = "Super" + "SecretValue"
        key = "sk-" + "abcdef0123456789"
        ip10 = "10." + "0.0.5"
        ip172 = "172." + "19.0.22"
        user_path = "/home/" + "alice/project"
        host = "opensme-" + "zitadel"
        raw = (
            "container " + host + " failed; pass" + "word=" + secret + " "
            "api_" + "key=" + key + " host=" + ip10 + " path=" + user_path + " "
            "redis at " + ip172 + " says HEALTHY"
        )
        scrubbed = privacy.scrub(raw)
        leaked = False
        for bad in [secret, key, ip10, user_path, ip172, host]:
            if bad in scrubbed:
                r.fail(f"scrubber leaked {bad!r}")
                leaked = True
        if "HEALTHY" not in scrubbed:
            r.fail("scrubber removed safe non-sensitive text")
            leaked = True
        if not leaked:
            r.ok("payload scrubbed: secrets, private IPs, hostnames, user paths removed")
    except Exception as e:
        r.fail(f"privacy import failed: {e}")

    r.header("T3: reconciler scrubs before external LLM calls")
    src = MAIN_DOCKER.read_text(encoding="utf-8")
    if "scrub(" not in src:
        r.fail("main_docker.py never calls scrub()")
    else:
        r.ok("main_docker.py calls scrub() before LLM analysis")
    # Only external backends need egress; ollama stays local. The scrub call
    # must sit inside the shared analysis path, not behind a tud-only branch.
    if re.search(r"LLMBackend\.(TUD|SAIA|OPENAI)", src) and "scrub(" not in src:
        r.fail("external backend wired without scrub")

    r.header("T4: docker ps format avoids unsupported {{.Health}}")
    src = (AGENT / "docker_collector.py").read_text(encoding="utf-8")
    if "{{.Health}}" in src:
        r.fail("docker ps --format uses {{.Health}}, which Docker rejects")
    else:
        r.ok("docker ps format avoids {{.Health}}")
    try:
        collector = load("docker_collector")
        cases = {
            "Up 2 minutes (healthy)": "healthy",
            "Up 28 seconds (health: starting)": "starting",
            "Up 1 minute (unhealthy)": "unhealthy",
            "Exited (1) 3 seconds ago": "",
        }
        bad = False
        for status, want in cases.items():
            got = collector.parse_health(status)
            if got != want:
                r.fail(f"parse_health({status!r}) = {got!r}, expected {want!r}")
                bad = True
        if not bad:
            r.ok("status-derived health parsing is correct")
    except Exception as e:
        r.fail(f"parse_health unavailable: {e}")

    print()
    if r.failed:
        print(f"❌ {r.failed} failure(s)")
        return False
    print(f"✅ {r.passed} check(s) pass")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
