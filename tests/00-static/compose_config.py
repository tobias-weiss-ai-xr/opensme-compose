#!/usr/bin/env python3
"""
tests/00-static/compose_config.py — Real `docker compose config` gate.

PyYAML is lenient in ways the actual Docker Compose engine (go-yaml) is not:
duplicate `<<` merge keys ("mapping key \\"<<\\" already defined"), empty-list
overrides of anchored lists (security_opt: []), and permissive types all pass
`yaml.safe_load` but abort every real `docker compose up`.

This test shells out to the REAL `docker compose config` CLI for every
plausible file-set / profile combination (mirroring the deployment matrix) and
fails if any of them fails to parse.

DEFENSE-IN-DEPTH (Z-BRACE): after each successful render the rendered env
values are also inspected for stray closing braces — a '}' in an env value
with no matching '{' is a literal leak from a nested ${VAR:-…${INNER:-…}}
pattern (bug class Z-BRACE: admin email once rendered as 'admin@…local}').
compose-config validates that the YAML parses, NOT that rendered env values
are clean, so this guard backstops that hole.

The test self-skips (warn) when the Docker CLI or compose v2 plugin is not
available (e.g. minimal CI). On workstations and the deploy host it must pass.

Usage:
    python3 tests/00-static/compose_config.py

Exit codes:
    0 = all combinations produce valid config (or docker unavailable)
    1 = at least one combination failed to parse
"""

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT


# One tuple per combination: (label, [compose files], [profiles], tier)
# Mirrors the ansible role's deployment matrix (standalone/invoice/paperless)
# plus per-service and per-tier views.
MATRIX = [
    ("base", ["docker-compose.yml"], ["standalone"], None),
    ("base+iam", ["docker-compose.yml", "idm/zitadel.yml"], ["standalone"], None),
    ("base+files", ["docker-compose.yml", "opencloud/opencloud.yml",
                    "opencloud/minio.yml"], ["standalone"], None),
    ("base+mail", ["docker-compose.yml", "mail/stalwart.yml", "mail/sogo.yml"],
     ["standalone"], None),
    ("base+invoice", ["docker-compose.yml", "services/invoice-ninja.yml"],
     ["invoice"], None),
    ("base+paperless", ["docker-compose.yml", "services/paperless.yml"],
     ["paperless"], None),
    ("base+ticketing", ["docker-compose.yml", "services/ticketing.yml"],
     ["ticketing"], None),
    ("base+cms", ["docker-compose.yml", "services/cms.yml"],
     ["cms"], None),
    ("base+store", ["docker-compose.yml", "services/store.yml"],
     ["store"], None),
    ("base+ai", ["docker-compose.yml", "services/ai.yml"],
     ["ai"], None),
    ("base+camunda", ["docker-compose.yml", "services/camunda.yml"],
     ["camunda"], None),
    ("base+agent", ["docker-compose.yml", "monitoring/dev-agent.yml"],
     ["standalone"], None),
    # Full deploy set for each RAM tier
    ("soho", ["docker-compose.yml", "idm/zitadel.yml",
              "opencloud/opencloud.yml", "opencloud/minio.yml",
              "mail/stalwart.yml", "mail/sogo.yml",
              "services/invoice-ninja.yml", "services/paperless.yml",
              "monitoring/dev-agent.yml", "profiles/soho.yml"],
     ["standalone", "invoice", "paperless"], "soho"),
    ("small", ["docker-compose.yml", "idm/zitadel.yml",
               "opencloud/opencloud.yml", "opencloud/minio.yml",
               "mail/stalwart.yml", "mail/sogo.yml",
               "services/invoice-ninja.yml", "services/paperless.yml",
               "monitoring/dev-agent.yml", "profiles/small.yml"],
     ["standalone", "invoice", "paperless"], "small"),
    ("medium", ["docker-compose.yml", "idm/zitadel.yml",
                "opencloud/opencloud.yml", "opencloud/minio.yml",
                "mail/stalwart.yml", "mail/sogo.yml",
                "services/invoice-ninja.yml", "services/paperless.yml",
                "monitoring/dev-agent.yml", "profiles/medium.yml"],
     ["standalone", "invoice", "paperless"], "medium"),
    # Sidecar: pull-through registry (outside the suite chain). Its required
    # var is a deployment secret, so the render env supplies a dummy (this
    # gate validates shape, not credentials).
    ("registry", ["registry/registry-compose.yml"], [], None),
]

