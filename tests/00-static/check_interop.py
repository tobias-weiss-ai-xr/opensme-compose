#!/usr/bin/env python3
"""
tests/00-static/check_interop.py — Layer 0 validation of cross-service
interoperability (contracts/interop.yml).

Drift between consumer and provider breaks user journeys that static
layers cannot see (they need a running stack). This check pins the
DECLARED integration surfaces to the files that implement them:

  1. OIDC: both IdM swap points exist; the portal takes its IdP from IDP_URL.
  2. SMTP: every declared mail consumer defaults to the stalwart provider.
  3. Feeds: feeds.rs admits http(s) only; PORTAL_FEEDS reaches the portal.
  4. Share links: INTERCOM_ATTACHMENT_HOSTS is wired, defaulting to the
     cloud domain; the portal never proxies attachments.
  5. Processes: portal gets REST-only BPM_URL — no engine DB credentials
     anywhere in the portal env; BPMN bootstrap artifacts exist.
  6. Document flow: invoice storage and paperless export volumes exist.

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_interop.py
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT  # noqa: E402

INTEROP = ROOT / "contracts" / "interop.yml"
COMPOSE_MAIN = ROOT / "docker-compose.yml"


def main():
    result = Result("sme-interoperability")
    result.header("Layer 0: cross-service interoperability validation")

    failures = []
    contract = yaml.safe_load(INTEROP.read_text())
    surfaces = contract["surfaces"]
    main_doc = yaml.safe_load(COMPOSE_MAIN.read_text())
    portal_env = set((main_doc["services"]["portal"].get("environment") or {}).keys())

    def env_of(compose_file: Path) -> dict:
        doc = yaml.safe_load((ROOT / compose_file).read_text())
        merged = {}
        for svc in (doc or {}).get("services", {}).values():
            if isinstance(svc, dict) and isinstance(svc.get("environment"), dict):
                merged.update(svc["environment"])
        return merged

    # ── 1. OIDC: IdM swap points + portal wiring ───────────────────────
    oidc = surfaces["oidc"]
    for idm in oidc["idm_services"]:
        if not (ROOT / "idm" / f"{idm}.yml").exists():
            failures.append(f"IdM swap point missing: idm/{idm}.yml")
    if oidc["portal_env"] not in portal_env:
        failures.append(f"portal env lacks {oidc['portal_env']} (IdP wiring)")

    # ── 2. SMTP: consumers default to the provider ─────────────────────
    smtp = surfaces["smtp_submission"]
    provider = smtp["provider"]
    for c in smtp["consumers"]:
        env = env_of(Path(c["env_file"]))
        val = env.get(c["env"], "")
        if provider not in str(val):
            failures.append(
                f"mail consumer {c['service']}: {c['env']} does not default "
                f"to '{provider}' (got {val!r})"
            )

    # ── 3. Feeds: scheme allow-list + portal env ───────────────────────
    feeds = surfaces["feeds"]
    if feeds["portal_env"] not in portal_env:
        failures.append(f"portal env lacks {feeds['portal_env']}")
    feeds_src = (ROOT / "portal" / "src" / "feeds.rs").read_text()
    if not re.search(
        r'starts_with\("https://"\)\s*\|\|\s*\w+\.starts_with\("http://"\)', feeds_src
    ):
        failures.append("feeds.rs does not admit http(s) URLs only (scheme allow-list drifted)")

    # ── 4. Share links: attachment host wiring ─────────────────────────
    shares = surfaces["share_links"]
    if shares["portal_env"] not in portal_env:
        failures.append(f"portal env lacks {shares['portal_env']}")
    intercom_src = (ROOT / "portal" / "src" / "intercom.rs").read_text()
    if shares["portal_env"] not in intercom_src:
        failures.append("intercom.rs no longer honors INTERCOM_ATTACHMENT_HOSTS")
    # No undeclared file-content routes: portal routes are navigation/API
    # only. Declared exceptions live in contracts/interop.yml
    # (share_links.picked_file_routes — the picked-file local store).
    main_src = (ROOT / "portal" / "src" / "main.rs").read_text()
    routes = re.findall(r'\.route\(\s*"([^"]+)"', main_src, re.DOTALL)
    declared = [r.rstrip("/") for r in shares.get("picked_file_routes", [])]

    def is_declared(route: str) -> bool:
        return any(route == d or route.startswith(d + "/") for d in declared)

    fileish = [
        r
        for r in routes
        if re.search(r"/s/|attachment|file|share", r) and not is_declared(r.rstrip("/"))
    ]
    if fileish:
        failures.append(
            f"portal serves undeclared file-content routes (must link, "
            f"not mediate; declare in contracts/interop.yml): {fileish}"
        )

    # ── 5. Processes: REST-only + BPMN bootstrap ───────────────────────
    proc = surfaces["processes"]
    if proc["portal_env"] not in portal_env:
        failures.append(f"portal env lacks {proc['portal_env']} (engine REST wiring)")
    dbish = [k for k in portal_env if re.search(r"DB|DATABASE|SQL", k) and "camunda" in k.lower()]
    if dbish:
        failures.append(f"portal holds engine DB credentials: {dbish}")
    for bpmn in proc["bootstrap"]:
        if not (ROOT / bpmn).exists():
            failures.append(f"BPMN bootstrap artifact missing: {bpmn}")

    # ── 6. Document flow volumes ───────────────────────────────────────
    ninja_env = env_of(Path("services/invoice-ninja.yml"))
    paper_doc = yaml.safe_load((ROOT / "services" / "paperless.yml").read_text())
    paper_vols = " ".join(
        " ".join(s.get("volumes") or []) for s in paper_doc["services"].values()
    )
    if "invoiceninja-storage" not in str(env_of(Path("services/invoice-ninja.yml"))) and \
       "invoiceninja-storage" not in (ROOT / "services" / "invoice-ninja.yml").read_text():
        failures.append("invoice storage volume (document source) missing")
    if "paperless-export" not in paper_vols:
        failures.append("paperless export volume (DMS ingest sink) missing")

    if failures:
        for f in failures:
            result.fail(f)
    else:
        result.ok(
            "interoperability surfaces declared and implemented: "
            "OIDC swap points, stalwart SMTP consumers, http(s) feeds, "
            "share-link hosts, REST-only engine + BPMN bootstrap, "
            "document-flow volumes"
        )
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
