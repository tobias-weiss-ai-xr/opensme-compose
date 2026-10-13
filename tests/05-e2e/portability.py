#!/usr/bin/env python3
"""
tests/05-e2e/portability.py — bring your own domain (Epic V).

An SME doesn't run on opensme.example forever. This journey proves the
portal can be MOVED to another domain by env alone — no code, no image
rebuild — and that derived defaults follow:

  V1  portal recreated with PORTAL_DOMAIN=portal.meine-firma.test
      answers 200 on the NEW host (DNS fallback maps *.test to loopback)
      and shows the new domain on the page
  V2  the OLD domain stops routing (migration, not a copy)
  V3  derived defaults follow: the intercom default attachment
      allowlist is cloud.<OPENSME_DOMAIN> — a cloud stand-in aliased
      cloud.meine-firma.test on the compose network is accepted
  V4  rollback: recreate with the original env — old domain answers
      again, new domain is gone

The journey recreates the portal twice and self-heals the traefik
router (conftest.ensure_portal_routed) — lifecycle-driving like
broadcast/ai_journey/intercom.

Usage:
    python3 tests/05-e2e/portability.py [domain]   # default: opensme.local
"""

import argparse
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import Result, ensure_portal_routed, wait_http_ok
from cloud_mock_support import start_cloud_standin, stop_cloud_standin

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"
NEW_DOMAIN = "meine-firma.test"
CLOUD_PORT = 8099


def compose_args() -> list[str]:
    import os
    cf = os.environ.get("COMPOSE_FILE") or DEMO_SET
    args: list[str] = []
    for f in cf.split(os.pathsep):
        if f:
            args += ["-f", f]
    return args


def recreate_portal(env_overrides: dict[str, str]) -> None:
    """Recreate the portal; drop every domain/intercom var first so
    overrides are the ONLY source of truth for this recreation."""
    import os
    env = dict(os.environ)
    for k in ("AI_API_URL", "AI_MODEL", "AI_API_KEY",
              "INTERCOM_ATTACHMENT_HOSTS", "INTERCOM_ALLOW_HTTP",
              "PORTAL_DOMAIN", "OPENSME_DOMAIN", "OPENCLOUD_DOMAIN",
              "ZITADEL_DOMAIN", "CMS_DOMAIN", "SHOP_DOMAIN",
              "TICKETING_DOMAIN", "COLLABORA_DOMAIN", "MAIL_DOMAIN"):
        env.pop(k, None)
    env.update(env_overrides)
    subprocess.run(["docker", "compose"] + compose_args() + ["up", "-d", "portal"],
                   env=env, check=True, capture_output=True, timeout=180)


def install_dns_fallback() -> None:
    original = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        try:
            return original(host, *args, **kwargs)
        except socket.gaierror:
            if (isinstance(host, str)
                    and re.fullmatch(r"[a-zA-Z0-9.-]+", host)
                    and any(host.endswith(s) for s in LOCALISH_SUFFIXES)):
                return original("127.0.0.1", *args, **kwargs)
            raise

    socket.getaddrinfo = patched