# Extra env per MATRIX label (see run_config).
MATRIX_ENV: dict[str, dict[str, str]] = {
    "registry": {"REGISTRY_PROXY_PASSWORD": "dummy-for-config-validation-only"},
    # Operaton SSO secrets are deployment secrets deliberately absent from
    # .env.example — shape-only dummies let the base+camunda render gate pass.
    "base+camunda": {
        "OPERATON_OIDC_CLIENT_ID": "dummy-for-config-validation-only",
        "OPERATON_OIDC_CLIENT_SECRET": "dummy-for-config-validation-only",
        "ZITADEL_PAT": "dummy-for-config-validation-only",
    },
}


def docker_compose_available() -> bool:
    return shutil.which("docker") is not None


def has_compose_plugin() -> bool:
    try:
        out = subprocess.run(["docker", "compose", "version"],
                             capture_output=True, text=True, timeout=20)
        return out.returncode == 0
    except Exception:
        return False


def run_config(files: list[str], profiles: list[str],
               extra_env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Run `docker compose -f ... --profile ... config`.

    Validated against .env.example (the documented defaults) — in CI there
    is no .env, and fail-fast :? interpolations must resolve against the
    example file, not an ambient local .env.

    extra_env: additional environment variables for sidecar compose files
    whose required vars are deployment secrets (e.g. REGISTRY_PROXY_PASSWORD)
    that no example file carries. Only used for config rendering.

    Returns (ok, output). On success `output` is the FULL RENDERED compose
    config (needed by the Z-BRACE env-value guard); on failure it is the
    error text.
    """
    import os

    env_example = ROOT / ".env.example"
    cmd = ["docker", "compose"]
    if env_example.exists():
        cmd += ["--env-file", str(env_example)]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    for p in profiles:
        cmd += ["--profile", p]
    cmd += ["config"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=120, cwd=str(ROOT),
                             env={**os.environ, **(extra_env or {})})
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except Exception as e:
        return False, str(e)
    if out.returncode == 0:
        return True, out.stdout
    return False, (out.stderr or out.stdout).strip()


def stray_brace_in_env(label: str, rendered: str) -> list[str]:
    """Z-BRACE guard: return rendered env values containing a stray '}'.

    A stray closing brace from a nested ${VAR:-…${INNER:-…}} pattern renders a
    literal '}' into the container env value; compose-config validates only
    that the YAML parses, not that rendered env values are clean. This exact
    hole let the Z-BRACE bug (stray '}' in the zitadel admin email) slip
    through CI.

    Rule: an env value is a stray-brace leak when it is a string containing
    '}' but NOT '{'. Values containing both { and } are intentional templates
    and are allowed.

    Returns a list of 'service.VAR=value' strings (one grep away from the fix),
    or ['(could not parse rendered config: …)'] if rendering is unreadable.
    """
    try:
        cfg = yaml.safe_load(rendered)
    except Exception as exc:
        return [f"(could not parse rendered config for [{label}]: {exc})"]
    if not isinstance(cfg, dict):
        return []
    services = cfg.get("services")
    if not isinstance(services, dict):
        return []
    offenders = []
    for svc_name, svc in services.items():
        env = svc.get("environment") if isinstance(svc, dict) else None
        if not isinstance(env, dict):
            continue  # rendered environment is a mapping; skip non-mapping forms
        for var, value in env.items():
            if isinstance(value, str) and "}" in value and "{" not in value:
                offenders.append(f"{svc_name}.{var}={value}")
    return offenders


def main():
    result = Result("compose-config")
    result.header("Layer 0: real `docker compose config` gate")

    if not docker_compose_available():
        result.warn("Docker CLI not found — skipping the real compose-config "
                    "gate (install docker compose to enable it locally)")
        return result.summary()

    if not has_compose_plugin():
        result.warn("`docker compose` v2 plugin not found — skipping")
        return result.summary()

    ok_all = True
    for label, files, profiles, tier in MATRIX:
        missing = [f for f in files if not (ROOT / f).exists()]
        if missing:
            result.skip(f"[{label}] missing files {missing} (not in this "
                        "install profile)")
            continue
        ok, output = run_config(files, profiles, MATRIX_ENV.get(label))
        if ok:
            # Z-BRACE backstop: even a parse-clean config can leak a stray '}'
            # into a rendered env value; fail loudly if any do.
            offenders = stray_brace_in_env(label, output)
            if offenders:
                ok_all = False
                for off in offenders:
                    result.fail(f"[{label}] stray '}}' in rendered env value: "
                                f"{off}")
                continue
            tier_note = f" ({tier} tier)" if tier else ""
            result.ok(f"[{label}] compose config OK{tier_note}")
        else:
            ok_all = False
            first = output.splitlines()[0][:220] if output else "(no error output)"
            result.fail(f"[{label}] docker compose config FAILED: {first}")

    if ok_all:
        result.info("All file-set/profile combinations parse with the real "
                    "Docker Compose engine (go-yaml).")
    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
