#!/usr/bin/env python3
"""
tests/05-e2e/restore_drill.py — disaster recovery drill (Epic Y).

backup.py proves artifacts EXIST; this journey proves they RESTORE.
A restore path that has never been drilled is fiction. The drill runs
the operator's own tooling end to end:

  Y1  backup.sh produces a complete restore point (SQL dump + volume
      archives) containing the drill marker database
  Y2  restore.sh --dry-run accepts the prefix and lists the artifacts
  Y3  the drill: the marker database is DESTROYED for real (DROP),
      then `restore.sh` (the interactive script, answered 'yes' on
      stdin) brings the stack down, restores PostgreSQL + volumes and
      boots it back up
  Y4  the marker row is back — byte-for-byte the value we buried
  Y5  the stack is healthy on the public route afterwards

The drill drives the stack lifecycle and ALWAYS restores it in the
finally block (self-heal via ensure_portal_routed / demo.sh re-boot).

Usage:
    python3 tests/05-e2e/restore_drill.py [domain]  # default: opensme.local
"""

import argparse
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ensure_portal_routed, print_boot_failure

import requests

T = 20
LOCALISH_SUFFIXES = (".local", ".localhost", ".test")
DEMO_SET = "docker-compose.yml:idm/zitadel.yml:opencloud/opencloud.yml:profiles/demo.dev.yml"
DB = "e2e_restore_drill"
MARKER = "opensme was buried here at %s"


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


def drill_env(backup_dir: Path) -> dict:
    env = dict(os.environ)
    env["COMPOSE_FILE"] = DEMO_SET
    env["COMPOSE_PROFILES"] = "standalone"
    env["BACKUP_DIR"] = str(backup_dir)
    return env


def psql(env, sql: str, timeout: int = 60,
         db: str = "postgres") -> tuple[int, str]:
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "opensme", "-d", db, "-Atc", sql],
        env=env, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def run_script(env, script: str, *args: str, timeout: int = 900,
               stdin_text: str | None = None):
    return subprocess.run(["bash", script, *args], env=env,
                          input=stdin_text, capture_output=True,
                          text=True, timeout=timeout)


