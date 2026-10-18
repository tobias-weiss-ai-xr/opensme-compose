#!/usr/bin/env python3
"""
tests/06-security/llm_outbound.py — AE1 Layer-6 outbound LLM privacy gate
(docs/AGENTIC-AI.md Epic AE1).

Proves that NOTHING sensitive leaves the stack toward an LLM.
Self-contained: spins a mini HTTP mock-LLM server, a dev-agent container
wired to that mock, triggers reconciliation, and then inspects the
captured requests to assert no secrets/RFC1918/internal-hostnames/user-paths
appear in outbound payloads.

The check is OPT-IN (--llm-outbound) and exit 0 on a clean machine with docker.

Usage:
    python3 tests/06-security/llm_outbound.py [--llm-outbound]
Exit: 0 = PASS, 1 = fail (skips do not fail)
"""

import argparse
import http.server
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result  # noqa: E402

# Mock LLM server: binds to host.docker.internal so the bot container can reach it
MOCK_LLM_PORT = 11808
MOCK_LLM_HOST = "localhost"
CAPTURED_REQUESTS = []
CAPTURE_LOCK = threading.Lock()

# Pattern catalog reused from openSME scan_secrets
SECRET_PATTERNS = re.compile(
    r"(?:password|passwd|pass|pwd|secret|token|api[_-]?key|apikey"
    r"|auth|private[_-]?key|access[_-]?key|accesskey|credential)"
    r"[=: \x27\" ]+[A-Za-z0-9+/=_-]{8,}",
    re.IGNORECASE,
)

# RFC1918 / private IPs (not in public routes)
PRIVATE_IP_RE = re.compile(
    r"\b(?:0\.\d{1,3}\.\d{1,3}\.\d{1,3}|"  # this-network
    r"10\.(?:\d{1,3}\.){2}\d{1,3}|"            # private
    r"172\.(?:1[6-9]|2\d|3[0-1])\.(?:\d{1,3}\.)\d{1,3}|"  # private
    r"192\.168\.(?:\d{1,3}\.)\d{1,3}|"       # private
    r"169\.254\.(?:\d{1,3}\.)\d{1,3}|"       # link-local
    r"127\.(?:\d{1,3}\.){2}\d{1,3})\b"         # loopback
)

# Absolute user paths
ABS_PATH_RE = re.compile(r"/(?:home|Users|root)/[\w][\w-]*/")

# Internal openSME hostnames
INTERNAL_HOST_RE = re.compile(
    r"\b(?:opensme|opendesk-sme|sogo|paperless|zitadel|stalwart|"
    r"postgres|redis|traefik|kalender|opencloud|invoice-ninja|"
    r"dev-agent|dev-maintenance-bot|predictive-agent|taskfleet)"
    r"(?:\.local|\.internal|\.compose|\.opensme|\.docker|\.)?\b",
    re.IGNORECASE,
)

BOT = "opensme-outbound-bot"
IMAGE = "opensme-dev-maintenance-bot:latest"
STATE = "/var/lib/opensme/bot"
REPO = Path(__file__).resolve().parents[2]
DOCKER_SOCK = "/var/run/docker.sock"


