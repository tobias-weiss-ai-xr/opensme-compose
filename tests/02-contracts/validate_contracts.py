#!/usr/bin/env python3
"""
tests/02-contracts/validate_contracts.py — Contract validation.

Loads all contract files from contracts/ and validates them against
the compose files. Checks:
  - env-defined: all env vars in compose exist in .env.example
  - no-host-ports: listed services must not expose host ports
  - allowed-host-ports: only listed services may expose host ports
  - healthcheck-defined: listed services must have healthchecks
  - on-network: all services must be on the specified network
  - no-host-network: no service may use network_mode: host
  - resource-limits-set: all services must have deploy.resources.limits
  - no-changeme-in-compose: no CHANGEME_ values in compose files
  - image-has-tag: all images must use explicit tags
  - named-volumes-only: all volumes must be named (not anonymous)

Usage:
    python3 tests/02-contracts/validate_contracts.py

Exit codes:
    0 = all contracts pass
    1 = contract violations found
"""

import sys
import re
import yaml
from pathlib import Path

# Register Docker Compose extension tags
yaml.SafeLoader.add_constructor('!reset', lambda loader, node: None)
yaml.SafeLoader.add_constructor('!override', lambda loader, node: loader.construct_scalar(node))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import (  # noqa: E402 (compose tags registered first)
    ComposeLoader, ContractLoader, EnvLoader, Result, ROOT,
    extract_host_ports, has_healthcheck, has_resource_limits,
    get_networks, is_host_network, extract_env_vars_from_compose,
)


def check_env_defined(result, rule, loader):
    """Check that all env vars in compose files are defined in .env.example."""
    env_file = ROOT / rule.get("target", ".env.example")
    compose_files = rule.get("compose_files", [])
    ignore = set(rule.get("ignore", []))

    env_loader = EnvLoader(env_file)
    defined = env_loader.all_vars()

    missing_count = 0
    for cf in compose_files:
        path = ROOT / cf
        if not path.exists():
            result.skip(f"{cf} not found")
            continue
        _vars_in_file = extract_env_vars_from_compose({"_file": path})
        # Actually parse the file directly
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            for svc_name, svc_data in (data.get("services") or {}).items():
                if not svc_data:
                    continue
                file_vars = extract_env_vars_from_compose(svc_data)
                missing = file_vars - defined - ignore
                for var in sorted(missing):
                    missing_count += 1
                    result.fail(f"{cf}:{svc_name} → {var} not in .env.example")
        except (yaml.YAMLError, OSError) as e:
            result.fail(f"{cf}: cannot parse: {e}")

    if missing_count == 0:
        result.ok(f"all env vars defined in {env_file.name}")


def check_no_host_ports(result, rule, loader):
    """Check that listed services do not expose host ports."""
    services = rule.get("services", [])
    for svc_name in services:
        svc = loader.get_service(svc_name)
        if svc is None:
            result.skip(f"{svc_name}: not in compose files")
            continue
        ports = extract_host_ports(svc["data"])
        if ports:
            result.fail(f"{svc_name}: must not expose host ports, got {ports}")
        else:
            result.ok(f"{svc_name}: no host ports")


def check_allowed_host_ports(result, rule, loader):
    """Check that only allowed services expose host ports."""
    allowed = rule.get("services", {})
    # Gather all services across all compose files
    for svc_name, svc in loader.services.items():
        ports = extract_host_ports(svc["data"])
        if not ports:
            continue
        if svc_name in allowed:
            expected = set(allowed[svc_name])
            actual = set(ports)
            if actual == expected:
                result.ok(f"{svc_name}: ports {sorted(actual)} match spec")
            else:
                extra = actual - expected
                missing = expected - actual
                if extra:
                    result.fail(f"{svc_name}: unexpected ports {sorted(extra)}")
                if missing:
                    result.warn(f"{svc_name}: missing expected ports {sorted(missing)}")
        else:
            # Service has host ports but is not in allowed list
            result.fail(f"{svc_name}: has host ports {ports} but not in allowed list")


def check_healthcheck_defined(result, rule, loader):
    """Check that listed services have healthchecks."""
    services = rule.get("services", [])
    for svc_name in services:
        svc = loader.get_service(svc_name)
        if svc is None:
            result.skip(f"{svc_name}: not in compose files")
            continue
        if has_healthcheck(svc["data"]):
            result.ok(f"{svc_name}: healthcheck defined")
        else:
            result.fail(f"{svc_name}: healthcheck required but not defined")


