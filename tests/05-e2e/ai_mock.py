#!/usr/bin/env python3
"""
tests/05-e2e/ai_mock.py — a tiny OpenAI-compatible chat server.

Speaks just enough of the protocol for the portal's /api/ai/chat proxy:
accepts POST /v1/chat/completions, requires a bearer token, logs the
received request body to <tmpdir>/last_request.json (so the journey can
verify the portal speaks the right contract), and answers with a fixed,
recognisable text.

Usage: python3 tests/05-e2e/ai_mock.py <port> <tmpdir> <api-key> [model]
Terminates when the temp marker file disappears (journey cleanup) or on
SIGTERM.
"""

import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    api_key: str = ""
    model: str = ""
    tmpdir: str = ""

    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {self.api_key}":
            body = json.dumps({"error": {"message": "bad key"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n)
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400)
            return
        with open(os.path.join(self.tmpdir, "last_request.json"), "w") as fh:
            json.dump({"headers": {"authorization": auth}, "body": req}, fh)
        answer = "E2E-MOCK: Ich bin ein lokales Modell und antworte deterministisch."
        body = json.dumps({
            "id": "chatcmpl-e2e-mock",
            "object": "chat.completion",
            "model": req.get("model", self.model),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # health for the journey
        body = b'{"status":"mock-ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    port, tmpdir, api_key = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    Handler.api_key = api_key
    Handler.model = sys.argv[4] if len(sys.argv) > 4 else "e2e-mock"
    Handler.tmpdir = tmpdir

    def die(*_):
        sys.exit(0)

    signal.signal(signal.SIGTERM, die)
    server = HTTPServer(("0.0.0.0", port), Handler)
    deadline = time.time() + 600  # hard cap; journey deletes the marker sooner
    server.timeout = 1
    while time.time() < deadline:
        if not os.path.isdir(tmpdir):
            break
        server.handle_request()
    return 0


if __name__ == "__main__":
    sys.exit(main())
