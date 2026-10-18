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
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "monitoring" / "dev-agent.yml"
KB = ROOT / "opensme-knowledge"
KNOWLEDGE_CONTRACT = ROOT / "contracts" / "knowledge.yml"
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
        if not doc.get("kb_version"):
            # agent-knowledge: aktualität — every runbook file must carry a
            # version the answer evidence can point at (source duty)
            errors.append(f"{f.name}: missing kb_version (aktualität / source duty)")
        svc = doc.get("service", "")
        if svc != f.stem:
            errors.append(f"{f.name}: service {svc!r} does not match file name")
        if not doc.get("runbooks"):
            errors.append(f"{f.name}: no runbooks")
        # agent-knowledge: PII-frei — KB must not carry secrets, private
        # addresses or user paths (mirrors the anonymizer's strip-hop)
        for hit in KBLeak(doc):
            errors.append(f"{f.name}: PII leak {hit}")
        seen.add(svc)
    missing = CORE_SERVICES - seen
    if missing:
        errors.append(f"core services without runbook: {sorted(missing)}")
    return errors


# PII/leak patterns the knowledge base must stay free of (agent-knowledge:
# PII-frei). Mirrors the Go anonymizer's strip set. Private-address literals
# are assembled from parts to keep the leak-scan itself tight. The secret
# rule requires a real credential value (long, symbol-only) right after the
# key name, so prose like "password invalid" or placeholders stay quiet.
PII_SECRET_RE = re.compile(r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd)\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{12,}")
PII_URI_RE = re.compile(r"(?i)\b(postgres|redis|mongodb|mysql|amqp)://[^\s\"']+")
PII_HOME_RE = re.compile(r"/home/[a-z0-9_.-]+|/Users/[a-z0-9_.-]+|/root\b")
PII_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_PRIV4 = re.compile(r"(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.")


def KBLeak(doc) -> list[str]:
    """Return short descriptions of PII found in a parsed runbook doc."""
    text = json.dumps(doc, ensure_ascii=False)
    out = []
    for pat, label in ((PII_SECRET_RE, "secret"), (PII_URI_RE, "uri"),
                       (PII_HOME_RE, "home-path"), (PII_JWT_RE, "jwt")):
        if pat.search(text):
            out.append(f"{label}")
    if _PRIV4.search(text):
        out.append("private-address")
    return out


def check_knowledge_contract() -> list[str]:
    """contracts/knowledge.yml — the RAG contract must exist and be valid."""
    errors = []
    if not KNOWLEDGE_CONTRACT.is_file():
        return [f"{KNOWLEDGE_CONTRACT.name}: missing — no knowledge contract"]
    doc = yaml.safe_load(KNOWLEDGE_CONTRACT.read_text())
    if doc.get("name") != "agent-knowledge":
        errors.append("knowledge.yml: name must be agent-knowledge")
    rules = {r.get("type") for r in (doc.get("rules") or [])}
    for want in ("kb_version-required", "source-duty", "pii-free"):
        if want not in rules:
            errors.append(f"knowledge.yml: missing rule {want}")
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


def check_agent_state_durability() -> list[str]:
    errors = []
    agents = yaml.safe_load(AGENTS_CONTRACT.read_text()) or {}
    for a in (agents.get("agents") or []):
        name = a.get("name")
        write_mode = a.get("write_mode", "none")
        compose_file = a.get("compose_file")
        if write_mode in ("consent", "approved", "draft"):
            if not compose_file:
                errors.append(f"{name}: write_mode={write_mode} but no compose_file defined")
                continue
            full_path = ROOT / compose_file
            if not full_path.exists():
                errors.append(f"{name}: compose_file {compose_file} not found")
                continue
            compose = yaml.safe_load(full_path.read_text()) or {}
            svc = compose.get("services", {}).get(a.get("compose_service"))
            if not svc:
                errors.append(f"{name}: compose_service {a.get('compose_service')} not in {compose_file}")
                continue
            named_volumes = []
            for v in (svc.get("volumes") or []):
                if isinstance(v, dict):
                    vs = v.get("source") or v.get("target")
                    if vs and ":" in str(vs):
                        named_volumes.append(str(vs).split(":")[0])
                else:
                    parts = str(v).split(":")
                    if len(parts) >= 1:
                        named_volumes.append(parts[0])
            state_volumes = [v for v in named_volumes if "-state" in v]
            backup_path = ROOT / "scripts" / "backup.sh"
            backup_text = backup_path.read_text() if backup_path.exists() else ""
            for sv in state_volumes:
                if sv not in backup_text:
                    errors.append(f"{name}: state volume {sv} not in scripts/backup.sh")
    return errors


def main() -> int:
    errors = (check_compose() + check_knowledge() + check_knowledge_contract() +
              check_agents_contract() + check_agent_state_durability())
    if errors:
        for e in errors:
            print(f"  ❌ {e}")
        print("❌ dev-maintenance-bot static check: FAILED")
        return 1
    print("  ✅ compose wiring (ro socket, no ports, one-shot)")
    print(f"  ✅ knowledge base: {len(list(KB.glob('*.json')))} services incl. all {len(CORE_SERVICES)} core services (versioned, source duty, PII-free)")
    n_agents = len((yaml.safe_load(AGENTS_CONTRACT.read_text()) or {}).get("agents") or [])
    print(f"  ✅ agents contract: {n_agents} agents under guardrails (write modes, consent, privacy, invariants)")
    print("✅ dev-maintenance-bot static check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
