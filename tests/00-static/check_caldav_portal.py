#!/usr/bin/env python3
"""Layer 0 pins for the CalDAV/CardDAV integration contract (campaign C2/C3).

C2 — SOGo subscribes OpenCloud CalDAV/CardDAV as secondary sources; the
primary calendar/contacts stay in SOGo's own SQL backend.
C3 — the portal does not embed a second calendar/contacts UI: users reach
them through the WebMail and Calendar tiles. portal/web config.ts keeps the
endpoint helpers for direct DAV clients and for future integrations.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

SOGO = ROOT / "mail" / "sogo.yml"
EDGE = ROOT / "edge" / "opencloud.yml"
ENV = ROOT / ".env.example"
CONFIG = ROOT / "portal" / "web" / "src" / "lib" / "config.ts"
CATALOG = ROOT / "contracts" / "tiles.yml"
BACKLOG = ROOT / "docs" / "OPENCLOUD-CAMPAIGNS-SUMMARY.md"


def main():
    result = Result("caldav-portal")
    result.header("Layer 0: CalDAV/CardDAV portal integration contract")
    sogo = SOGO.read_text() if SOGO.exists() else ""
    edge = EDGE.read_text() if EDGE.exists() else ""
    env = ENV.read_text() if ENV.exists() else ""
    config = CONFIG.read_text() if CONFIG.exists() else ""
    catalog = CATALOG.read_text() if CATALOG.exists() else ""
    backlog = BACKLOG.read_text() if BACKLOG.exists() else ""

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    # C2 — SOGo talks to OpenCloud DAV as a secondary source.
    for key in ("SOGO_CALDAV_ENABLED", "SOGO_CALDAV_SERVER", "SOGO_CARDDAV_ENABLED",
                "SOGO_CARDDAV_SERVER"):
        check(key in sogo, f"C2 sogo.yml wires {key} to OpenCloud")
    for key in ("SOGO_CALDAV_ENABLED", "SOGO_CARDDAV_ENABLED"):
        check(f"{key}: ${{{key}:-true}}" in sogo,
              f"C2 {key} defaults to enabled (OpenCloud DAV source active)")
    for router in ("opencloud-caldav", "opencloud-carddav"):
        check(router in edge, f"C2 edge keeps the {router} router")
    for key in ("OPENCLOUD_CALDAV_URL", "OPENCLOUD_CARDDAV_URL", "OPENCLOUD_PRINCIPAL_URL"):
        check(key in env, f"C3 .env.example documents {key}")

    # C3 — endpoint helpers exist; catalog routes users to the owning tiles.
    for fn in ("computeCaldavUrl", "computeCarddavUrl"):
        check(f"export function {fn}" in config, f"C3 config.ts exposes {fn}")
    for service in ("webmail", "calendar"):
        check(f"id: {service}" in catalog, f"C3 catalog routes calendar/contacts via {service} tile")

    c2 = next((line for line in backlog.splitlines() if "**C2**:" in line), "")
    c3 = next((line for line in backlog.splitlines() if "**C3**:" in line), "")
    check(c2.startswith("- [x] **C2**:") and "SOGo" in c2,
          "C2 backlog records the SOGo DAV proxy as implemented")
    check(c3.startswith("- [x] **C3**:") and "WebMail" in c3,
          "C3 backlog records the tile-based portal integration as current")

    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