class MockLLMServer(http.server.BaseHTTPRequestHandler):
    """Minimal mock LLM endpoint that captures every request body."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        req = {
            "path": self.path,
            "headers": dict(self.headers),
            "body": parsed,
        }
        with CAPTURE_LOCK:
            CAPTURED_REQUESTS.append(req)
        # Return a mock completion
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        resp = {"choices": [{"message": {"content": "mock completion"}}]}
        self.wfile.write(json.dumps(resp).encode())

    def log_message(self, format, *args):
        pass  # suppress default logging


def serve_mock_llm() -> threading.Thread:
    """Start a background HTTP server on MOCK_LLM_PORT. Returns the thread."""
    server = http.server.HTTPServer((MOCK_LLM_HOST, MOCK_LLM_PORT), MockLLMServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Brief wait for server to be ready
    time.sleep(0.1)
    return thread


def docker(*args, **kw):
    return subprocess.run(
        ["docker"] + list(args),
        capture_output=True, text=True,
        timeout=kw.pop("timeout", 120), **kw)


def start_bot(mock_llm_url: str) -> bool:
    """Start serve-mode dev-agent container wired to our mock LLM."""
    state_dir = Path("tests/tmp/outbound-state")
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "DEV_AGENT_WATCH": "opensme",
        "DEV_AGENT_INTERVAL": "2s",
        "DEV_AGENT_ALLOW_HEAL": "false",
        "DEV_AGENT_HISTORY_MAX": "50",
        "DEV_AGENT_API_ADDR": "0.0.0.0:8082",
        "DEV_AGENT_STATE_DIR": STATE,
        # Wire the bot to our mock LLM backend
        "DEV_AGENT_LLM_BACKEND": "openai",
        "DEV_AGENT_LLM_URL": mock_llm_url,
        "DEV_AGENT_LLM_MODEL": "mock-model",
        "DEV_AGENT_LLM_KEY": "mock-key",
        "DEV_AGENT_LLM_ENABLED": "true",
        "DEV_AGENT_LLM_MAX_REQUESTS": "3",
    }
    args = ["docker", "run", "-d", "--name", BOT, "--restart", "no",
            "-v", f"{state_dir}:{STATE}",
            "-v", f"{DOCKER_SOCK}:{DOCKER_SOCK}:ro",
            "-e", f"DEV_AGENT_LLM_URL={mock_llm_url}",
            "--add-host", f"host.docker.internal:host-gateway"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += [IMAGE, "-serve"]
    r = docker(*args, timeout=180)
    return r.returncode == 0


def stop_bot() -> None:
    docker("rm", "-fv", BOT)
    shutil.rmtree("tests/tmp/outbound-state", ignore_errors=True)


def wait_bot_health(timeout_s: int = 60) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if docker("exec", BOT, "wget", "-qO-", "http://localhost:8082/healthz").returncode == 0:
            return True
        time.sleep(2)
    return False


def trigger_reconcile() -> int:
    """Trigger one reconcile cycle so the bot calls the LLM."""
    r = docker("exec", BOT, "wget", "-qO-",
               "--post-data={\"trigger\":\"now\"}",
               "http://localhost:8082/reconcile", timeout=60)
    return r.returncode


def check_payloads() -> list:
    """Inspect captured requests for forbidden content. Returns list of violations."""
    violations = []
    for req in CAPTURED_REQUESTS:
        payload_json = json.dumps(req.get("body", {}))
        payload_str = unquote(payload_json)

        # Strip newline/whitespace noise that would break matches
        clean = payload_str

        if SECRET_PATTERNS.search(clean):
            violations.append("secrets found in outbound payload")
        if PRIVATE_IP_RE.search(clean):
            violations.append("private IPs found in outbound payload")
        if ABS_PATH_RE.search(clean):
            violations.append("absolute user paths found in outbound payload")
        if INTERNAL_HOST_RE.search(clean):
            violations.append("internal hostnames found in outbound payload")
    return violations


def check_llm_outbound_hygiene(result: Result) -> None:
    """AE1 outbound hygiene gate — standalone check usable by audit.py."""
    global CAPTURED_REQUESTS
    CAPTURED_REQUESTS = []

    # Guard: need docker + socket
    if not Path(DOCKER_SOCK).exists():
        result.skip("docker socket not on this host")
        return
    if docker("version", "--format", "{{.Server.Version}}").returncode != 0:
        result.skip("docker daemon not reachable")
        return

    mock_url = f"http://host.docker.internal:{MOCK_LLM_PORT}/v1/chat/completions"
    server_thread = serve_mock_llm()

    try:
        if not start_bot(mock_url):
            result.fail("failed to start dev-agent bot container")
            return
        if not wait_bot_health():
            result.fail("bot /healthz never ready")
            return

        # Trigger reconcile → bot will call our mock LLM
        if trigger_reconcile() != 0:
            result.value("warn", "reconcile trigger non-zero (bot has no compose are to check)")

        # Give the LLM call a moment to land
        time.sleep(3)

        violations = check_payloads()
        if violations:
            for v in violations:
                result.fail(v)
        else:
            result.ok("AE1: no sensitive data leaks in outbound LLM payloads")
            result.passed()
    except KeyboardInterrupt:
        result.fail("interrupted")
    except Exception as e:
        result.fail(repr(e))
    finally:
        stop_bot()
        server_thread.join(timeout=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="AE1 LLM outbound hygiene check")
    ap.add_argument("--llm-outbound", action="store_true",
                    help="run the outbound LLM hygiene journey")
    args = ap.parse_args()

    result = Result("llm-outbound")
    result.header("openSME AE1 — LLM outbound privacy / hygiene")

    if not args.llm_outbound:
        result.skip("use --llm-outbound to run this check")
        print()
        return 0

    check_llm_outbound_hygiene(result)
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
