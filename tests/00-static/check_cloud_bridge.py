#!/usr/bin/env python3
"""Layer 0 pins for portal cloud-file bridge mutations (D1-D5/B3).

C1 — mkdir and path-delete routes are registered.
C2 — both routes are inside the session firewall.
C3 — handlers validate the caller path and scope to the session user.
C4 — all four cloud error helpers are used (no raw upstream leaks).
C5 — the Next.js catch-all proxy forwards cookies and raw bodies.
C6 — CloudFileBrowser calls both endpoints with encoded paths.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

MAIN = ROOT / "portal" / "src" / "main.rs"
WEB = ROOT / "portal" / "web" / "src" / "components" / "CloudFileBrowser.tsx"
PROXY = ROOT / "portal" / "web" / "src" / "app" / "api" / "[[...path]]" / "route.ts"


def main():
    result = Result("cloud-bridge")
    result.header("Layer 0: portal cloud-file bridge validation")
    main = MAIN.read_text() if MAIN.exists() else ""
    web = WEB.read_text() if WEB.exists() else ""
    proxy = PROXY.read_text() if PROXY.exists() else ""

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    mkdir_route = '.route("/api/cloud/files/mkdir", post(handle_cloud_mkdir_post))'
    delete_route = '.route("/api/cloud/files/*path", delete(handle_cloud_delete_file))'
    check(mkdir_route in main, "C1 mkdir route registered")
    check(delete_route in main, "C1 path-delete route registered")

    firewall = main.find("session::require_session")
    for label, needle in (("mkdir", mkdir_route), ("delete", delete_route)):
        pos = main.find(needle)
        check(pos >= 0 and firewall >= 0 and pos < firewall,
              f"C2 {label} route is inside the session firewall")

    for name in ("handle_cloud_mkdir_post", "handle_cloud_delete_file"):
        start = main.find(f"async fn {name}")
        body = main[start:start + 1100] if start >= 0 else ""
        check("cloud::validate_path" in body,
              f"C3 {name} validates the caller path")
        check("cloud.for_user(session.sub.clone())" in body,
              f"C3 {name} scopes the DAV client to the session user")

    for helper in ("cloud_bad", "cloud_error", "cloud_mkdir_error", "cloud_not_configured"):
        check(helper in main, f"C4 {helper} used for error mapping")

    check(all(t in proxy for t in ('"cookie"', "req.arrayBuffer()", "forwardToBackend")),
          "C5 catch-all proxy forwards cookie and raw body")
    check(all(t in web for t in ("/api/cloud/files/mkdir", "/api/cloud/files/",
                                 "encodeURIComponent")),
          "C6 browser calls mkdir and path-delete with encoded paths")

    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
