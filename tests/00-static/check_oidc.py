#!/usr/bin/env python3
"""
tests/00-static/check_oidc.py — Layer 0 validation of the Zitadel OIDC
client registration contract.

A service that logs in through the central IdP (Zitadel) needs a REGISTERED
OIDC client app. When that client is missing the login dead-ends: the 2026-10
OpenCloud outage (infinite loading screen at cloud.home.opensme.graphwiz.ai)
was exactly this — authorize → 400 Errors.App.NotFound because env.j2 never
set OC_OIDC_CLIENT_ID and no app 'opencloud' existed in Zitadel.

This check pins the registration contract statically so the same drift is
caught on a fresh checkout, without a running stack:

  R1 — Registration coverage: every OIDC client-id variable referenced in a
       compose service must have a matching --app branch in
       scripts/zitadel-client.sh (the idempotent registrar that persists
       the Zitadel-minted pair into .env).
  R2 — Env documentation: every OIDC variable referenced by compose must be
       declared in .env.example. (check_env.py skips ${VAR:-default} forms,
       which is exactly how OC_OIDC_CLIENT_ID slipped through.)
  R3 — No phantom defaults: a non-empty ${VAR:-DEFAULT} fallback for a
       client-id must equal the APP_NAME of the branch that owns VAR — a
       made-up default (like 'opencloud') is a client that can never log in.

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_oidc.py
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

ZITADEL_CLIENT_SH = ROOT / "scripts" / "zitadel-client.sh"
ENV_EXAMPLE = ROOT / ".env.example"

# Compose files that may carry OIDC client references (base + optionals).
COMPOSE_CANDIDATES = [
    "docker-compose.yml",
    "idm/zitadel.yml",
    "idm/casdoor.yml",
    "opencloud/opencloud.yml",
    "services/ticketing.yml",
    "services/notes.yml",
    "services/synapse.yml",
]


# ── Parsing helpers ─────────────────────────────────────────────

def extract_oidc_vars_from_compose(compose_path: Path) -> dict:
    """Return {var_name: default_or_None} for OIDC client references in a
    service env, e.g. OC_OIDC_CLIENT_ID → 'opencloud'.

    Only variables whose name clearly marks an OIDC client credential are
    collected (OIDC containing 'CLIENT', the RP alias, issuer is informational
    and excluded).
    """
    out: dict[str, str | None] = {}
    if not compose_path.exists():
        return out
    data = yaml.safe_load(compose_path.read_text())
    if not data:
        return out

    env_ref = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}")

    def is_client_var(name: str) -> bool:
        n = name.upper()
        if "OIDC" not in n:
            return False
        return ("CLIENT" in n) or n in ("OIDC_RP_CLIENT_ID", "OIDC_RP_CLIENT_SECRET")

    for svc in (data.get("services") or {}).values():
        if not svc:
            continue
        env = svc.get("environment") or {}
        # compose allows a list form ["KEY=value", ...] or a mapping
        env_items = env.items() if isinstance(env, dict) else [(e.split("=", 1)[0], e.split("=", 1)[1] if "=" in e else "") for e in env]
        for _, val in env_items:
            if not isinstance(val, str):
                continue
            for m in env_ref.finditer(val):
                var, default = m.group(1), m.group(2)
                if is_client_var(var):
                    # first occurrence wins (base file order)
                    out.setdefault(var, default or None)
    return out


def extract_app_branches(shell_path: Path) -> dict:
    """Parse scripts/zitadel-client.sh into {app_name: {APP_NAME, CLIENT_ID_KEY,
    CLIENT_SECRET_KEY, APP_PROJECT}} for every --app branch."""
    branches: dict[str, dict] = {}
    if not shell_path.exists():
        return branches
    lines = shell_path.read_text().splitlines()

    # A branch starts at a case-arm line like "  opencloud)" followed by
    # APP_NAME= / CLIENT_ID_KEY= / CLIENT_SECRET_KEY= assignments.
    arm_re = re.compile(r"^  ([a-z][a-z0-9_-]*)\)\s*$")
    assign_re = re.compile(r'^\s{4}(APP_NAME|CLIENT_ID_KEY|CLIENT_SECRET_KEY|APP_PROJECT)="([^"]*)"')

    current: str | None = None
    for ln in lines:
        m = arm_re.match(ln)
        if m:
            current = m.group(1)
            branches[current] = {}
            continue
        if current is None:
            continue
        a = assign_re.match(ln)
        if a:
            branches[current][a.group(1)] = a.group(2)
        # stop collecting once we leave the case arm assignments
        if a is None and "base_paths=" in ln:
            current = None
    # keep only branches that declare an OIDC client id key
    return {name: cfg for name, cfg in branches.items() if "CLIENT_ID_KEY" in cfg}


def env_example_vars(env_path: Path) -> set[str]:
    if not env_path.exists():
        return set()
    return set(
        re.findall(r"^([A-Z][A-Z0-9_]*)\s*[:=]", env_path.read_text(), re.M)
    )


# ── Checks ─────────────────────────────────────────────────────

def main() -> bool:
    result = Result("sme-oidc-clients")
    result.header(
        "Layer 0: Zitadel OIDC client registration contract "
        "(all services that log in through Zitadel need a registered app)"
    )

    branches = extract_app_branches(ZITADEL_CLIENT_SH)
    result.info(
        f"zitadel-client.sh --app branches: {sorted(branches)}"
    )

    # Collect every OIDC client-id var referenced anywhere in the compose set.
    referenced: dict[str, str | None] = {}
    for rel in COMPOSE_CANDIDATES:
        referenced.update(extract_oidc_vars_from_compose(ROOT / rel))

    if not referenced:
        result.fail("no OIDC client references found — is the compose set stale?")
        return result.summary()

    env_vars = env_example_vars(ENV_EXAMPLE)

    # Map CLIENT_ID_KEY → owning branch for the registration-coverage check.
    id_key_owner: dict[str, str] = {}
    for app, cfg in branches.items():
        k = cfg.get("CLIENT_ID_KEY")
        if k:
            id_key_owner[k] = app

    # ── R2: every referenced OIDC var must be documented in .env.example ──
    missing_doc = sorted(v for v in referenced if v not in env_vars)
    if missing_doc:
        for var in missing_doc:
            result.fail(
                f"{var} referenced by compose but NOT declared in .env.example "
                "(add a placeholder line so deployers know to set it)"
            )
    else:
        result.ok("all referenced OIDC vars are declared in .env.example")

    # ── R1: every client-id var needs a registration branch ──
    id_refs = sorted(v for v in referenced if v.endswith("CLIENT_ID") or v in ("OIDC_RP_CLIENT_ID",))
    orphan = sorted(v for v in id_refs if v not in id_key_owner)
    if orphan:
        for var in orphan:
            result.fail(
                f"{var} has no --app branch in scripts/zitadel-client.sh — "
                "this service can never complete a Zitadel login"
            )
    else:
        result.ok(f"all {len(id_refs)} OIDC client-id vars have a registration branch")

    # ── R3: a non-empty client-id default must match the branch APP_NAME ──
    phantom = []
    for var in id_refs:
        default = referenced.get(var)
        if not default:
            continue  # empty fallback = explicit opt-in, fine
        owner = id_key_owner.get(var)
        if owner is None:
            continue  # R1 already failed this one
        app_name = branches[owner].get("APP_NAME", "")
        if default != app_name:
            phantom.append(f"{var} (default '{default}' != branch '{owner}' APP_NAME '{app_name}')")
    if phantom:
        for p in phantom:
            result.fail(
                f"phantom OIDC client default: {p} — this default is not a "
                "registered client and users will hit Errors.App.NotFound"
            )
    else:
        result.ok("no phantom (unregistered) client-id defaults")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
