#!/usr/bin/env python3
"""
cloud_mock_support.py — shared cloud stand-in container for journeys.

Runs a real container (`opensme-e2e-cloud`, python:3-alpine) on the
compose network so the PORTAL resolves it by DNS NAME — keeping the
SSRF guard's IP-literal rejection live during tests. Adds an optional
extra --network-alias (e.g. cloud.meine-firma.test) so journeys can
prove domain-derived defaults. Serves files with a Content-Disposition
attachment header (filename = basename of the request path, URL-decoded
like a real cloud would).
"""

import time
from pathlib import Path

CLOUD_NAME = "opensme-e2e-cloud"
CLOUD_PORT = 8099
HERE = Path(__file__).resolve().parent


def start_cloud_standin(tmpdir: Path, alias: str | None = None,
                        port: int = CLOUD_PORT) -> bool:
    """Start the stand-in via docker create + cp + start (NOT `exec -d`
    — exec-spawned servers proved flaky as an attachment source).

    The mock writes its own fixture files on startup; journeys only
    supply this script.
    """
    import subprocess
    subprocess.run(["docker", "rm", "-f", CLOUD_NAME],
                   capture_output=True, timeout=60)
    cmd = ["docker", "create", "--name", CLOUD_NAME,
           "--network", "opensme-net"]
    if alias:
        cmd += ["--network-alias", alias]
    cmd += ["python:3-alpine", "python3", "/srv/cloud_mock.py", str(port)]
    create = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if create.returncode != 0:
        return False
    cp = subprocess.run(["docker", "cp", str(HERE / "cloud_mock.py"),
                         f"{CLOUD_NAME}:/srv/cloud_mock.py"],
                        capture_output=True, timeout=60)
    if cp.returncode != 0:
        return False
    start = subprocess.run(["docker", "start", CLOUD_NAME],
                           capture_output=True, timeout=120)
    if start.returncode != 0:
        return False
    for _ in range(20):
        probe = subprocess.run(
            ["docker", "exec", CLOUD_NAME, "python3", "-c",
             (f"import socket;s=socket.create_connection(('127.0.0.1',{port}),2);"
             "s.close()")],
            capture_output=True, timeout=30)
        if probe.returncode == 0:
            return True
        time.sleep(0.5)
    return False


def stop_cloud_standin() -> None:
    import subprocess
    subprocess.run(["docker", "rm", "-f", CLOUD_NAME],
                   capture_output=True, timeout=60)
