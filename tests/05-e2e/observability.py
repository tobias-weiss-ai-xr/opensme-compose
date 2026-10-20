#!/usr/bin/env python3
"""
tests/05-e2e/observability.py — health + log hygiene (Epic W).

The operator's contract with the stack: services self-report health,
logs are structured, and — above all — logs never leak secrets.

  W1  every running opensme-* container WITH a healthcheck reports
      healthy (containers younger than their start period are skipped)
  W2  every secret-shaped .env value (PASS/SECRET/TOKEN/KEY/SALT,
      >=10 chars) is absent from the last 4000 log lines of every
      opensme container
  W3  the portal's recent log lines carry an RFC-3339 timestamp and a
      level (tracing format) — aggregation works out of the box

Read-only: recreates nothing, safe to run anywhere.

Usage:
    python3 tests/05-e2e/observability.py [domain]
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

T = 15
LOG_TAIL = 4000
SECRET_KEY_RE = re.compile(r"PASS|SECRET|TOKEN|KEY|SALT", re.IGNORECASE)
RFC3339_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\s+\S*(INFO|WARN|ERROR|DEBUG)")


def load_env_secrets() -> dict[str, str]:
    """Secret-shaped values from .env (the demo stack's live passwords)."""
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    secrets: dict[str, str] = {}
    if not env_path.exists():
        return secrets
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not v or len(v) < 10 or v.lower() in ("true", "false"):
            continue
        if SECRET_KEY_RE.search(k):
            secrets[k] = v
    return secrets


def list_opensme_containers() -> list[dict]:
    r = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}", "--filter", "name=opensme-"],
        capture_output=True, text=True, timeout=60)
    return [n for n in r.stdout.split() if n]


def container_logs(name: str, tail: int = LOG_TAIL) -> str:
    r = subprocess.run(["docker", "logs", "--tail", str(tail), name],
                       capture_output=True, text=True, timeout=120)
    return r.stdout + r.stderr


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_go_duration(s: str) -> float:
    """Parse docker's duration format ("10s", "1m30s", "0s") to seconds."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s or "")
    if not m or not any(m.groups()):
        return 0.0
    h, mi, se = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)


def main() -> int:
    ap = argparse.ArgumentParser(description="observability journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    result = Result("observability")
    result.header(f"openSME e2e observability journey — domain={args.domain}")

    containers = list_opensme_containers()
    if not containers:
        result.skip("no opensme containers running (start the stack first)")
        print()
        return 0

    # ── W1: healthchecks report healthy ─────────────────────────────
    inspected = []
    for name in containers:
        r = subprocess.run(
            ["docker", "inspect", name, "--format",
             ('{"health": {{if .Config.Healthcheck}}true{{else}}false{{end}},'
             '"status": "{{if .State.Health}}{{.State.Health.Status}}{{end}}",'
             '"start_period": "{{if .Config.Healthcheck}}{{.Config.Healthcheck.StartPeriod}}{{end}}",'
             '"started": "{{.State.StartedAt}}"}')],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            continue
        inspected.append((name, json.loads(r.stdout)))

    with_hc = [(n, d) for n, d in inspected if d["health"]]
    if not with_hc:
        result.skip("no opensme containers with healthchecks running")
    else:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        young = []
        unhealthy = []
        for name, d in with_hc:
            started = datetime.datetime.fromisoformat(
                d["started"].replace("Z", "+00:00"))
            start_period = parse_go_duration(d.get("start_period") or "0s")
            if (now - started).total_seconds() < max(start_period, 60.0):
                young.append(name)
            elif d["status"] != "healthy":
                unhealthy.append(f"{name}={d['status'] or '?'}")
        (result.ok if not unhealthy else result.fail)(
            f"{len(with_hc)} healthchecked containers report healthy"
            if not unhealthy else f"unhealthy: {', '.join(unhealthy)}"
        )
        for n in young:
            result.info(f"{n} inside its start period — skipped, not judged")

    # ── W2: secrets never reach the logs ────────────────────────────
    secrets = load_env_secrets()
    if not secrets:
        result.skip(".env not found — secret scan not possible")
    else:
        leaks = []
        for name in containers:
            logs = container_logs(name)
            for k, v in secrets.items():
                if v in logs:
                    leaks.append(f"{k} in {name}")
        (result.ok if not leaks else result.fail)(
            f"{len(secrets)} secret-shaped .env values absent from "
            f"{len(containers)} containers' logs (last {LOG_TAIL} lines each)"
            if not leaks else f"SECRET LEAK: {'; '.join(leaks)}"
        )

    # ── W3: portal logs are structured (RFC 3339 + level) ───────────
    r = subprocess.run(
        ["docker", "logs", "--tail", "30", "opensme-portal"],
        capture_output=True, text=True, timeout=60)
    lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
    if not lines:
        result.skip("portal has no log lines yet")
    else:
        # multiline output (panics, stack traces) is allowed; the bulk
        # of recent lines must be structured. tracing paints its output
        # (ANSI) even on non-tty docker logs — strip before matching.
        clean = [ANSI_RE.sub("", ln) for ln in lines]
        structured = sum(1 for ln in clean if RFC3339_PREFIX.match(ln))
        ratio_ok = structured >= max(1, int(len(lines) * 0.8))
        (result.ok if ratio_ok else result.fail)(
            f"portal logs structured: {structured}/{len(lines)} recent lines "
            "carry RFC-3339 + level"
            if ratio_ok
            else f"portal logs unstructured: only {structured}/{len(lines)} "
                 "recent lines have an RFC-3339 timestamp + level"
        )

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
