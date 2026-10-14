#!/usr/bin/env python3
"""
tests/05-e2e/saga_smoke.py — Saga navigation smoke (portal-catalog).

The sagas (S1 daily work, S2 quote-to-cash, S3 approvals, S4 customer
service, S5 external visibility, S6 knowledge) are cross-service user
journeys. This smoke pins their NAVIGATION edges — the part every saga
shares: a tile the portal renders must lead somewhere that answers
("no tile to nowhere"), and the portal's own APIs must degrade
gracefully. Full journeys (credentials, document flow) live in the
dedicated e2e scripts and docs/E2E-JOURNEYS.md.

Checks:
  P1  portal /health answers
  P2  portal /api/feeds returns 200 with a columns payload (feed
      failures degrade to empty columns — never an error)
  P3  every tile from /api/services leads to a URL that answers
      2xx/3xx over the edge
  P4  configured-but-unrendered catalog functions are reported as
      skips (saga preconditions absent — stack simply doesn't run
      that overlay)

Usage:
  python3 tests/05-e2e/saga_smoke.py                     # domain from .env
  python3 tests/05-e2e/saga_smoke.py home.opensme.graphwiz.ai

Exit codes: 0 = pass, 1 = failed (skips don't fail).
"""

import argparse
import os
import sys
from pathlib import Path

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import T, install_dns_fallback, is_localish, load_env_file  # noqa: E402
from saga_common import (  # noqa: E402
    OPTIONAL_CATALOG, probe_portal, service_deployed,
)


def main() -> bool:
    ap = argparse.ArgumentParser(description="openSME saga navigation smoke")
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
    result = Result("saga-smoke")

    # Portal discovery + /api/services via the shared harness (saga_common).
    env_portal = os.environ.get("PORTAL_DOMAIN") or cfg("PORTAL_DOMAIN", "")
    portal_base, services = probe_portal(session, domain, localish, schema, env_portal)
    if portal_base is None:
        result.skip("portal unreachable — start the stack first")
        return result.summary()

    result.header(f"saga navigation smoke — portal={portal_base}")

    # P1: portal health — precondition of every saga (probe_portal confirmed
    # /health answered 200 to find the base; re-assert for explicit output).
    result.ok("portal /health answers")

    # P2: feeds degrade gracefully (S5 precondition)
    feeds = session.get(portal_base + "/api/feeds", timeout=T)
    if feeds.status_code != 200 or "columns" not in feeds.json():
        result.fail(f"/api/feeds must return 200 + columns payload, got {feeds.status_code}")
    else:
        cols = feeds.json()["columns"]
        result.ok(f"/api/feeds serves {len(cols)} columns (degradation-safe)")

    # P3: every rendered tile leads somewhere — no tile to nowhere
    if not services:
        result.fail("portal renders no tiles — catalog is empty")
    for svc in services:
        sid, url = svc.get("id"), svc.get("url", "")
        if not sid or not url:
            result.fail(f"tile without id or url: {svc}")
            continue
        try:
            r = session.get(url if url.startswith("http") else "https://" + url,
                            allow_redirects=True, timeout=T)
        except requests.RequestException as e:
            result.fail(f"tile '{sid}' → {url} unreachable ({e.__class__.__name__})")
            continue
        if r.status_code < 400:
            result.ok(f"tile '{sid}' → {url} answers {r.status_code}")
        else:
            result.fail(f"tile '{sid}' → {url} broken (HTTP {r.status_code})")

    # P4: saga preconditions absent → visible skips, never silent
    for sid in OPTIONAL_CATALOG:
        if not service_deployed(services, sid):
            result.skip(f"catalog function '{sid}' not deployed — saga step skipped")

    # P5: document-flow endpoints behind auth walls (S2 quote-to-cash).
    # The flow composes Invoice exports and the DMS ingest — both must
    # answer at the edge but never without credentials.
    flow_urls = {
        # bare /api/v1/ is NOT an Invoice Ninja API endpoint (the Laravel app
        # serves the SPA fallback there, HTTP 200 HTML) — probe a real
        # resource collection, which is always token-walled
        "invoice-api": [s["url"] + "/api/v1/invoices" for s in services if s.get("id") == "invoice"],
        "archive-api": [s["url"] for s in services if s.get("id") == "archive"],
        # impress notes exposes its Django API on the same origin since the
        # notesapi edge router (Host && PathPrefix(/api)) — unauthenticated
        # reads are rejected with 400 (no data, no redirect target), which
        # counts as walled alongside 401/403
        "notes-api": [s["url"] + "/api/v1.0/documents/" for s in services if s.get("id") == "notes"],
    }
    for name, urls in flow_urls.items():
        if not urls:
            result.skip(f"{name}: function not deployed")
            continue
        base = urls[0].rstrip("/")
        api = base if "invoice" in name else base + "/api/documents/"
        if "notes" in name:
            api = urls[0]
        try:
            r = session.get(api, allow_redirects=False, timeout=T)
        except requests.RequestException as e:
            result.fail(f"{name} endpoint unreachable ({e.__class__.__name__})")
            continue
        if r.status_code in (400, 401, 403):
            result.ok(f"{name} API behind auth wall ({r.status_code})")
        elif r.status_code in (301, 302, 303, 307, 308):
            result.ok(f"{name} API redirects to login ({r.status_code})")
        else:
            result.fail(f"{name} API must wall auth, got {r.status_code}")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