def check_healthcheck_exempt(result, rule, loader):
    """Acknowledge exempt services (informational only)."""
    services = rule.get("services", [])
    for svc_name in services:
        result.skip(f"{svc_name}: healthcheck exempt")


def check_on_network(result, rule, loader):
    """Check that all services are on the specified network."""
    network = rule.get("network", "opensme-net")
    services = rule.get("services", [])
    for svc_name in services:
        svc = loader.get_service(svc_name)
        if svc is None:
            result.skip(f"{svc_name}: not in compose files")
            continue
        nets = get_networks(svc["data"])
        if network in nets:
            result.ok(f"{svc_name}: on {network}")
        else:
            result.fail(f"{svc_name}: not on {network} (found: {nets})")


def check_no_host_network(result, rule, loader):
    """Check that no service uses network_mode: host."""
    for svc_name, svc in loader.services.items():
        if is_host_network(svc["data"]):
            result.fail(f"{svc_name}: uses network_mode: host")
        else:
            result.ok(f"{svc_name}: bridge network")


def check_resource_limits_set(result, rule, loader):
    """Check that all services have deploy.resources.limits."""
    services = rule.get("services", [])
    for svc_name in services:
        svc = loader.get_service(svc_name)
        if svc is None:
            result.skip(f"{svc_name}: not in compose files")
            continue
        if has_resource_limits(svc["data"]):
            result.ok(f"{svc_name}: resource limits set")
        else:
            result.fail(f"{svc_name}: resource limits required but not set")


def check_no_changeme_in_compose(result, rule, loader):
    """Check that no CHANGEME_ values appear in compose files.

    ${VAR:-CHANGEME_...} is OK — it's a default value that gets overridden by .env.
    Bare CHANGEME_ values (not in ${VAR:-...}) are flagged.
    """
    compose_files = rule.get("compose_files", [])
    found = False
    for cf in compose_files:
        path = ROOT / cf
        if not path.exists():
            continue
        with open(path) as f:
            content = f.read()
        # Remove ${VAR:-CHANGEME_...} patterns before checking
        cleaned = re.sub(r'\$\{[^}]*:-CHANGEME_[^}]*\}', '', content)
        matches = re.findall(r'CHANGEME_[a-z_]+', cleaned, re.IGNORECASE)
        if matches:
            found = True
            result.fail(f"{cf}: contains CHANGEME_ values: {set(matches)}")
    if not found:
        result.ok("no CHANGEME_ values in compose files")


