#!/usr/bin/env python3
"""Layer 0 pin for the public Operaton /engine-rest block.

E1 — compose keeps the security note about the unauthenticated REST API.
E2 — the camunda-rest-block router matches /engine-rest.
E3 — the block router is bound to the websecure entrypoint.
E4 — the block router has priority 10000.
E5 — the block router terminates at noop@internal.
E6 — the ordinary camunda router does not expose /engine-rest.
E7 — bootstrap docs state that the public path is blocked.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

COMPOSE = ROOT / "services" / "camunda.yml"
README = ROOT / "bootstrap" / "README.md"


def main():
    result = Result("engine-rest-guard")
    result.header("Layer 0: Operaton engine-rest public block")
    compose = COMPOSE.read_text() if COMPOSE.exists() else ""
    readme = README.read_text() if README.exists() else ""

    def check(cond, msg):
        (result.ok if cond else result.fail)(msg)

    def label_line(name):
        return next((line for line in compose.splitlines()
                     if f'traefik.http.routers.{name}.' in line), "")

    check("UNAUTHENTICATED" in compose, "E1 security note present")
    check("PathPrefix(`/engine-rest`)" in label_line("camunda-rest-block"),
          "E2 block router matches /engine-rest")
    check('"traefik.http.routers.camunda-rest-block.entrypoints=websecure"' in compose,
          "E3 block router on websecure")
    check('"traefik.http.routers.camunda-rest-block.priority=10000"' in compose,
          "E4 block router has priority 10000")
    check('"traefik.http.routers.camunda-rest-block.service=noop@internal"' in compose,
          "E5 block router stops at noop@internal")
    check("PathPrefix(`/engine-rest`)" not in label_line("camunda"),
          "E6 ordinary camunda router does not expose engine-rest")
    check("BLOCKED" in readme, "E7 bootstrap docs state the public block")

    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
