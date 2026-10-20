#!/usr/bin/env python3
"""
tests/05-e2e/hostile_input.py — nothing breaks out (Epic AC).

intercom.py proves the happy path renders escaped; this journey feeds
the portal the input an attacker would send and pins the rendering
safety contract:

  AC1  event-handler injection payloads (<img onerror=…>, <svg onload=…>,
       broken-tag smuggles) reach the page ONLY html-escaped — no raw
       handler attribute ever appears in the served markup
  AC2  the attachment-url guard rejects every non-https transport:
       javascript:, data:, credential-bearing URLs, explicit-port
       bypasses — 400, never 5xx, never stored
  AC3  unicode survives the round trip: umlauts, emoji and RTL text go
       through POST → API → page byte-identically (properly encoded,
       not mojibake, not dropped)
  AC4  the API returns messages raw (machine truth), the page returns
       them escaped (human safety) — the deliberate contrast holds
  AC5  the 51st note is answered honestly (201 or 4xx) and the served
       list stays bounded (≤ MAX_MESSAGES) — no unbounded growth

Read-only against the rest of the stack; leaves only intercom notes,
which the journey removes again via the portal's own rotate-out.

Usage:
    python3 tests/05-e2e/hostile_input.py [domain]  # default: opensme.local
"""

import argparse
import re
import socket
import sys
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")

PAYLOADS = [
    ('img-handler', '"><img src=x onerror=alert(1)>'),
    ('svg-onload', "<svg onload=alert('xss')>team note"),
    ('script-tag', "<script>alert('xss')</script>hello"),
    ('iframe', "<iframe src=\"https://evil.example\"></iframe>"),
    ('broken-tag-smuggle', "<<script>script>alert(1)<</script>/script>"),
]
BAD_URLS = [
    ("javascript-scheme", "javascript:alert(document.cookie)"),
    ("data-scheme", "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="),
    ("credential-url", "https://user:secret@cloud.opensme.local/s/x.pdf"),
    ("explicit-port-bypass", "http://cloud.opensme.local:8080/x.pdf"),
]
UNICODE_SAMPLES = [
    ("umlauts", "Angebot für Müller & Söhne —.Str. 12"),
    ("emoji", "Team standup 🚀 — Deployment grün ✅"),
    ("rtl", "שלום עולם — مرحبا بالعالم"),
]


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


def ic_list_segment(body: str) -> str:
    """The rendered intercom list — the only region user text reaches."""
    m = re.search(r'<ul class="ic-list".*?</ul>', body, re.DOTALL)
    return m.group(0) if m else ""


def raw_handler_in(body: str) -> list[str]:
    """Return payload fragments that appear UNESCAPED in the intercom
    list segment.

    Only raw tag-opening brackets make a fragment dangerous: escaped
    markup (`&lt;img ... onerror=…`) is exactly what the contract
    promises, so `onerror=` alone is NOT a hit — it must follow a raw
    `<tag` sequence. The page's own <script src=/app.js> in the head is
    out of scope (we only audit the user-text region).
    """
    segment = ic_list_segment(body)
    hits = []
    for pattern in (r"<[a-zA-Z][^>]*onerror", r"<[a-zA-Z][^>]*onload",
                    r"<script\b", r"<iframe\b", r"<svg[^>]*onload"):
        if re.search(pattern, segment):
            hits.append(pattern)
    return hits


