#!/usr/bin/env python3
"""
tests/05-e2e/webmail.py — SOGo webmail page contract (standalone journey).

Background (2026-10-12): the live webmail served a blank login page — the
HTML rendered, but every static asset under /SOGo.woa/WebServerResources/
returned 404. Root cause: a stale sogo container still ran with the old
Traefik labels (sogod :20000, which 404s WebServerResources) while
mail/sogo.yml correctly fronts sogod with apache2 on :80. Nothing in the
suite caught it: section_sogo_sso only checks the SSO redirect shape.

This journey pins the page contract:

  0. Precondition: SOGo is deployed. A stack that boots no mail profile
     (the demo stack — `scripts/demo.sh`) skips here instead of failing;
     a SOGo container that exists but does not answer /SOGo/ FAILS, which
     is the regression this journey is for (a container that silently
     lost its routing).
  1. GET /            → 301 (permanent) to /SOGo/ (root-redirect middleware)
  2. GET /SOGo/       → 200 with SOGo login markup
  3. Every asset the page references (css/js/img under WebServerResources)
     must resolve with 200 — a blank page fails here, even when the HTML
     and the SSO redirect look healthy.

Usage:
  python3 tests/05-e2e/webmail.py                    # domain from .env
  python3 tests/05-e2e/webmail.py home.opensme.graphwiz.ai

Exit codes: 0 = pass, 1 = failed (skips don't fail).
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import (  # noqa: E402  (path bootstrap before import)
    T,
    install_dns_fallback,
    is_localish,
    load_env_file,
)


def asset_urls(html: str, base: str) -> list[str]:
    """All local css/js/img asset URLs referenced by the page."""
    urls: list[str] = []
    for attr in ("href", "src"):
        for raw in re.findall(rf'{attr}="([^"]+)"', html):
            if not re.search(r"\.(css|js|ico|png|svg|woff2?)(\?|$)", raw):
                continue
            if raw.startswith(("http://", "https://", "data:", "mailto:")):
                continue
            urls.append(urljoin(base, raw))
    return sorted(set(urls))


def sogo_container_present() -> bool:
    """True when the local engine runs a `opensme-sogo` container.

    Only docker speaks here: the container name is fixed by mail/sogo.yml
    (`container_name: opensme-sogo`), and every deployment that fronts SOGo
    with this compose file has it. `docker ps` lists running AND
    restarting containers — a crash-looping SOGo counts as deployed (it must
    be caught), a container that was created but never started does not.
    When the engine is unreachable (or the host uses another runtime) we
    cannot tell — treat it as "not deployed" so the journey skips instead of
    inventing a failure.
    """
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return "opensme-sogo" in proc.stdout.split()


def main() -> bool:
    ap = argparse.ArgumentParser(description="openSME webmail page contract")
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
    # An explicit domain arg targets a specific deployment — it must win
    # over shell env and repo-local .env (both may point at the local demo).
    sogo_host = (
        f"webmail.{domain}" if args.domain
        else (os.environ.get("SOGO_DOMAIN") or cfg("SOGO_DOMAIN", f"webmail.{domain}"))
    )
    webmail_base = f"{schema}{sogo_host}"

    session = requests.Session()
    session.verify = not localish

    result = Result("webmail-page")
    result.header(f"SOGo webmail page contract — {webmail_base}")

    # 0. Precondition — is SOGo deployed at all?
    # A deployed SOGo answers /SOGo/ with 200 (check 2 below). When it does not
    # answer, only the presence of the container distinguishes "no mail profile
    # booted" (skip — the demo stack ships neither Stalwart nor SOGo) from
    # "deployed but no longer routed" (fail — the 2026-10-12 regression).
    try:
        probe = session.get(webmail_base + "/SOGo/", timeout=T)
    except requests.RequestException as e:
        probe = None
        probe_error = f"{type(e).__name__}: {e}"
    else:
        probe_error = f"HTTP {probe.status_code}"
    if probe is None or probe.status_code != 200:
        if sogo_container_present():
            result.fail(
                f"opensme-sogo container exists but GET /SOGo/ → {probe_error} "
                "— deployed SOGo must answer (routing/label regression)"
            )
        else:
            result.skip(
                f"SOGo not deployed ({webmail_base}/SOGo/ → {probe_error}, no "
                "opensme-sogo container) — boot the mail profile to run this"
            )
        return result.summary()

    # 1. root redirect → /SOGo/
    try:
        r = session.get(webmail_base + "/", allow_redirects=False, timeout=T)
    except requests.RequestException:
        result.skip("sogo not running")
        return result.summary()
    loc = r.headers.get("Location", "")
    if r.status_code != 301:
        result.fail(f"GET / → expected 301 to /SOGo/, got {r.status_code} {loc[:60]}")
    elif not loc.startswith(webmail_base + "/SOGo"):
        result.fail(f"GET / → Location must stay on the webmail host, got {loc[:80]}")
    else:
        result.ok("root redirects (301) to /SOGo/")

    # 2. login page markup (the precondition probe already fetched it)
    page = probe
    if page.status_code != 200:
        result.fail(f"GET /SOGo/ → {page.status_code} (expected 200)")
        return result.summary()
    body = page.text
    if "SOGo" not in body:
        result.fail("/SOGo/ HTML carries no SOGo marker — is this the right app?")
    elif "<form" not in body and "log" not in body.lower():
        result.fail("/SOGo/ HTML carries no login markup")
    else:
        result.ok("login page serves SOGo markup")

    # 3. every referenced asset must load (THE blank-page regression)
    assets = asset_urls(body, webmail_base)
    if not assets:
        result.fail("login page references no assets — blank-page suspected")
    else:
        broken = []
        for url in assets:
            try:
                ar = session.get(url, timeout=T)
            except requests.RequestException as e:
                broken.append(f"{url} ({e.__class__.__name__})")
                continue
            if ar.status_code != 200:
                broken.append(f"{url} → {ar.status_code}")
        if broken:
            result.fail(f"{len(broken)}/{len(assets)} page assets broken:")
            for b in broken:
                result.info(f"  {b}")
        else:
            result.ok(f"all {len(assets)} referenced assets serve 200")

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
