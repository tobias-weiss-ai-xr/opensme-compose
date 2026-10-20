#!/usr/bin/env python3
"""
tests/00-static/check_platform.py — Runtime platform minimum-version preflight.

Verifies that the orchestration platforms the stack targets meet a minimum
version. Two checks:

  1. k3s on scs-k3s      — kubectl server + node kubelet versions
  2. Docker on vhrz2392  — `ssh <host> docker version` server version

Semantics:
  version >= minimum          -> PASS  (reports the running version)
  version <  minimum          -> FAIL  (exit code 1)
  target/tool unreachable     -> SKIP  (clear reason, exit code stays 0)

Usage:
    python3 tests/00-static/check_platform.py
    python3 tests/00-static/check_platform.py --kubectl-min 1.28.0 --docker-min 24.0.0
    python3 tests/00-static/check_platform.py --kubectl-context scs-k3s --ssh-host vhrz2392
    python3 tests/00-static/check_platform.py --strict

Exit codes:
    0 = all reachable platforms meet minimums (skips do not fail)
    1 = a reachable platform is below its minimum
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Add parent to path for conftest import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

# ─── Defaults (overridable via CLI) ────────────────────────────────
KUBECTL_MIN = "1.30.0"      # minimum k3s Kubernetes version
DOCKER_MIN = "24.0.0"       # minimum Docker Engine version
SSH_HOST = "vhrz2392"       # docker host (resolved via ~/.ssh/config)
KUBECTL_CONTEXT = None      # None = use current kubectl context


def parse_version(s):
    """Parse a version string into a comparable tuple of ints.

    Handles common suffixes: 'v1.36.3+k3s1' -> (1,36,3),
    '26.1.5+dfsg1' -> (26,1,5), '1.28.0-rc1' -> (1,28,0).
    Returns None if no dotted numeric version can be found.
    """
    if not s:
        return None
    text = s.strip().lstrip("vV")
    text = re.split(r"[+~_]", text)[0]
    m = re.match(r"^(\d+(?:\.\d+){0,3})", text)
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def meets_minimum(version, minimum):
    """True if version tuple >= minimum tuple (zero-padded comparison)."""
    if version is None or minimum is None:
        return False
    width = max(len(version), len(minimum))
    return tuple(version) + (0,) * (width - len(version)) >= \
        tuple(minimum) + (0,) * (width - len(minimum))


def run_cmd(cmd, timeout=20):
    """Run a command, return (returncode, stdout). Suppress stderr noise."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return -1, f"could not run: {exc}"


def check_k3s(result, kubectl_min, context):
    """Check k3s (server + nodes) against kubectl_min."""
    if not shutil.which("kubectl"):
        result.skip("kubectl not found on PATH — k3s version check skipped")
        return

    ctx_args = ["--context", context] if context else []
    rc, current_ctx = run_cmd(["kubectl", "config", "current-context"])
    ctx_label = context or (current_ctx if rc == 0 else "current")

    rc, out = run_cmd(["kubectl"] + ctx_args + ["version", "--output=json"])
    if rc != 0:
        result.skip(
            f"cluster unreachable via kubectl (context '{ctx_label}') — "
            f"k3s version check skipped: {out[:120] or 'command failed'}"
        )
        return

    try:
        data = json.loads(out)
        server_version = data.get("serverVersion", {}).get("gitVersion", "")
    except (json.JSONDecodeError, AttributeError):
        result.skip("kubectl version output was not valid JSON — k3s check skipped")
        return

    server_tuple = parse_version(server_version)
    min_tuple = parse_version(kubectl_min)

    if server_tuple is None:
        result.fail(f"could not parse k3s server version '{server_version}'")
        return

    desc = f"k3s server {server_version} (context '{ctx_label}')"
    if meets_minimum(server_tuple, min_tuple):
        result.ok(f"{desc} >= {kubectl_min}")
    else:
        result.fail(f"{desc} < minimum {kubectl_min}")

    # Node kubelet versions: every node must meet the minimum too.
    rc, out = run_cmd(["kubectl"] + ctx_args + ["get", "nodes", "-o", "json"])
    if rc != 0:
        result.warn("could not read node kubelet versions (nodes check skipped)")
        return
    try:
        nodes = json.loads(out)["items"]
    except (json.JSONDecodeError, KeyError):
        result.warn("could not parse node list (nodes check skipped)")
        return

    for node in nodes:
        name = node["metadata"]["name"]
        kubelet = (node.get("status", {}).get("nodeInfo", {}) or {}).get("kubeletVersion", "")
        kubelet_tuple = parse_version(kubelet)
        if kubelet_tuple is None:
            result.warn(f"node {name}: unparseable kubelet version '{kubelet}'")
        elif meets_minimum(kubelet_tuple, min_tuple):
            result.ok(f"node {name}: kubelet {kubelet} >= {kubectl_min}")
        else:
            result.fail(f"node {name}: kubelet {kubelet} < minimum {kubectl_min}")


def check_docker(result, docker_min, ssh_host):
    """Check Docker server version on ssh_host against docker_min."""
    if not shutil.which("ssh"):
        result.skip("ssh not found on PATH — Docker version check skipped")
        return

    remote_cmd = "docker version --format '{{.Server.Version}}'"
    rc, out = run_cmd(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_host, remote_cmd],
        timeout=25,
    )
    if rc != 0 or not out:
        result.skip(
            f"could not reach '{ssh_host}' or Docker daemon not responding "
            f"({out[:120] or 'ssh/daemon error'}) — Docker version check skipped"
        )
        return

    docker_tuple = parse_version(out)
    min_tuple = parse_version(docker_min)
    if docker_tuple is None:
        result.fail(f"could not parse Docker version '{out}' on {ssh_host}")
        return

    desc = f"Docker {out} on {ssh_host}"
    if meets_minimum(docker_tuple, min_tuple):
        result.ok(f"{desc} >= {docker_min}")
    else:
        result.fail(f"{desc} < minimum {docker_min}")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubectl-min", default=KUBECTL_MIN,
                        help=f"minimum k3s version (default {KUBECTL_MIN})")
    parser.add_argument("--docker-min", default=DOCKER_MIN,
                        help=f"minimum Docker version (default {DOCKER_MIN})")
    parser.add_argument("--kubectl-context", default=KUBECTL_CONTEXT,
                        help="kubectl context to test (default: current context)")
    parser.add_argument("--ssh-host", default=SSH_HOST,
                        help=f"docker host to test (default {SSH_HOST})")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    result = Result("platform-versions")
    result.header("Layer 0: Runtime platform minimum versions")

    result.info(f"kubectl->{args.kubectl_context or 'current-context'}, "
                f"ssh->{args.ssh_host}")
    check_k3s(result, args.kubectl_min, args.kubectl_context)
    check_docker(result, args.docker_min, args.ssh_host)

    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
