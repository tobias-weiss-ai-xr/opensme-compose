#!/usr/bin/env python3
"""
tests/05-e2e/agent_saga.py — AS1: krank → Diagnose → Consent → Heal → Audit.

Proves the first agentic-AI saga end to end against the dev-maintenance-bot
(docs/AGENTIC-AI.md §7.3, contracts/agents.yml):

  A1  a watched container that turns unhealthy is surfaced as a Finding
  A2  without consent the heal is a dry-run receipt (nothing executed)
  A3  with DEV_AGENT_ALLOW_HEAL=true the heal truly restarts the container
  A4  every heal attempt lands in the evidence log (kind "heal") — auditable
  A5  metrics expose the heal counter

The test is fully isolated: it creates its own opensme-saga-* containers on
the host docker daemon (a serve-mode instance of the bot plus a mock app with
a deliberately failing healthcheck), drives the bot's private REST API via
docker exec (never publishing a host port), then tears both down. It is safe
against a live stack sharing the same host.

Usage:
  python3 tests/05-e2e/agent_saga.py

Exit codes: 0 = pass, 1 = failed (skips don't fail).
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result  # noqa: E402

BOT = "opensme-saga-bot"
MOCK = "opensme-saga-mock"
IMAGE = "opensme-dev-maintenance-bot:latest"
API = "http://localhost:8082"
STATE = "/var/lib/opensme/bot"
REPO = Path(__file__).resolve().parents[2]


def docker(*args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=kw.pop("timeout", 120), **kw)


def bot_exec(*args, timeout=30):
    """Run a command inside the bot container (busybox-alpine base)."""
    return docker("exec", BOT, *args, timeout=timeout)


def bot_http(path, post=None, timeout=60):
    """GET/POST the bot's private API via docker exec (no host port)."""
    if post is None:
        return bot_exec("wget", "-q", "-O", "-", API + path, timeout=timeout)
    return bot_exec("wget", "-q", "-O", "-",
                    "--post-data=" + json.dumps(post), API + path, timeout=timeout)


def wait_until(what, timeout_s, fn):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        ok, last = fn()
        if ok:
            return True, last
        time.sleep(2)
    return False, last


