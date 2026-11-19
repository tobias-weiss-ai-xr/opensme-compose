#!/usr/bin/env python3
"""
tests/00-static/check_tiles.py — Layer 0 validation of the portal function
catalog (contracts/tiles.yml).

The portal landing page is the SME function catalog. This check pins the
tile wiring contract:

  1. Pass-through: every catalog entry's url_env is forwarded to the portal
     service in docker-compose.yml (default empty = card hidden), and the
     removed COLLABORA_URL pass-through stays gone.
  2. Deployment truth: every catalog entry resolves to a compose service
     that carries the declared Traefik router (host rule referencing the
     documented domain env or OPENSME_DOMAIN prefix).
  3. Completeness: every Traefik-routed service in the stack is either a
     catalog function or on the exclusion list — no routed service may
     appear on the portal un-cataloged, and no tile may point to a
     non-routed service ("no tile to nowhere").
  4. Exclusion enforcement: identity/auth and collabora never come back as
     tiles in the portal source (spec: portal-catalog, infrastructure
     exclusion).

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_tiles.py
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

TILES_CONTRACT = ROOT / "contracts" / "tiles.yml"
COMPOSE_MAIN = ROOT / "docker-compose.yml"
PORTAL_SRC = ROOT / "portal" / "src" / "main.rs"
WEB_CONFIG = ROOT / "portal" / "web" / "src" / "lib" / "config.ts"
ENV_EXAMPLE = ROOT / ".env.example"

# Catalog ids rendered via a dedicated component instead of the standard
# buildServices tile loop. 'ai' is the AI-assistant card (AiCard inside
# PortalShell) — it must exist as a component, not as a catalog entry.
WEB_TILES_SPECIAL = {"ai": "components/PortalShell.tsx:<AiCard />"}

# All compose files that may carry routed services (profiles excluded —
# they are overrides, not new surfaces).
COMPOSE_GLOBS = [
    ROOT / "docker-compose.yml",
    *sorted((ROOT / "idm").glob("*.yml")),
    *sorted((ROOT / "mail").glob("*.yml")),
    *sorted((ROOT / "opencloud").glob("*.yml")),
    *sorted((ROOT / "services").glob("*.yml")),
]


def load_labels(service_def):
    """Extract labels as a flat string, tolerating dict and list form."""
    labs = service_def.get("labels")
    if labs is None:
        labs = (service_def.get("deploy") or {}).get("labels")
    if isinstance(labs, dict):
        return " ".join(str(k) for k in labs)
    if isinstance(labs, list):
        return " ".join(str(x) for x in labs)
    return ""


def routed_services():
    """Map service name -> set of traefik router names across the stack."""
    routed = {}
    for path in COMPOSE_GLOBS:
        if not path.exists():
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for name, svc in (doc or {}).get("services", {}).items():
            if not isinstance(svc, dict):
                continue
            labels = load_labels(svc)
            routers = set(re.findall(r"routers\.([a-z0-9-]+)\.rule", labels))
            if routers:
                entry = routed.setdefault(name, {"routers": set(), "labels": ""})
                entry["routers"] |= routers
                entry["labels"] += " " + labels
    return routed


def web_catalog():
    """Parse the Next.js frontend catalog from lib/config.ts.

    Returns (specs, order) where specs maps key -> URL env var name
    (loadServiceSpecs) and order is a list of (id, name, specs_key)
    tuples from the buildServices order array.
    """
    text = WEB_CONFIG.read_text()

    # loadServiceSpecs return object: 'key: envOr("ENV_NAME", ...)' lines
    specs = dict(
        re.findall(r'(\w+):\s*envOr\("([A-Z_0-9]+)"', text)
    )

    # buildServices order entries, e.g.
    # { id: "opencloud", name: "OpenCloud", description: "...",
    #   url: specs.opencloud }
    order = re.findall(
        r'\{\s*id:\s*"(\w+)",\s*name:\s*"([^"]+)",'
        r'\s*description:\s*"[^"]*",\s*url:\s*specs\.(\w+)\s*\}',
        text,
    )
    return specs, order


def main():
    result = Result("portal-function-catalog")
    result.header("Layer 0: portal function-catalog (tiles) validation")

    failures = []

    contract = yaml.safe_load(TILES_CONTRACT.read_text())
    catalog = contract.get("catalog", [])
    excluded = {e["id"] for e in contract.get("excluded", [])}
    routed = routed_services()

    # ── 1. Portal env pass-through ─────────────────────────────────────
    main_doc = yaml.safe_load(COMPOSE_MAIN.read_text())
    portal_env = (main_doc["services"]["portal"].get("environment") or {})
    portal_env_keys = set(portal_env.keys())

    for entry in catalog:
        if entry["url_env"] not in portal_env_keys:
            failures.append(
                f"tile '{entry['id']}': {entry['url_env']} is not passed "
                "through to the portal service"
            )
        elif entry.get("default_url"):
            # core functions derive their URL from a domain env and are
            # always present (never empty-hidden)
            if entry["domain_env"] not in str(portal_env[entry["url_env"]]):
                failures.append(
                    f"tile '{entry['id']}': {entry['url_env']} must derive "
                    f"from ${entry['domain_env']}"
                )
        else:
            want = f"${{{entry['url_env']}:-}}"
            if portal_env[entry["url_env"]] != want:
                failures.append(
                    f"tile '{entry['id']}': portal env {entry['url_env']} "
                    f"must default to empty ({want}), "
                    f"got {portal_env[entry['url_env']]!r}"
                )

    if "COLLABORA_URL" in portal_env_keys:
        failures.append(
            "portal still receives COLLABORA_URL — Collabora is an embedded "
            "editor and must not be wired as a tile env (use COLLABORA_DOMAIN "
            "for routing only)"
        )

    # ── 2. Catalog entries resolve to routed services ─────────────────
    for entry in catalog:
        # External tiles point at a service outside the compose chain
        # (e.g. group-managed Grist) — there is nothing to route-check.
        if entry.get("external"):
            if "service" in entry or "router" in entry:
                failures.append(
                    f"tile '{entry['id']}': external tiles must not "
                    "declare a compose service/router"
                )
            continue
        svc = entry["service"]
        router = entry["router"]
        info = routed.get(svc)
        if info is None:
            failures.append(
                f"tile '{entry['id']}': service '{svc}' has no Traefik "
                "router — tile would point to nowhere"
            )
            continue
        if router not in info["routers"]:
            failures.append(
                f"tile '{entry['id']}': service '{svc}' lacks router "
                f"'{router}' (has: {sorted(info['routers'])})"
            )
        elif entry.get("domain_env"):
            if entry["domain_env"] not in info["labels"]:
                failures.append(
                    f"tile '{entry['id']}': router '{router}' does not "
                    f"reference ${entry['domain_env']}"
                )
        elif entry.get("host_prefix"):
            if f"{entry['host_prefix']}." not in info["labels"]:
                failures.append(
                    f"tile '{entry['id']}': router '{router}' host does not "
                    f"use the documented '{entry['host_prefix']}.' prefix"
                )

    # ── 3. Completeness: routed ⊆ catalog ∪ excluded ──────────────────
    catalog_services = {e["service"] for e in catalog if not e.get("external")}
    unknown = sorted(set(routed) - catalog_services - excluded)
    if unknown:
        failures.append(
            "routed services missing from tiles.yml (add to catalog or "
            f"excluded): {', '.join(unknown)}"
        )

    # ── 4. Infrastructure exclusion in portal source ───────────────────
    src = PORTAL_SRC.read_text()
    if re.search(r'"identity"\s*=>', src) or "collabora_url" in src:
        failures.append(
            "portal source still references identity/collabora tiles — "
            "infrastructure must never be cataloged"
        )
    env_ex = ENV_EXAMPLE.read_text()
    if re.search(r"^COLLABORA_URL=", env_ex, re.M):
        failures.append(".env.example still declares COLLABORA_URL")

    # ── 5. Web frontend catalog parity (tiles.yml ↔ config.ts) ────────
    # The live landing is rendered by the Next.js frontend (portal-web);
    # its catalog must mirror the contract or tiles silently disappear
    # (2026-11-19: calendar/vc were absent while contract, Rust catalog
    # and compose env were complete). Parity is resolved via the URL env:
    # order entry → specs key → envOr(...) env name must equal the
    # contract's url_env (spec keys may differ from tile ids — 'mail',
    # 'bpm', 'paperless', 'ticketing', 'cms' are legacy keys).
    web_specs, web_order = web_catalog()
    env_by_id: dict[str, str] = {}
    name_by_id: dict[str, str] = {}
    for tid, name, specs_key in web_order:
        env_by_id[tid] = web_specs.get(specs_key, "")
        name_by_id[tid] = name
    for entry in catalog:
        tid = entry["id"]
        if tid in WEB_TILES_SPECIAL:
            continue  # dedicated render path (AiCard), no catalog entry
        if tid not in env_by_id:
            failures.append(
                f"tile '{tid}': missing from web catalog buildServices "
                "order — the live landing would never render it"
            )
            continue
        if env_by_id[tid] != entry["url_env"]:
            failures.append(
                f"tile '{tid}': web catalog reads "
                f"{env_by_id[tid] or '<unset>'} but the contract pins "
                f"{entry['url_env']}"
            )
        if name_by_id[tid] != entry.get("tile"):
            failures.append(
                f"tile '{tid}': web name {name_by_id[tid]!r} != contract "
                f"tile name {entry.get('tile')!r}"
            )
    # unknown web tiles: the reverse direction of the "no tile to nowhere"
    # rule — the web catalog must not invent functions the contract lacks
    contract_ids = {e["id"] for e in catalog} | set(WEB_TILES_SPECIAL)
    contract_envs = {e["url_env"] for e in catalog}
    phantom = sorted(
        tid for tid in env_by_id
        if tid not in contract_ids and env_by_id[tid] not in contract_envs
    )
    if phantom:
        failures.append(
            "web catalog renders tiles unknown to contracts/tiles.yml: "
            f"{', '.join(phantom)}"
        )
    # every spec key must be consumed by an order entry (no dead specs)
    used_keys = {specs_key for _, _, specs_key in web_order}
    dead_keys = sorted(set(web_specs) - used_keys)
    if dead_keys:
        failures.append(
            "web loadServiceSpecs defines specs no order entry consumes: "
            f"{', '.join(dead_keys)}"
        )

    if failures:
        for f in failures:
            result.fail(f)
        result.info(
            f"catalog: {len(catalog)} functions, {len(excluded)} excluded, "
            f"{len(routed)} routed services in stack"
        )
    else:
        result.ok(
            f"catalog complete: {len(catalog)} tiles ↔ routed services, "
            f"{len(excluded)} infra surfaces excluded, COLLABORA_URL gone, "
            f"web catalog parity ok ({len(web_order)} tiles + "
            f"{len(WEB_TILES_SPECIAL)} special)"
        )
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
