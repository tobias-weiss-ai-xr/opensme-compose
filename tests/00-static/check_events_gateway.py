#!/usr/bin/env python3
"""Layer 0 pins for the opt-in standalone events gateway."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT  # noqa: E402


def text(path: str) -> str:
    file = ROOT / path
    return file.read_text() if file.exists() else ""


def main() -> int:
    result = Result("events-gateway")
    result.header("Layer 0: opt-in standalone events gateway")

    def check(cond: bool, msg: str) -> None:
        if cond:
            result.ok(msg)
        else:
            result.fail(msg)

    manifest = text("gateway/Cargo.toml")
    main_rs = text("gateway/src/main.rs")
    session_rs = text("gateway/src/session.rs")
    compose = text("docker-compose.yml")
    runbook = ROOT / "docs" / "EVENTS-GATEWAY.md"

    check('name = "opensme-events-gateway"' in manifest,
          "G1 gateway crate is named opensme-events-gateway")
    check('#[path = "../../portal/src/events.rs"]' in main_rs and "mod events;" in main_rs,
          "G2 gateway reuses the portal events module via path")
    check('.route("/api/events", get(handle_events))' in main_rs,
          "G3 gateway exposes only GET /api/events")
    check("session::verify_session(&state.secret, &token)" in main_rs and "UNAUTHORIZED" in main_rs,
          "G4 gateway verifies the session cookie before streaming")
    check('"OPENSMEPORTALSESSION"' in session_rs
          and "HmacSha256" in session_rs
          and '"LOGIN_SESSION_COOKIE_SECRET"' in session_rs,
          "G5 session crypto mirrors the portal cookie format")
    service = compose.split("\n  events-gateway:", 1)[1] if "\n  events-gateway:" in compose else ""
    check('profiles: ["events-gateway"]' in service[:400]
          and "/api/events" in service
          and "/api/events/publish" not in service,
          "G6 compose service is opt-in and read-only")
    check(runbook.exists()
          and "- [Events gateway](EVENTS-GATEWAY.md)" in text("docs/README.md"),
          "G7 runbook exists and is linked from docs/README.md")
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
