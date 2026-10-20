#!/usr/bin/env python3
"""
cloud_mock.py — stand-in "cloud" for e2e attachment journeys.

Runs a tiny HTTP server that behaves like the real cloud's share
endpoint: every answer carries a Content-Disposition attachment header
with the DECODED basename of the request path. On startup it writes its
own test files (a 5 KB pdf-ish Angebot and a hostile-named file), so
journeys only need to copy THIS script into the container.

Usage: python3 cloud_mock.py <port>
"""

import os
import sys
import urllib.parse
from http.server import SimpleHTTPRequestHandler, HTTPServer


class CloudHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # real clouds send the DECODED filename in Content-Disposition
        name = urllib.parse.unquote(self.path.rsplit("/", 1)[-1])
        if name:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{name}"'
            )
        super().end_headers()


def ensure_files(srv: str) -> None:
    """Synthesize the fixtures journeys expect — no docker cp needed."""
    angebot = os.path.join(srv, "Angebot_2026.pdf")
    if not os.path.exists(angebot):
        with open(angebot, "wb") as f:
            f.write(b"%PDF-1.4\n% e2e attachment\n" + b"x" * 5000)
    hostile = os.path.join(srv, "Rechnung<script>.pdf")
    if not os.path.exists(hostile):
        with open(hostile, "wb") as f:
            f.write(b"%PDF-1.4\n% hostile name\n")


if __name__ == "__main__":
    srv = "/srv"
    os.makedirs(srv, exist_ok=True)
    ensure_files(srv)
    os.chdir(srv)
    HTTPServer(("0.0.0.0", int(sys.argv[1])), CloudHandler).serve_forever()