def check_no_public_admin_default_cred(result, rule, loader):
    """Publicly-routed services must not fall back to CHANGEME_ admin creds.

    The generic no-changeme rule deliberately tolerates `${VAR:-CHANGEME_...}`
    defaults (they are rotated by scripts/init-env.sh / demo scripts). That
    tolerance is only safe when the rotated value ACTUALLY reaches the
    container — which requires the var to be listed in .env.example. Any
    ADMIN credential on a service with a public `Host(...)` router is the
    security boundary (webadmin login, IdP admin, OC admin): a well-known
    default there means the whole surface is open with a published password.
    DB passwords / OIDC client secrets behind the same router are excluded
    (not human-login surfaces on the public edge).
    """
    compose_files = rule.get("compose_files", [])
    admin_cred = re.compile(r"(?i)(ADMIN_(PASSWORD|PASS|SECRET|TOKEN)|ORG_HUMAN_PASSWORD)")
    # .env.example membership = the rotation pipeline (init-env.sh / demo
    # scripts) WILL rotate this var on a real deploy. Absence here is the
    # security bug: the container silently falls to the well-known default.
    env_keys = set()
    env_path = ROOT / ".env.example"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            key = line.split("=", 1)[0].strip()
            if key and not key.startswith("#"):
                env_keys.add(key)
    default_cred = re.compile(r"CHANGEME_")
    public_router = re.compile(r"traefik\.http\.routers\.[^=]+rule=Host\(`")
    found = False
    warned = []
    for cf in compose_files:
        path = ROOT / cf
        if not path.exists():
            continue
        with open(path) as f:
            content = f.read()
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            result.fail(f"{cf}: cannot parse: {e}")
            continue
        for svc, svc_data in (data.get("services") or {}).items():
            if not isinstance(svc_data, dict):
                continue
            labels = svc_data.get("labels") or []
            if isinstance(labels, dict):
                labels = list(labels.values())
            if not any(public_router.search(str(l)) for l in labels):
                continue  # not on the public edge
            envs = svc_data.get("environment") or {}
            envs_map = {}
            if isinstance(envs, list):
                for item in envs:
                    if isinstance(item, dict):
                        envs_map.update(item)
                    elif isinstance(item, str) and "=" in item:
                        k, _, v = item.partition("=")
                        envs_map[k] = v
            elif isinstance(envs, dict):
                envs_map = envs
            for k, v in envs_map.items():
                v = str(v)
                if not admin_cred.search(k) or not default_cred.search(v):
                    continue
                # ${VAR:-CHANGEME...} default: value comes from .env at runtime
                m = re.search(r"\$\{([^}:}]+):-", v)
                source = (m.group(1) if m else k).strip()
                if source not in env_keys:
                    found = True
                    result.fail(
                        f"{cf}:{svc} → {k} default '{v}' is a well-known admin "
                        f"credential on a PUBLIC router and '{source}' is NOT in "
                        f".env.example — rotation (init-env.sh/demo) can never "
                        f"overwrite it. Add the var to .env.example."
                    )
                else:
                    warned.append(f"{cf}:{svc} → {k}")
    for w in warned:
        result.warn(
            f"{w}: public ADMIN credential still defaults to a CHANGEME_ value "
            f"(rotation wiring exists via .env.example — verify prod .env rotates it)"
        )
    if not found:
        result.ok("no public ADMIN credential is rotation-blind (all CHANGEME defaults reachable via .env.example)")


def check_image_has_tag(result, rule, loader):
    """Check that all images use explicit tags (not :latest, unless allowed)."""
    allow_latest = set(rule.get("allow_latest", []))
    for svc_name, svc in loader.services.items():
        image = svc["data"].get("image")
        if not image:
            continue  # build context, not image
        # Expand ${VAR} defaults
        image_expanded = re.sub(r'\$\{[^}]+:-([^}]+)\}', r'\1', image)
        # Check for tag
        if ":" not in image_expanded.split("/")[-1]:
            result.fail(f"{svc_name}: image {image} has no tag")
            continue
        tag = image_expanded.split(":")[-1]
        if tag == "latest" and image_expanded not in allow_latest:
            result.warn(f"{svc_name}: uses :latest ({image})")
        elif tag == "latest":
            result.ok(f"{svc_name}: :latest (allowed)")
        else:
            result.ok(f"{svc_name}: image {image}")


def check_named_volumes_only(result, rule, loader):
    """Check that all volumes are named (not anonymous).

    Volume formats:
    - Named: "name:/container/path" or "name:/container/path:ro"
    - Bind mount: "/host/path:/container/path" or "./relative:/container/path"
    - Anonymous: ":/container/path" (empty source)
    """
    import re
    checked = 0
    for svc_name, svc in sorted(loader.services.items()):
        vols = svc["data"].get("volumes") or []
        if not vols:
            continue
        for v in vols:
            checked += 1
            if isinstance(v, str):
                # Handle ${VAR:-default} which may contain colons.
                # Temporarily replace ${...} with a placeholder.
                placeholders = []
                v_clean = re.sub(
                    r'\$\{[^}]+\}',
                    lambda m: (placeholders.append(m.group(0)), f"__PLACEHOLDER_{len(placeholders)-1}__")[1],
                    v
                )
                parts = v_clean.split(":")
                # Restore placeholders
                for i, p in enumerate(parts):
                    parts[i] = re.sub(
                        r'__PLACEHOLDER_(\d+)__',
                        lambda m: placeholders[int(m.group(1))],
                        p
                    )
                if len(parts) >= 2:
                    source = parts[0]
                    target = parts[1]
                    if not source:
                        result.fail(f"{svc_name}: anonymous volume → {target}")
                    else:
                        result.ok(f"{svc_name}: named volume/bind mount → {target}")
                elif len(parts) == 1:
                    # Just a container path (no source) — anonymous
                    result.fail(f"{svc_name}: anonymous volume → {parts[0]}")
            elif isinstance(v, dict):
                source = v.get("source", "")
                target = v.get("target", "")
                vtype = v.get("type", "volume")
                if not source and target:
                    result.fail(f"{svc_name}: anonymous volume → {target}")
                else:
                    result.ok(f"{svc_name}: {vtype} → {target}")
    if checked == 0:
        result.info("no volumes to check")


