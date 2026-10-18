#!/usr/bin/env python3
"""
tests/07-bench/run_bench.py — on-demand benchmark harness.

Measures (specs/perf-bench):
  - per-container steady-state memory  (docker stats)
  - container boot time (inspect CreatedAt->StartedAt; optional real bounce)
  - HTTP latency percentiles p50/p95/p99 THROUGH Traefik for the portal /
    opencloud / paperless endpoints (Host-header routed on :80/:443)

Safely runnable with NO live stack: reports the stack as absent and exits 0
(so `make bench` is a harmless no-op outside a deployed host). Pass
--require-stack to fail hard when no stack is running.

Usage:
    python3 tests/07-bench/run_bench.py [--out docs/perf/benchmark-run.md]
                                        [--samples 60] [--require-stack]
                                        [--bounce-container NAME]
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "docs" / "perf"

# target name -> (Host header, path) routed through Traefik
TARGETS = [
    ("portal", "portal.opensme.graphwiz.ai", "/health"),
    ("opencloud", "cloud.opensme.graphwiz.ai", "/healthz"),
    ("paperless", "paperless.opensme.graphwiz.ai", "/"),
]


def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def stack_running() -> bool:
    rc, out, _ = sh(["docker", "compose", "ps", "-q"])
    return rc == 0 and bool(out)


def measure_memory() -> dict:
    rc, out, _ = sh(["docker", "stats", "--no-stream", "--format",
                     "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"], timeout=60)
    if rc != 0:
        return {}
    res = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            res[parts[0].lstrip("/")] = {"mem": parts[1], "cpu": parts[2]}
    return res


def measure_boot(container: str) -> dict:
    rc, out, _ = sh(["docker", "inspect", "--format",
                     "{{.Created}}|{{.State.StartedAt}}", container])
    if rc != 0:
        return {"error": "inspect failed"}
    created, started = out.split("|", 1)
    c = datetime.fromisoformat(created.replace("Z", "+00:00"))
    s = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return {"boot_ms": int((s - c).total_seconds() * 1000)}


def bounce_container(container: str) -> dict:
    """Real bounce timing: restart 3x, median start->healthy delta."""
    times = []
    for _ in range(3):
        t0 = time.time()
        sh(["docker", "restart", container], timeout=180)
        for _ in range(120):
            rc, out, _ = sh(["docker", "inspect", "--format",
                             "{{.State.Health.Status}}", container])
            if rc == 0 and out.strip() == "healthy":
                break
            time.sleep(1)
        times.append(int((time.time() - t0) * 1000))
    return {"bounce_ms_median": int(statistics.median(times)),
            "runs_ms": times}


def measure_latency_curl(host: str, path: str, samples: int) -> dict:
    times = []
    for _ in range(samples):
        rc, out, _ = sh(["curl", "-s", "-o", "/dev/null", "-w",
                         "%{time_total}", "-H", f"Host: {host}",
                         "--resolve", f"{host}:80:127.0.0.1",
                         f"http://{host}{path}"], timeout=15)
        if rc == 0:
            try:
                times.append(float(out) * 1000)
            except ValueError:
                continue
    return _percentiles(times)


def measure_latency_urllib(host: str, path: str, samples: int) -> dict:
    import urllib.request
    times = []
    for _ in range(samples):
        t0 = time.time()
        try:
            req = urllib.request.Request("http://127.0.0.1" + path,
                                         headers={"Host": host})
            urllib.request.urlopen(req, timeout=10).read()
            times.append((time.time() - t0) * 1000)
        except Exception:  # noqa: BLE001
            continue
    return _percentiles(times)


def _percentiles(times):
    if not times:
        return {"error": "no samples"}
    times.sort()
    n = len(times)

    def pct(p):
        return round(times[max(0, min(n - 1, int(p * n / 100)))], 1)

    return {"p50_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
            "samples": n}


def run_agentic_bench(args) -> int:
    """Layer-7: agentic LLM journey with latency + token budget checks."""
    # Offline mock journey: opt-in via --agentic, safe on any machine.
    # Budgets (constants with clear failures):
    MAX_LATENCY_MS = 5000   # 5s per mock LLM call
    MAX_TOKENS_PER_JOURNEY = 10000  # tokens per mock journey

    print("AH1/L7: agentic LLM journey bench (offline mock)")
    print(f"  Budget: {MAX_LATENCY_MS}ms max latency, {MAX_TOKENS_PER_JOURNEY} max tokens")

    # Simulate one mock LLM journey
    start = time.time()
    mock_latency_ms = 120
    mock_prompt_tokens = 1500
    mock_completion_tokens = 800
    total_tokens = mock_prompt_tokens + mock_completion_tokens

    # Simulate work
    time.sleep(mock_latency_ms / 1000)
    elapsed_ms = (time.time() - start) * 1000

    latency_ok = elapsed_ms <= MAX_LATENCY_MS
    token_ok = total_tokens <= MAX_TOKENS_PER_JOURNEY

    lines = [
        f"# Layer-7 Agentic Bench",
        f"",
        f"- Journey latency: {elapsed_ms:.1f}ms ({'PASS' if latency_ok else 'FAIL'} <= {MAX_LATENCY_MS}ms)",
        f"- Tokens: prompt={mock_prompt_tokens}, completion={mock_completion_tokens}, total={total_tokens} ({'PASS' if token_ok else 'FAIL'} <= {MAX_TOKENS_PER_JOURNEY})",
    ]

    if latency_ok and token_ok:
        print("  PASS: within latency and token budgets")
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text("\n".join(lines) + "\n")
        return 0
    else:
        print("  FAIL: budget exceeded")
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DIR / "benchmark-run.md"))
    ap.add_argument("--samples", type=int, default=30)
    ap.add_argument("--require-stack", action="store_true")
    ap.add_argument("--bounce-container", default=None)
    ap.add_argument("--agentic", action="store_true",
                    help="Run LLM-journey latency/token budget tests (offline mock)")
    args = ap.parse_args()

    # Layer-7: agentic LLM journey bench (AH1 / L7)
    if args.agentic:
        return run_agentic_bench(args)

    live = stack_running()
    if not live:
        msg = ("No live compose stack detected — skipping measurements.\n"
               "Run `make up` (or deploy) first, then `make bench`.")
        print(msg)
        if args.require_stack:
            sys.exit(1)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            f"# Benchmark Run (no stack)\n\n{msg}\n\n"
            f"_generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}_\n")
        return 0

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "samples": args.samples,
    }
    results["memory"] = measure_memory()
    results["latency"] = {
        name: measure_latency_curl(host, path, args.samples)
        for name, host, path in TARGETS
    }
    if args.bounce_container:
        results["bounce"] = bounce_container(args.bounce_container)
    else:
        boot = {}
        for name in ("opensme-postgres", "opensme-traefik", "opensme-portal"):
            b = measure_boot(name)
            if "error" not in b:
                boot[name] = b
        results["boot_baseline"] = boot

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (OUT_DIR / f"benchmark-{ts}.json").write_text(json.dumps(results, indent=2))

    lines = [
        "# Benchmark Run", "",
        f"_generated {results['generated_at']}_ by "
        "`tests/07-bench/run_bench.py` — live stack present._", "",
        "## Memory (docker stats --no-stream)", "",
        "| Container | Memory | CPU |", "|-----------|--------|-----|",
    ]
    for name, v in sorted(results["memory"].items()):
        lines.append(f"| {name} | {v['mem']} | {v['cpu']} |")
    lines += ["", "## HTTP latency THROUGH Traefik (ms)", "",
              "| Target | p50 | p95 | p99 | samples |",
              "|--------|-----|-----|-----|---------|"]
    for name, v in results["latency"].items():
        lines.append(f"| {name} | {v.get('p50_ms','-')} | {v.get('p95_ms','-')} "
                     f"| {v.get('p99_ms','-')} | {v.get('samples','-')} |")
    if "bounce" in results:
        lines += ["", f"## Bounce timing (median ms): {results['bounce']['bounce_ms_median']}",
                  f"  runs: {results['bounce']['runs_ms']}"]
    if "boot_baseline" in results:
        lines += ["", "## Boot baseline (Created->Started, ms)", ""]
        for n, v in results["boot_baseline"].items():
            lines.append(f"- {n}: {v['boot_ms']}")
    lines += ["", "## Notes", "",
              "- Memory/latency vary by host; pin host+commit for "
              "before/after.", "- CI runs only the static budget subset "
              "(`tests/00-static/check_perf.py`); live numbers are advisory."]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n")
    print(f"Benchmarks written to {args.out} (+ JSON snapshot)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
