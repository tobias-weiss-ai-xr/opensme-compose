#!/usr/bin/env python3
"""
check_agent.py — Layer 0 static lint for the dev-maintenance-bot.

Validates (dev-agent + dev-agent-compose + dev-agent-knowledge specs):
  1. monitoring/dev-agent.yml wires the bot safely:
     - /var/run/docker.sock mounted READ-ONLY
     - no published host ports
     - restart: "no" (one-shot)
  2. opensme-knowledge/*.json runbooks parse, schema == 1, and every
     core service has a runbook.

Usage: python3 tests/00-static/check_agent.py
"""

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "monitoring" / "dev-agent.yml"
KB = ROOT / "opensme-knowledge"
AGENTS_CONTRACT = ROOT / "contracts" / "agents.yml"
CORE_SERVICES = {
    "traefik", "postgres", "zitadel", "stalwart",
    "sogo", "opencloud", "invoice-ninja", "paperless",
}
SERVICE = "dev-maintenance-bot"
WRITE_MODES = {"none", "draft", "consent", "approved"}


def check_compose() -> list[str]:
    errors = []
    doc = yaml.safe_load(OVERLAY.read_text())
    svc = (doc.get("services") or {}).get(SERVICE)
    if svc is None:
        return [f"{OVERLAY.name}: service '{SERVICE}' missing"]

    ro_sock = any(
        "/var/run/docker.sock" in str(v) and str(v).rstrip().endswith(":ro")
        for v in (svc.get("volumes") or [])
    )
    if not ro_sock:
        errors.append("docker.sock must be mounted :ro")

    if svc.get("ports"):
        errors.append("no host ports may be published for the agent API")

    if svc.get("restart") != "no":
        errors.append(f'restart must be "no" (one-shot), got {svc.get("restart")!r}')
    return errors


def check_knowledge() -> list[str]:
    errors = []
    if not KB.is_dir():
        return [f"{KB}: knowledge base directory missing"]
    seen = set()
    for f in sorted(KB.glob("*.json")):
        try:
            doc = json.loads(f.read_text())
        except json.JSONDecodeError as e:
            errors.append(f"{f.name}: malformed runbook JSON: {e}")
            continue
        if doc.get("schema") != 1:
            errors.append(f"{f.name}: schema must be 1")
        svc = doc.get("service", "")
        if svc != f.stem:
            errors.append(f"{f.name}: service {svc!r} does not match file name")
        if not doc.get("runbooks"):
            errors.append(f"{f.name}: no runbooks")
        seen.add(svc)
    missing = CORE_SERVICES - seen
    if missing:
        errors.append(f"core services without runbook: {sorted(missing)}")
    return errors


def check_agents_contract() -> list[str]:
    """contracts/agents.yml — schema + contract ↔ compose wiring.

    Enforces docs/AGENTIC-AI.md §5: every agent declares write_mode, tools
    and privacy; acting agents declare a consent gate defaulting to off;
    the compose overlay matches (env flag present, ro socket where
    declared, no published ports).
    """
    errors = []
    if not AGENTS_CONTRACT.is_file():
        return [f"{AGENTS_CONTRACT.name}: missing — no agent without a contract line"]
    doc = yaml.safe_load(AGENTS_CONTRACT.read_text())
    if doc.get("schema") != 1:
        errors.append("schema must be 1")
    agents = doc.get("agents") or []
    if not agents:
        errors.append("no agents declared")

    overlays: dict[str, dict] = {}
    names = set()
    for a in agents:
        name = a.get("name", "")
        if not name:
            errors.append("agent without name")
            continue
        if name in names:
            errors.append(f"{name}: duplicate agent name")
        names.add(name)

        mode = a.get("write_mode")
        if mode not in WRITE_MODES:
            errors.append(f"{name}: write_mode must be one of {sorted(WRITE_MODES)}, got {mode!r}")

        tools = a.get("tools") or []
        if not tools:
            errors.append(f"{name}: tools must not be empty")
        strip = (a.get("privacy") or {}).get("strip") or []
        if any("llm" in t for t in tools) and not strip:
            errors.append(f"{name}: LLM tooling requires privacy.strip entries")

        consent = a.get("consent_env")
        if mode == "consent" and not consent:
            errors.append(f"{name}: write_mode consent requires consent_env")
        if mode == "approved" and not a.get("approval"):
            errors.append(f"{name}: write_mode approved requires an approval gate")
        if not a.get("invariants"):
            errors.append(f"{name}: invariants must not be empty (human-readable gate)")

        # ── contract ↔ compose wiring ──
        overlay_rel = a.get("compose_file", "")
        svc_name = a.get("compose_service", "")
        if not overlay_rel or not svc_name:
            errors.append(f"{name}: compose_file and compose_service required")
            continue
        overlay = overlays.get(overlay_rel)
        if overlay is None:
            path = ROOT / overlay_rel
            if not path.is_file():
                errors.append(f"{name}: compose_file {overlay_rel} missing")
                continue
            overlay = yaml.safe_load(path.read_text()) or {}
            overlays[overlay_rel] = overlay
        svc = (overlay.get("services") or {}).get(svc_name)
        if svc is None:
            errors.append(f"{name}: service {svc_name!r} missing in {overlay_rel}")
            continue
        env = svc.get("environment") or {}
        env_str = json.dumps(env)

        if svc.get("ports"):
            errors.append(f"{name}: agent services must not publish host ports")

        if "docker-readonly" in tools:
            ro_sock = any(
                "/var/run/docker.sock" in str(v) and str(v).rstrip().endswith(":ro")
                for v in (svc.get("volumes") or [])
            )
            if not ro_sock:
                errors.append(f"{name}: declares docker-readonly but socket is not mounted :ro")

        if consent:
            if consent not in env_str:
                errors.append(f"{name}: consent_env {consent} not wired in compose environment")
            if f"${{{consent}:-false}}" not in env_str:
                errors.append(f"{name}: consent_env {consent} must default to false")
            if svc.get("restart") != "no":
                errors.append(f"{name}: consent-gated writers must be one-shot (restart: 'no')")

        kill = a.get("kill_switch_env")
        if kill and kill not in env_str:
            # the switch may live in the consumer's env (e.g. portal-assistant
            # is switched via the portal's AI_API_URL), not the backend's
            wired = False
            consumer = a.get("consumer_service")
            if consumer:
                main = yaml.safe_load((ROOT / "docker-compose.yml").read_text()) or {}
                cenv = ((main.get("services") or {}).get(consumer) or {}).get("environment") or {}
                wired = kill in json.dumps(cenv)
            if not wired:
                errors.append(f"{name}: kill_switch_env {kill} not wired in compose environment")

        budget = (a.get("budgets") or {}).get("max_requests_env")
        if budget and budget not in env_str:
            errors.append(f"{name}: budgets.max_requests_env {budget} not wired in compose environment")
    return errors


def main() -> int:
    errors = check_compose() + check_knowledge() + check_agents_contract()
    if errors:
        for e in errors:
            print(f"  ❌ {e}")
        print("❌ dev-maintenance-bot static check: FAILED")
        return 1
    print("  ✅ compose wiring (ro socket, no ports, one-shot)")
    print(f"  ✅ knowledge base: {len(list(KB.glob('*.json')))} services incl. all {len(CORE_SERVICES)} core services")
    n_agents = len((yaml.safe_load(AGENTS_CONTRACT.read_text()) or {}).get("agents") or [])
    print(f"  ✅ agents contract: {n_agents} agents under guardrails (write modes, consent, privacy, invariants)")
    print("✅ dev-maintenance-bot static check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