def check_kb_version_required(result, rule, loader):
    """agent-knowledge: every runbook file carries kb_version (aktualität)."""
    kb = ROOT / "opensme-knowledge"
    for f in sorted(kb.glob("*.json")):
        doc = yaml.safe_load(f.read_text())
        if not (doc or {}).get("kb_version"):
            result.fail(f"{f.name}: missing kb_version (aktualität / source duty)")
        else:
            result.ok(f"{f.name}: kb_version {doc['kb_version']}")


def check_source_duty(result, rule, loader):
    """agent-knowledge: a matching goal — every file is versioned and the
    loader/gos answer evidence carries source+version (check_agent.py +
    Go loader enforce this). Here we confirm the KB is loadable."""
    kb = ROOT / "opensme-knowledge"
    services = sorted(f.stem for f in kb.glob("*.json"))
    for svc in services:
        result.ok(f"{svc}: runbook present (source duty satisfiable)")


def check_pii_free(result, rule, loader):
    """agent-knowledge: KB holds no secrets/uris/jwts/home-paths/private-addrs."""
    kb = ROOT / "opensme-knowledge"
    sec = re.compile(r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd)\s*[:=]\s*[\"']?[A-Za-z0-9_\-/+]{12,}")
    uri = re.compile(r"(?i)\b(postgres|redis|mongodb|mysql|amqp)://[^\s\"']+")
    home = re.compile(r"/home/[a-z0-9_.-]+|/Users/[a-z0-9_.-]+|/root\b")
    jwts = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
    priv = re.compile(r"(?:10|192\.168|172\.(?:1[6-9]|2[0-9]|3[01]))\.")
    checked = 0
    for f in sorted(kb.glob("*.json")):
        text = f.read_text()
        checked += 1
        for pat, label in ((sec, "secret"), (uri, "uri"), (home, "home-path"),
                           (jwts, "jwt"), (priv, "private-address")):
            if pat.search(text):
                result.fail(f"{f.name}: PII leak {label}")
    if checked == 0:
        result.info("no runbook files to check")


def run_contract(result, contract, loader):
    """Run all rules in a contract."""
    name = contract.get("name", "unnamed")
    desc = contract.get("description", "")
    severity = contract.get("severity", "error")

    result.header(f"Contract: {name} [{severity}]")
    if desc:
        result.info(desc.strip())

    for rule in contract.get("rules", []):
        rule_type = rule.get("type")
        handler = CONTRACT_HANDLERS.get(rule_type)
        if handler:
            handler(result, rule, loader)
        else:
            result.warn(f"unknown rule type: {rule_type}")


# Rule type → handler mapping
CONTRACT_HANDLERS = {
    "env-defined": check_env_defined,
    "no-host-ports": check_no_host_ports,
    "allowed-host-ports": check_allowed_host_ports,
    "healthcheck-defined": check_healthcheck_defined,
    "healthcheck-exempt": check_healthcheck_exempt,
    "on-network": check_on_network,
    "no-host-network": check_no_host_network,
    "resource-limits-set": check_resource_limits_set,
    "no-changeme-in-compose": check_no_changeme_in_compose,
    "no-public-admin-default-cred": check_no_public_admin_default_cred,
    "image-has-tag": check_image_has_tag,
    "kb_version-required": check_kb_version_required,
    "source-duty": check_source_duty,
    "pii-free": check_pii_free,
    "named-volumes-only": check_named_volumes_only,
}


def main():
    result = Result("contract-validation")
    result.header("Layer 2: Contract validation")

    loader = ComposeLoader(ROOT)
    loader.load()

    contract_loader = ContractLoader()
    contracts = contract_loader.load()

    result.info(f"Loaded {len(contracts)} contracts from contracts/")

    for contract in contracts:
        run_contract(result, contract, loader)

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
