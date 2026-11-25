#!/usr/bin/env python3
"""Layer 0 pins for cloud/SSO event producers (realtime-events slice 1)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT  # noqa: E402

PORTAL = ROOT / "portal" / "src"
MAIN = (PORTAL / "main.rs").read_text()
SESSION = (PORTAL / "session.rs").read_text()
LOGOUT = (PORTAL / "logout.rs").read_text()


def handler_block(src: str, fn: str) -> str:
    start = src.index(fn)
    nxt = src.find("\nasync fn ", start + 1)
    return src[start: nxt if nxt > 0 else len(src)]


def main() -> int:
    result = Result("event-producers")
    result.header("Layer 0: cloud and session event producers")

    def check(cond: bool, msg: str) -> None:
        (result.ok if cond else result.fail)(msg)

    cloud = {
        "handle_cloud_upload": ("cloud.uploaded", r'\{ "path": full, "name": name \}'),
        "handle_cloud_mkdir_post": ("cloud.mkdir", r'\{ "path": path \}'),
        "handle_cloud_delete_file": ("cloud.deleted", r'\{ "path": path \}'),
        "handle_cloud_share": ("cloud.shared", r'\{ "path": path, "url": url \}'),
    }
    for fn, (kind, payload) in cloud.items():
        block = handler_block(MAIN, fn)
        check(kind in block and ".event_bus" in block
              and ".publish(&events::Event::new(" in block,
              f"P1 {fn} publishes {kind} via EventBus")
        check(re.search(payload, block) is not None,
              f"P2 {fn} payload carries only path/name/url metadata")
        check("events::Visibility::Recipients(vec![session.sub.clone()])" in block,
              f"P3 {fn} scopes the event to the actor")
        check("Visibility::Team" not in block,
              f"P3 {fn} never uses Team visibility")

    callback = handler_block(SESSION, "pub async fn handle_callback")
    check('"session.login"' in callback and ".event_bus" in callback
          and "events::Visibility::Recipients(vec![user.sub.clone()])" in callback,
          "P4 OIDC callback publishes actor-scoped session.login")
    check(re.search(r'session\.(login|logout)[\s\S]{0,300}Visibility::Team', SESSION) is None,
          "P4 session events are never team-wide")

    check('"session.logout"' in LOGOUT
          and "session::verify_session(&config.session, token)" in LOGOUT
          and "events::Visibility::Recipients(vec![sub.clone()])" in LOGOUT,
          "P5 logout verifies the portal session before publishing session.logout")
    check(re.search(r'cloud\.(uploaded|mkdir|deleted|shared)[\s\S]{0,300}Visibility::Team', MAIN) is None,
          "P5 cloud events are never team-wide")
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