def run_bot(allow_heal: bool, extra_env: dict | None = None):
    """Start a fresh serve-mode bot container; returns stderr/error string."""
    env = dict(extra_env or {})
    env.update({
        "DEV_AGENT_WATCH": "opensme",
        "DEV_AGENT_INTERVAL": "2s",   # re-reconcile fast so a just-unhealthy
                                        # mock is found promptly (CI timing)
        "DEV_AGENT_ALLOW_HEAL": "true" if allow_heal else "false",
        "DEV_AGENT_HISTORY_MAX": "50",
        "DEV_AGENT_API_ADDR": "0.0.0.0:8082",
    })
    args = ["docker", "run", "-d", "--name", BOT, "--restart", "no",
            "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
            "--mount", f"type=tmpfs,destination={STATE}"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += [IMAGE, "-serve"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    return r.returncode, (r.stderr or r.stdout).strip()


def stop_bot():
    docker("rm", "-f", BOT, timeout=60)


def main() -> int:
    ap = argparse.ArgumentParser(description="AS1 agent saga (heal with consent)")
    ap.add_argument("--keep", action="store_true", help="leave saga containers up on failure")
    args = ap.parse_args()

    result = Result("agent-saga")
    result.header("openSME e2e agent saga AS1 (dev-maintenance-bot)")

    # Guard: host docker daemon + socket present.
    if not Path("/var/run/docker.sock").exists():
        result.skip("docker socket not on this host (need host daemon)")
        print()
        return 0
    ping = docker("version", "--format", "{{.Server.Version}}")
    if ping.returncode != 0:
        result.skip("docker daemon not reachable")
        print()
        return 0

    keep = args.keep
    try:
        # A0: the bot image exists (built if missing).
        img = docker("image", "inspect", IMAGE)
        if img.returncode != 0:
            result.info("building opensme-dev-maintenance-bot:latest from repo")
            b = subprocess.run(
                ["docker", "build", "-f", "opensme-dev-agent/Dockerfile",
                 "-t", IMAGE, "."],
                cwd=REPO, capture_output=True, text=True, timeout=600)
            (result.ok if b.returncode == 0 else result.fail)(
                "bot image builds" if b.returncode == 0
                else f"bot image build failed: {b.stderr[-300:]}")
            if b.returncode != 0:
                return 1
        result.ok(f"bot image present: {IMAGE}")

        # Mock app with a deliberately failing healthcheck → unhealthy.
        # It carries the compose project/service labels so the bot's watch
        # filter (com.docker.compose.project == opensme) recognizes it as
        # part of the watched stack.
        docker("rm", "-f", MOCK, timeout=30)
        m = subprocess.run(
            ["docker", "run", "-d", "--name", MOCK, "--restart", "no",
             "--label", "com.docker.compose.project=opensme",
             "--label", "com.docker.compose.service=saga-mock",
             "--health-cmd", "exit 1", "--health-interval", "1s",
             "--health-timeout", "1s", "--health-retries", "1",
             "alpine:latest", "sleep", "600"],
            capture_output=True, text=True, timeout=120)
        (result.ok if m.returncode == 0 else result.fail)(
            "mock app started" if m.returncode == 0 else f"mock failed: {m.stderr}")
        if m.returncode != 0:
            return 1

        # ── phase 1: consent OFF (the safe default) ──────────────────────
        rc, err = run_bot(allow_heal=False)
        if rc != 0:
            result.fail(f"bot (consent off) did not start: {err}")
            return 1
        result.ok("bot started consent-off (DEV_AGENT_ALLOW_HEAL=false)")

        ok, _ = wait_until("bot health", 90,
                           lambda: (bot_http("/healthz").returncode == 0, None))
        (result.ok if ok else result.fail)("bot private API is up (/healthz)")
        if not ok:
            return 1

        # A1: the mock surfaces as an unhealthy finding.
        def mock_seen():
            r = bot_http("/status")
            if r.returncode != 0:
                return False, None
            return "opensme-saga-mock" in r.stdout, r.stdout

        ok, status = wait_until("unhealthy finding", 90, mock_seen)
        (result.ok if ok else result.fail)(
            "A1 watched mock surfaced as a Finding (krank → Diagnose)"
            if ok else f"A1 mock not surfaced: {status[:200] if status else 'no status'}")

        # A2: heal without consent → dry-run receipt, nothing executed.
        heal = bot_http("/heal", {"action": "restart", "target": MOCK})
        dry = False
        if heal.returncode == 0:
            try:
                dry = json.loads(heal.stdout).get("executed") is False
            except json.JSONDecodeError:
                pass
        (result.ok if heal.returncode == 0 and dry else result.fail)(
            "A2 heal without consent is a dry-run receipt (Executed=false)"
            if heal.returncode == 0 and dry
            else f"A2 consent gate failed: rc={heal.returncode} body={heal.stdout[:200]}")

        # A4a: the dry-run attempt is in the evidence log.
        ev = bot_http("/evidence")
        audited = False
        if ev.returncode == 0:
            try:
                entries = json.loads(ev.stdout)
                audited = any(
                    e.get("kind") == "heal" and e.get("what", "").find("executed=false") >= 0
                    for e in entries)
            except json.JSONDecodeError:
                pass
        (result.ok if audited else result.fail)(
            "A4 dry-run heal recorded in evidence log (kind heal)")
        stop_bot()

        # ── phase 2: consent ON ──────────────────────────────────────────
        keep = False  # once we pose a real restart we always clean up
        rc, err = run_bot(allow_heal=True)
        (result.ok if rc == 0 else result.fail)(f"bot restarted consent-on: {err if rc else ''}")
        if rc != 0:
            return 1
        ok, _ = wait_until("bot health (consent on)", 90,
                           lambda: (bot_http("/healthz").returncode == 0, None))
        if not ok:
            result.fail("bot did not come back healthy consent-on")
            return 1

        # A3: heal with consent truly restarts the container.
        heal2 = bot_http("/heal", {"action": "restart", "target": MOCK})
        done = False
        if heal2.returncode == 0:
            try:
                done = json.loads(heal2.stdout).get("executed") is True
            except json.JSONDecodeError:
                pass
        (result.ok if done else result.fail)(
            "A3 heal with consent restarts the container (Executed=true)"
            if done else f"A3 real heal failed: rc={heal2.returncode} body={heal2.stdout[:200]}")

        # A4b: the real heal is also audited.
        ev2 = bot_http("/evidence")
        audited2 = False
        if ev2.returncode == 0:
            try:
                entries2 = json.loads(ev2.stdout)
                audited2 = any(
                    e.get("kind") == "heal" and e.get("what", "").find("executed=true") >= 0
                    for e in entries2)
            except json.JSONDecodeError:
                pass
        (result.ok if audited2 else result.fail)(
            "A4 real heal recorded in evidence log (kind heal, executed=true)")

        # A5: metrics expose the heal counter (1 heal this consent-on run).
        met = bot_http("/metrics")
        mm = re.search(r"dev_agent_heals_total (\d+)", met.stdout) if met.returncode == 0 else None
        metric_ok = mm is not None and int(mm.group(1)) >= 1
        (result.ok if metric_ok else result.fail)(
            "A5 /metrics exposes dev_agent_heals_total (>=1 heal)"
            if metric_ok else f"A5 metrics: rc={met.returncode} body={met.stdout[:200]}")
    finally:
        # Teardown: remove saga containers (host-only, never touches the stack).
        subprocess.run(["docker", "rm", "-f", BOT, MOCK],
                       capture_output=True, text=True, timeout=120)

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
