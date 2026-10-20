#!/usr/bin/env python3
"""
tests/05-e2e/accessibility.py — SSR document hygiene (Epic AB).

Progressive enhancement means the SERVER'S markup is the product. This
journey parses the landing page with the stdlib HTML parser and pins
the structural contract screen readers and every browser rely on:

  AB1  document skeleton: <html lang>, viewport meta, non-empty title
  AB2  every <img> carries an alt attribute (decorative ones may be
       alt="" — they must EXIST)
  AB3  every form control has a programmatic label: aria-label,
       aria-labelledby, or an associated <label for=id>
  AB4  no inline event handlers (onclick= et al.) — scripts stay in
       /app.js (CSP script-src 'self' without unsafe-inline)
  AB5  heading outline: exactly one <h1>, and no level is skipped on
       the way down (h2 after h1, h3 only after h2, …)
  AB6  interactive elements (a, button) have accessible names — no
       empty link text without aria-label

Read-only.

Usage:
    python3 tests/05-e2e/accessibility.py [domain]   # default: opensme.local
"""

import argparse
import re
import socket
import sys
import urllib3
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed

import requests

T = 15
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
INLINE_HANDLERS = re.compile(r"^on[a-z]+$")


class PageAudit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.lang = None
        self.title = None
        self._in_title = False
        self.viewport = None
        self.images = []          # (alt-present, src)
        self.form_controls = []   # (tag, aria-label?, aria-labelledby?, id)
        self.labels = set()       # for= values of <label>
        self.inline_handlers = [] # (tag, attr)
        self.headings = []        # heading levels in document order
        self.named_elements = []  # (tag, text, aria-label?)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "meta" and a.get("name") == "viewport":
            self.viewport = a.get("content")
        elif tag == "title":
            self._in_title = True
        elif tag == "img":
            self.images.append(("alt" in a, a.get("src", "?")))
        elif tag in ("input", "textarea", "select"):
            self.form_controls.append((tag, a.get("aria-label"),
                                       a.get("aria-labelledby"), a.get("id")))
        elif tag == "label" and a.get("for"):
            self.labels.add(a["for"])
        elif tag in ("a", "button"):
            self.named_elements.append((tag, "", a.get("aria-label")))
        for k in a:
            if INLINE_HANDLERS.match(k):
                self.inline_handlers.append((tag, k))
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))

    def handle_data(self, data):
        if self._in_title:
            self.title = (self.title or "") + data
        # feed link/button text into the last named element
        if self.named_elements and not self.named_elements[-1][1]:
            tag, _, aria = self.named_elements[-1]
            self.named_elements[-1] = (tag, data.strip(), aria)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


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
    ap = argparse.ArgumentParser(description="accessibility journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("accessibility")
    result.header(f"openSME e2e accessibility journey — domain={args.domain}")

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

    page = session.get(portal + "/", timeout=T)
    if page.status_code != 200:
        result.fail(f"landing page answers {page.status_code} (want 200)")
        print()
        return 1

    audit = PageAudit()
    audit.feed(page.text)

    # ── AB1: document skeleton ──────────────────────────────────────
    ab1 = bool(audit.lang) and bool(audit.viewport) and \
        bool(audit.title and audit.title.strip())
    (result.ok if ab1 else result.fail)(
        f"document skeleton present (lang={audit.lang!r}, title set, viewport meta)"
        if ab1
        else f"document skeleton broken: lang={audit.lang!r} "
             f"viewport={'yes' if audit.viewport else 'no'} "
             f"title={audit.title!r}"
    )

    # ── AB2: images carry alt ───────────────────────────────────────
    missing_alt = [src for has_alt, src in audit.images if not has_alt]
    (result.ok if not missing_alt else result.fail)(
        f"all {len(audit.images)} images carry an alt attribute"
        if not missing_alt
        else f"images without alt: {missing_alt[:5]}"
    )

    # ── AB3: form controls are labelled ─────────────────────────────
    unlabelled = []
    for tag, aria, aria_by, ident in audit.form_controls:
        if aria or aria_by or (ident and ident in audit.labels):
            continue
        unlabelled.append(f"{tag}#{ident or '?'}")
    (result.ok if not unlabelled else result.fail)(
        f"all {len(audit.form_controls)} form controls carry a programmatic label"
        if not unlabelled
        else f"unlabelled form controls: {unlabelled}"
    )

    # ── AB4: no inline event handlers ───────────────────────────────
    (result.ok if not audit.inline_handlers else result.fail)(
        "no inline event handlers (scripts stay external, CSP-safe)"
        if not audit.inline_handlers
        else f"inline handlers: {audit.inline_handlers[:5]}"
    )

    # ── AB5: heading outline ────────────────────────────────────────
    h1_count = sum(1 for h in audit.headings if h == 1)
    skips = []
    prev = None
    for h in audit.headings:
        if prev is not None and h > prev + 1:
            skips.append(f"h{prev}→h{h}")
        prev = h
    ab5 = h1_count == 1 and not skips
    (result.ok if ab5 else result.fail)(
        f"heading outline clean: exactly one h1, no skipped levels "
        f"({len(audit.headings)} headings)"
        if ab5
        else f"heading outline broken: h1 count={h1_count} "
             f"skips={skips}"
    )

    # ── AB6: interactive elements have accessible names ─────────────
    unnamed = [f"{tag}({(text or '')[:24]!r})"
               for tag, text, aria in audit.named_elements
               if not (text or aria)]
    (result.ok if not unnamed else result.fail)(
        f"all {len(audit.named_elements)} links/buttons have accessible names"
        if not unnamed
        else f"unnamed interactive elements: {unnamed}"
    )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
