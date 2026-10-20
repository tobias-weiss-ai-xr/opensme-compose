#!/usr/bin/env python3
"""
tests/05-e2e/saga_leave_request.py — Leave-request journey blueprint (S3).

Validates the public contracts of the leave-request saga: the Operaton engine
API is reachable and the vacation-request BPMN process definition is deployed.
Skips cleanly when Operaton (workflow) is not deployed.

Usage:
    python3 tests/05-e2e/saga_leave_request.py              # domain from .env
    python3 tests/05-e2e/saga_leave_request.py opensme.local

Exit codes: 0 = pass, 1 = failed (skips don't fail).
"""

import argparse
import os
import sys
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import T, install_dns_fallback, is_localish, load_env_file
from saga_common import probe_portal, service_deployed, service_url


# Operaton (BPMN) REST API paths.
ENGINE_HEALTH = "/engine-rest/engine"
PROCESS_KEY = "opensme-vacation-request"
PROCESS_DEF = f"/engine-rest/process-definition/key/{PROCESS_KEY}"


def main() -> bool:
    ap = argparse.ArgumentParser(description="openSME leave-request journey blueprint")
    ap.add_argument("domain", nargs="?", default=None, help="stack domain")
    args = ap.parse_args()

    dotenv = load_env_file()

    def cfg(key, default=""):
        return os.environ.get(key) or dotenv.get(key) or default

    domain = (args.domain or cfg("OPENSME_DOMAIN", "localhost")).strip()
    localish = is_localish(domain)
    if localish:
        install_dns_fallback()
        urllib3.disable_warnings()

    schema = "http://" if localish else "https://"

    session = requests.Session()
    session.verify = not localish
    result = Result("saga-leave-request")

    # Portal discovery + rendered tiles
    env_portal = os.environ.get("PORTAL_DOMAIN") or cfg("PORTAL_DOMAIN", "")
    portal_base, services = probe_portal(session, domain, localish, schema, env_portal)
    if portal_base is None:
        result.skip("portal unreachable — start the stack first")
        return result.summary()

    result.header(f"leave-request journey — portal={portal_base}")

    # Participating service: Operaton (workflow tile)
    if not service_deployed(services, "workflow"):
        result.skip("leave-request: Operaton (workflow) not deployed — journey skipped")
        return result.summary()

    # 1. Operaton engine API reachable
    workflow_url = service_url(services, "workflow")
    if workflow_url and not workflow_url.startswith("http"):
        workflow_url = "https://" + workflow_url
    engine_url = workflow_url.rstrip("/") + ENGINE_HEALTH
    try:
        r = session.get(engine_url, timeout=T)
        if r.status_code == 200:
            result.ok("Operaton engine API answers (HTTP 200)")
        else:
            result.fail(f"Operaton engine API: HTTP {r.status_code}")
            return result.summary()
    except requests.RequestException as e:
        result.fail(f"Operaton engine API unreachable: {e.__class__.__name__}")
        return result.summary()

    # 2. Vacation-request BPMN process definition deployed
    proc_url = workflow_url.rstrip("/") + PROCESS_DEF
    try:
        r = session.get(proc_url, timeout=T)
        if r.status_code == 200:
            body = r.json()
            key = body.get("key", "")
            if key == PROCESS_KEY:
                result.ok(f"vacation-request process definition deployed (key={key})")
            else:
                result.fail(f"process definition key mismatch: expected {PROCESS_KEY}, got {key}")
        elif r.status_code == 404:
            result.fail("vacation-request process definition not deployed (404)")
        else:
            result.fail(f"process definition probe: HTTP {r.status_code}")
    except (requests.RequestException, ValueError) as e:
        result.fail(f"process definition probe failed: {e.__class__.__name__}")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