def main() -> int:
    ap = argparse.ArgumentParser(description="portability journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("portability")
    result.header(f"openSME e2e portability journey — domain={args.domain}")

    probe = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True)
    if not any("opensme-portal" in n for n in probe.stdout.split()):
        result.skip("portal not running (start the stack first)")
        print()
        return 0

    old_portal = f"https://portal.{args.domain}"
    new_portal = f"https://portal.{NEW_DOMAIN}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    tmpdir = Path("/tmp/opensme-portability-cloud")
    try:
        # routing first: traefik's provider can lose the portal router
        # after earlier journeys' fast recreates — heal before judging
        if not ensure_portal_routed(session, old_portal):
            result.fail("portal unreachable via traefik even after resync")
            return 1

        baseline = session.get(old_portal + "/", timeout=T)
        v0 = baseline.status_code == 200
        (result.ok if v0 else result.fail)(
            "baseline: portal reachable on the original domain"
            if v0 else f"baseline broken: HTTP {baseline.status_code}"
        )

        # ── the move: portal labels re-interpolate at recreate time ──
        recreate_portal({
            "PORTAL_DOMAIN": f"portal.{NEW_DOMAIN}",
            "OPENSME_DOMAIN": NEW_DOMAIN,
        })
        if not ensure_portal_routed(session, new_portal):
            result.fail("portal did not come up under the new domain")
            return 1

        # V1: new domain answers, shows the new domain
        new_page = session.get(new_portal + "/", timeout=T)
        v1 = (new_page.status_code == 200
              and NEW_DOMAIN in new_page.text
              and "openSME" in new_page.text)
        (result.ok if v1 else result.fail)(
            f"portal answers on portal.{NEW_DOMAIN} with the new domain on the page"
            if v1
            else f"new domain broken: HTTP {new_page.status_code}"
        )

        # V2: the old domain is GONE (traefik router label was replaced)
        old_health = session.get(old_portal + "/health", timeout=T)
        v2 = old_health.status_code == 404
        (result.ok if v2 else result.fail)(
            "old domain stopped routing (migration, not a copy)"
            if v2 else f"old domain still routed: HTTP {old_health.status_code}"
        )

        # V3: derived defaults follow the domain — the intercom default
        # allowlist is cloud.<OPENSME_DOMAIN>; a stand-in aliased as
        # cloud.meine-firma.test on the compose network must be accepted
        recreate_portal({
            "PORTAL_DOMAIN": f"portal.{NEW_DOMAIN}",
            "OPENSME_DOMAIN": NEW_DOMAIN,
            "INTERCOM_ALLOW_HTTP": "1",  # stand-in serves plain http
        })
        if not ensure_portal_routed(session, new_portal):
            result.fail("portal did not come back for the allowlist check")
            return 1
        cloud_up = start_cloud_standin(tmpdir, alias=f"cloud.{NEW_DOMAIN}",
                                       port=80)  # URL stays portless — the derived default is cloud.<domain>
        if not cloud_up:
            result.skip("cloud stand-in container did not start")
        else:
            # give the alias a moment to propagate into the embedded DNS
            time.sleep(2)
            # the portal POST races traefik's provider reload (the stand-in
            # container events trigger a full router re-evaluation that can
            # transiently drop the portal router in CI) — re-assert routing
            # before every attempt instead of failing on a single 404
            att = None
            for attempt in range(6):
                if not ensure_portal_routed(session, new_portal):
                    # Diagnose the routing state instead of guessing:
                    # container up? which routers does traefik actually have?
                    import subprocess as _sp
                    state = _sp.run(
                        ["docker", "ps", "--filter", "name=opensme-portal",
                         "--format", "{{.Names}} {{.Status}}"],
                        capture_output=True, text=True, timeout=30)
                    print(f"  [diag] ensure-fail attempt {attempt}: "
                          f"portal={state.stdout.strip() or 'NOT RUNNING'}")
                    continue
                att = session.post(new_portal + "/api/intercom",
                                   json={"text": "Domain-Migration klappt",
                                         "attachment_url":
                                             f"http://cloud.{NEW_DOMAIN}/Angebot_2026.pdf"},
                                   timeout=30)
                if att.status_code != 404:
                    break
                time.sleep(5)
            v3 = att is not None and att.status_code == 201
            (result.ok if v3 else result.fail)(
                "derived allowlist follows the new domain (cloud.<domain> accepted)"
                if v3
                else f"default allowlist did not follow: "
                     f"HTTP {att.status_code if att else 'unroutable'} "
                     f"{att.text[:120] if att else ''}"
            )

        # V4: rollback — original env restores the original routing
        recreate_portal({})
        if not ensure_portal_routed(session, old_portal):
            result.fail("rollback: original domain did not come back")
            return 1
        back = session.get(old_portal + "/health", timeout=T)
        gone = session.get(new_portal + "/health", timeout=T)
        v4 = back.status_code == 200 and gone.status_code == 404
        (result.ok if v4 else result.fail)(
            "rollback restores the original domain (and only that)"
            if v4
            else f"rollback broken: old={back.status_code} new={gone.status_code}"
        )
    finally:
        stop_cloud_standin()
        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            recreate_portal({})
        except subprocess.SubprocessError:
            pass
        ensure_portal_routed(session, old_portal)

    health = session.get(old_portal + "/health", timeout=T).status_code
    (result.ok if health == 200 else result.fail)(
        "stack restored: portal healthy on the original domain"
        if health == 200 else f"restore broken: health={health}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