def main() -> int:
    ap = argparse.ArgumentParser(description="restore drill journey")
    ap.add_argument("domain", nargs="?", default="opensme.local")
    args = ap.parse_args()

    install_dns_fallback()
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = Result("restore-drill")
    result.header(f"openSME e2e restore drill — domain={args.domain}")

    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True).stdout.split()
    if not any("opensme-portal" in n for n in names) or \
            not any("opensme-postgres" in n or "postgres" in n for n in names):
        result.skip("portal/postgres not running (start the stack first)")
        print()
        return 0

    portal = f"https://portal.{args.domain}"
    session = requests.Session()
    session.verify = False
    session.trust_env = False

    backup_dir = Path(tempfile.mkdtemp(prefix="opensme-e2e-restore-"))
    env = drill_env(backup_dir)
    drilled = False
    try:
        # ── Y1: backup with the marker buried in it ─────────────────
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        rc, _ = psql(env, f"DROP DATABASE IF EXISTS {DB}")
        if rc != 0:
            result.fail("could not reach postgres for the drill database")
            return 1
        psql(env, f"CREATE DATABASE {DB}")
        # the marker must be IN the drill database (CREATE DATABASE makes a
        # database, not a schema) — and the insert is verified, not assumed
        rc, out = psql(env, "CREATE TABLE drill (note text)", db=DB)
        rc2, out2 = psql(env, f"INSERT INTO drill VALUES ('{MARKER % stamp}')",
                         db=DB)
        if rc != 0 or rc2 != 0:
            result.fail(f"could not plant marker: {out or out2}")
            return 1

        # --volumes: the drill must also exercise the volume restore path;
        # the script downs the stack itself and brings it back up after
        proc = run_script(env, "scripts/backup.sh", "--volumes")
        if proc.returncode != 0:
            result.fail(f"backup.sh failed: rc={proc.returncode} "
                        f"{(proc.stderr or proc.stdout)[-200:]}")
            return 1
        prefixes = sorted(p.name[len("postgres_"):-len(".sql.gz")]
                          for p in backup_dir.glob("postgres_*.sql.gz"))
        if not prefixes:
            result.fail("backup produced no postgres dump")
            return 1
        prefix = prefixes[-1]
        vols = backup_dir / f"volumes_{prefix}.tar.gz"
        dump = backup_dir / f"postgres_{prefix}.sql.gz"
        complete = vols.exists() and vols.stat().st_size > 0 and \
            dump.stat().st_size > 1000
        (result.ok if complete else result.fail)(
            f"restore point complete: postgres_{prefix}.sql.gz "
            f"({dump.stat().st_size}B) + volumes.tar.gz "
            f"({vols.stat().st_size if vols.exists() else 0}B)"
            if complete
            else "restore point incomplete (missing/empty artifacts)"
        )

        # ── Y2: dry-run accepts the prefix ──────────────────────────
        proc = run_script(env, "scripts/restore.sh", "--dry-run", prefix)
        y2 = proc.returncode == 0 and prefix in proc.stdout
        (result.ok if y2 else result.fail)(
            f"restore.sh --dry-run accepts prefix {prefix}"
            if y2
            else f"dry-run broken: rc={proc.returncode}"
        )

        # ── Y3: the drill — destroy, then restore ───────────────────
        rc, _ = psql(env, f"DROP DATABASE {DB}")
        rc2, out = psql(env, "SELECT 1 FROM pg_database "
                             f"WHERE datname='{DB}'")
        gone = rc == 0 and rc2 == 0 and out == ""
        (result.ok if gone else result.fail)(
            "drill marker database destroyed for real (DROP verified)"
            if gone
            else f"could not verify destruction: rc={rc}/{rc2} out={out!r}"
        )

        proc = run_script(env, "scripts/restore.sh", prefix,
                          stdin_text="yes\n", timeout=900)
        drilled = proc.returncode == 0
        (result.ok if drilled else result.fail)(
            "restore.sh completed (stack down → restore → up)"
            if drilled
            else f"restore.sh failed: rc={proc.returncode} "
                 f"{(proc.stderr or proc.stdout)[-300:]}"
        )

        # ── Traefik ACME material returns after restore ────────────
        # restore.sh now applies traefik_<ts>.tar.gz to the traefik-data
        # volume. The only GUARANTEED archive member is the file-provider
        # config dynamic.yml (bind-mounted into /etc/traefik, so it is
        # always captured by backup.sh's tar). acme.json itself is NOT
        # deterministic: on a .local demo domain with LE unreachable
        # (offline CI) traefik never writes it — asserting it would flake.
        # Asserting dynamic.yml proves the archive was actually applied to
        # the volume; the gravity of the ACME state lives in restore.sh's
        # own loud failure on an invalid/missing archive.
        traefik_archive = backup_dir / f"traefik_{prefix}.tar.gz"
        if traefik_archive.exists():
            proj = env.get("COMPOSE_PROJECT_NAME", "opensme-compose")
            vol = f"{proj}_traefik-data"
            ls = subprocess.run(
                ["docker", "run", "--rm",
                 "-v", f"{vol}:/data", "alpine:3.20", "ls", "-a", "/data"],
                capture_output=True, text=True, timeout=120)
            listing = ls.stdout + ls.stderr
            carries = ls.returncode == 0 and \
                any(n in listing for n in ("dynamic.yml", "acme.json"))
            (result.ok if carries else result.fail)(
                "restored traefik volume carries the ACME/TLS material "
                "(archive applied)"
                if carries
                else f"traefik volume {vol} missing restored content: "
                     f"{listing[-200:]}")
        else:
            result.warn("no traefik archive produced by this backup run — "
                        "volume-level traefik check skipped")

        # ── Y4: the marker is back ──────────────────────────────────
        time.sleep(8)  # postgres post-restore settle
        rc, val = psql(env, "SELECT note FROM drill LIMIT 1", db=DB,
                       timeout=120)
        y4 = rc == 0 and val == MARKER % stamp
        (result.ok if y4 else result.fail)(
            "marker row resurrected byte-for-byte from the dump"
            if y4
            else f"marker not restored: rc={rc} val={val!r}"
        )

        # ── Y5: stack healthy on the public route ───────────────────
        if ensure_portal_routed(session, portal):
            health = session.get(portal + "/health", timeout=T)
            (result.ok if health.status_code == 200 else result.fail)(
                "stack healthy on the public route after the drill"
                if health.status_code == 200
                else f"portal answered {health.status_code} after restore"
            )
        else:
            result.fail("portal unreachable via traefik after restore")
    finally:
        # guarantee: stack back to the demo state, drill DB gone
        psql(env, f"DROP DATABASE IF EXISTS {DB}")
        if not ensure_portal_routed(session, portal):
            rb = subprocess.run(["bash", "scripts/demo.sh"], cwd=str(Path.cwd()),
                                env=drill_env(backup_dir), capture_output=True,
                                text=True, timeout=1200)
            # recovery boot: if THIS fails too, the stack is left broken —
            # the operator must see why, not just a silent finally block.
            if rb.returncode != 0:
                print(f"--- recovery boot FAILED rc={rb.returncode} — stack may be down")
                print_boot_failure(rb, "demo.sh recovery")
            ensure_portal_routed(session, portal)
        import shutil
        shutil.rmtree(backup_dir, ignore_errors=True)

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
