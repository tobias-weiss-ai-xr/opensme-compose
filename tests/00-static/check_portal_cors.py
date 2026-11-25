#!/usr/bin/env python3
"""Layer 0 pins for the portal's same-origin CORS contract (campaign E1/E2).

E1 — the edge keeps an origin-scoped CORS middleware for direct DAV/OCS
     clients (edge/opencloud.yml: opencloud-cors).
E2 — the portal backend emits no permissive CORS headers: commit 4e5c491
     deliberately removed CorsLayer::allow_origin(Any) because a mis-exposed
     container port let any website read /api/* responses. The Next.js
     portal-web catch-all /api/* proxy is the single browser entry point; it
     forwards cookies/bodies and all methods, including OPTIONS.
E3 — the backlog records E2 as superseded by that proxy instead of asking
     for a new CORS-enabled backend route.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

MAIN = ROOT / "portal" / "src" / "main.rs"
PROXY = ROOT / "portal" / "web" / "src" / "app" / "api" / "[[...path]]" / "route.ts"
EDGE = ROOT / "edge" / "opencloud.yml"
BACKLOG = ROOT / "docs" / "OPENCLOUD-CAMPAIGNS-SUMMARY.md"


def main():
    result = Result("portal-cors")
    result.header("Layer 0: portal same-origin CORS contract validation")
    main = MAIN.read_text() if MAIN.exists() else ""
    proxy = PROXY.read_text() if PROXY.exists() else ""
    edge = EDGE.read_text() if EDGE.exists() else ""
    backlog = BACKLOG.read_text() if BACKLOG.exists() else ""

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    # E1 — direct browser DAV stays available with an explicit allow-list.
    check("opencloud-cors:" in edge and "accessControlAllowOriginList:" in edge,
          "E1 edge/opencloud.yml keeps an origin-scoped CORS middleware")
    for router in ("opencloud-webdav", "opencloud-ocs"):
        block = edge.split(router + ":", 1)[1].split("\n\n", 1)[0] if router + ":" in edge else ""
        check("opencloud-cors" in block,
              f"E1 {router} router uses the scoped opencloud-cors middleware")

    # E2 — no permissive CORS in the Rust portal (security fix 4e5c491).
    # The rationale comment itself names the removed call, so ignore it.
    code = "\n".join(line for line in main.splitlines() if not line.strip().startswith("//"))
    check(not any(t in code for t in (
        "CorsLayer", "allow_origin", "tower_http::cors", "Access-Control-Allow-Origin",
    )), "E2 Rust portal has no permissive CORS layer or header")
    check("NO permissive CORS layer" in main,
          "E2 same-origin rationale is documented next to build_router")

    # E2 — the same-origin catch-all is the single browser entry point.
    check(all(t in proxy for t in (
        '"cookie"', "export const OPTIONS = proxy;", "export const DELETE = proxy;",
        "export const PATCH = proxy;", "forwardToBackend",
    )), "E2 Next.js /api/* proxy forwards cookies, bodies and all methods")
    check("access-control-allow-origin" not in proxy.lower(),
          "E2 proxy does not widen the browser contract with CORS headers")

    # E3 — the backlog reflects the decision.
    e2 = next((line for line in backlog.splitlines() if "**E2**:" in line), "")
    check(e2.startswith("- [x] **E2**:") and "same-origin" in e2,
          "E3 backlog marks the CORS-proxy item superseded by the same-origin proxy")

    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