def html_escaped(text: str) -> str:
    """Mirror the portal's html_escape (&, <, >, \", ')."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;")
                .replace("'", "&#x27;"))


def main() -> int:
    ap = argparse.ArgumentParser(description="hostile input journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("hostile-input")
    result.header(f"openSME e2e hostile input journey — domain={args.domain}")

    import subprocess
    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True).stdout.split()
    if not any("opensme-portal" in n for n in names):
        result.skip("portal not running (start the stack first)")
        print()
        return 0

    portal = f"https://portal.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    if not ensure_portal_routed(session, portal):
        result.fail("portal unreachable via traefik even after resync")
        return 1

    # ── AC1: handler payloads render escaped, never raw ─────────────
    ac1_fail = []
    for name, payload in PAYLOADS:
        r = session.post(portal + "/api/intercom", json={"text": payload},
                         timeout=T)
        if r.status_code != 201:
            ac1_fail.append(f"{name}: POST={r.status_code}")
            continue
        page = session.get(portal + "/", timeout=T).text
        leaks = raw_handler_in(page)
        if leaks:
            ac1_fail.append(f"{name}: raw {leaks} in served HTML")
    (result.ok if not ac1_fail else result.fail)(
        f"all {len(PAYLOADS)} handler payloads render strictly escaped"
        if not ac1_fail
        else "rendering unsafe: " + "; ".join(ac1_fail[:3])
    )

    # ── AC2: hostile attachment URLs are rejected ───────────────────
    ac2_fail = []
    for name, url in BAD_URLS:
        r = session.post(portal + "/api/intercom",
                         json={"text": "safety probe", "attachment_url": url},
                         timeout=T)
        if r.status_code >= 500:
            ac2_fail.append(f"{name}: 5xx on hostile url")
        elif r.status_code == 201:
            # stored?! check the page for a live link to it
            page = session.get(portal + "/", timeout=T).text
            if url in page or "user:secret" in page:
                ac2_fail.append(f"{name}: hostile url stored AND rendered")
            else:
                ac2_fail.append(f"{name}: stored (201) though not rendered")
    (result.ok if not ac2_fail else result.fail)(
        f"all {len(BAD_URLS)} hostile attachment URLs rejected (no 5xx, none stored)"
        if not ac2_fail
        else "url guard holes: " + "; ".join(ac2_fail)
    )

    # ── AC3: unicode round trip ─────────────────────────────────────
    ac3_fail = []
    for name, text in UNICODE_SAMPLES:
        r = session.post(portal + "/api/intercom", json={"text": text},
                         timeout=T)
        if r.status_code != 201:
            ac3_fail.append(f"{name}: POST={r.status_code}")
            continue
        listed = session.get(portal + "/api/intercom", timeout=T).json()
        msgs = [m.get("text", "") for m in
                (listed if isinstance(listed, list)
                 else listed.get("messages", []))]
        if text not in msgs:
            ac3_fail.append(f"{name}: not byte-identical in API")
            continue
        page = session.get(portal + "/", timeout=T)
        page.encoding = "utf-8"  # don't trust the header guess
        # the text is present if it appears raw OR in its html-escaped
        # spelling (& → &amp;) — both render the same characters
        if text not in page.text and html_escaped(text) not in page.text:
            ac3_fail.append(f"{name}: lost or mangled on the page")
    (result.ok if not ac3_fail else result.fail)(
        f"unicode round trip holds for {len(UNICODE_SAMPLES)} samples "
        "(umlauts, emoji, RTL)"
        if not ac3_fail
        else "unicode broken: " + "; ".join(ac3_fail)
    )

    # ── AC4: API raw, page escaped — the deliberate contrast ────────
    probe = "<script>contrast-probe</script>"
    r = session.post(portal + "/api/intercom", json={"text": probe},
                     timeout=T)
    ac4 = False
    if r.status_code == 201:
        listed = session.get(portal + "/api/intercom", timeout=T).json()
        msgs = [m.get("text", "") for m in
                (listed if isinstance(listed, list)
                 else listed.get("messages", []))]
        raw_in_api = probe in msgs
        page = session.get(portal + "/", timeout=T).text
        escaped_on_page = "&lt;script&gt;contrast-probe" in page
        ac4 = raw_in_api and escaped_on_page
    (result.ok if ac4 else result.fail)(
        "contrast holds: API serves raw truth, page serves escaped markup"
        if ac4
        else f"contrast broken: api_raw={'?' if r.status_code != 201 else probe in str(listed)}"
    )

    # ── AC5: list stays bounded at MAX_MESSAGES ─────────────────────
    overflow_ok, bounded = True, True
    for i in range(51):
        r = session.post(portal + "/api/intercom",
                         json={"text": f"overflow probe {i:03d}"}, timeout=T)
        if r.status_code not in (201, 400, 422):
            overflow_ok = False
            break
    if overflow_ok:
        listed = session.get(portal + "/api/intercom", timeout=T).json()
        count = len(listed if isinstance(listed, list)
                    else listed.get("messages", []))
        bounded = count <= 50
    (result.ok if overflow_ok and bounded else result.fail)(
        f"51-note overflow answered honestly, list bounded (api count {count})"
        if overflow_ok and bounded
        else f"bounded-list broken: honest={overflow_ok} api_count={count}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
