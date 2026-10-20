#!/usr/bin/env python3
"""
tests/00-static/check_env.py — Environment variable completeness checker.

Verifies that every env var referenced in compose files is defined in .env.example.
This prevents runtime failures from missing variables.

Usage:
    python3 tests/00-static/check_env.py [.env.example] [compose-file1 ...]

Exit codes:
    0 = all env vars defined
    1 = missing env vars found
"""

import sys
import yaml
from pathlib import Path

# Add parent to path for conftest import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import EnvLoader, Result, ROOT, extract_env_vars_from_compose


def extract_env_vars_from_file(filepath: Path) -> set[str]:
    """Extract env var names that MUST be in .env.example from a compose file.

    Only extracts vars referenced via ${VAR} (without default).
    Vars with hardcoded values or defaults (${VAR:-default}) are skipped.
    """
    if not filepath.exists():
        return set()

    with open(filepath) as f:
        data = yaml.safe_load(f)

    if not data:
        return set()

    env_vars = set()

    for svc_name, svc_data in (data.get("services") or {}).items():
        if not svc_data:
            continue
        env_vars |= extract_env_vars_from_compose(svc_data)

    return env_vars


def main():
    result = Result("env-completeness")
    result.header("Layer 0: Environment variable completeness")

    # Determine env file
    env_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".env.example"
    if not env_file.exists():
        result.fail(f".env.example not found at {env_file}")
        return result.summary()

    # Determine compose files
    if len(sys.argv) > 2:
        compose_files = [Path(f) for f in sys.argv[2:]]
    else:
        compose_files = [
            ROOT / "docker-compose.yml",
            ROOT / "idm/zitadel.yml",
            ROOT / "idm/casdoor.yml",
            ROOT / "opencloud/opencloud.yml",
            ROOT / "opencloud/minio.yml",
            ROOT / "mail/stalwart.yml",
            ROOT / "mail/sogo.yml",
            ROOT / "services/invoice-ninja.yml",
            ROOT / "services/paperless.yml",
            ROOT / "services/cryptpad.yml",
            ROOT / "services/synapse.yml",
            ROOT / "services/element.yml",
            ROOT / "services/notes.yml",
            ROOT / "services/camunda.yml",
            ROOT / "services/ticketing.yml",
            ROOT / "services/cms.yml",
            ROOT / "services/store.yml",
            ROOT / "services/ai.yml",
            ROOT / "services/limesurvey.yml",
            ROOT / "monitoring/dev-agent.yml",
            ROOT / "monitoring/predictive-agent.yml",
            ROOT / "monitoring/ollama.yml",
            ROOT / "monitoring/taskfleet.yml",
        ]

    # Load env vars from .env.example
    env_loader = EnvLoader(env_file)
    defined_vars = env_loader.all_vars()
    result.info(f"Found {len(defined_vars)} env vars in {env_file.name}")

    # Meta-variables that are not service env vars
    ignore = {"COMPOSE_FILE", "OPENSME_DOMAIN", "EXISTING_NETWORK", "EXISTING_TRAEFIK_HTTPS"}

    # Check each compose file
    all_missing = []
    for cf in compose_files:
        if not cf.exists():
            result.skip(f"{cf.name} not found")
            continue
        vars_in_file = extract_env_vars_from_file(cf)
        missing = vars_in_file - defined_vars - ignore
        if missing:
            for var in sorted(missing):
                all_missing.append(f"{cf.name}: {var}")
                result.fail(f"{cf.name} → {var} not in .env.example")
        else:
            if vars_in_file:
                result.ok(f"{cf.name} ({len(vars_in_file)} vars)")

    if not all_missing:
        result.info("All env vars are defined in .env.example")
    else:
        result.info(f"{len(all_missing)} missing env vars")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
