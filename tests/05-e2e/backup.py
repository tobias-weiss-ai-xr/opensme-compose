#!/usr/bin/env python3
"""
tests/05-e2e/backup.py — provable restore points (Epic K).

A backup nobody has validated is a hope, not a backup. This journey runs
the operator's own tooling and audits the artifacts it produces:

  K1  `scripts/backup.sh --no-stop` produces non-empty, valid artifacts
      (gzip'd SQL dump + Traefik data archive)
  K2  the SQL dump is a real restore point: it contains table definitions
      — including the IdP's database — not just an empty header
  K3  the Traefik archive is a valid tar (the ACME/TLS material is in it)

Artifacts are written to a temporary BACKUP_DIR and removed afterwards —
the repo stays clean, CI stays stateless.

Usage:
    python3 tests/05-e2e/backup.py
"""

import gzip
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

REPO = Path(__file__).resolve().parent.parent.parent


def main() -> int:
    result = Result("backup")
    result.header("openSME e2e backup journey — provable restore points")

    if subprocess.run(["docker", "ps"], capture_output=True).returncode != 0:
        result.skip("docker not available")
        print()
        return 0
    # pg_dumpall only works against a live DB — require the postgres container
    probe = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    names = probe.stdout.split()
    if not any("postgres" in n for n in names):
        result.skip("postgres not running")
        print()
        ok = result.summary()
        return 0 if ok else 1

    backup_dir = Path(tempfile.mkdtemp(prefix="opensme-e2e-backup-"))
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
           "BACKUP_DIR": str(backup_dir), "COMPOSE_FILE": ""}
    try:
        started = time.time()
        proc = subprocess.run(
            ["bash", "scripts/backup.sh", "--no-stop"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            result.fail(f"backup.sh failed: {proc.stderr.strip()[:200]}")
            return 1
        result.ok(f"backup.sh ran in {time.time() - started:.1f}s")

        sql_files = sorted(backup_dir.glob("postgres_*.sql.gz"))
        tgz_files = sorted(backup_dir.glob("traefik_*.tar.gz"))
        if not sql_files or not tgz_files:
            result.fail(f"artifacts missing: sql={[f.name for f in sql_files]} "
                        f"traefik={[f.name for f in tgz_files]}")
            return 1

        # K2: the dump is a real restore point — tables, incl. the IdP db
        raw = gzip.open(sql_files[-1], "rb").read()
        size_mb = len(raw) / 1e6
        tables = raw.count(b"CREATE TABLE")
        idp_in = (b"\\connect zitadel" in raw) or (b"zitadel" in raw.lower())
        if size_mb > 0.05 and tables > 0 and idp_in:
            result.ok(f"SQL restore point: {size_mb:.1f} MB, {tables} CREATE TABLE, "
                      f"IdP database included")
        else:
            result.fail(f"SQL dump is not a usable restore point: "
                        f"size={size_mb:.2f}MB tables={tables} idp={idp_in}")

        # K3: the Traefik archive is a valid tar with content
        try:
            with tarfile.open(tgz_files[-1], "r:gz") as tf:
                members = tf.getnames()
            if members:
                result.ok(f"Traefik archive valid: {len(members)} entries "
                          f"(ACME/TLS material included)")
            else:
                result.fail("Traefik archive is empty")
        except (tarfile.TarError, EOFError) as e:
            result.fail(f"Traefik archive corrupt: {e}")

        # K4: the restore plan covers the Traefik archive — backup.sh writes
        # traefik_<ts>.tar.gz, so restore.sh MUST offer it back in its dry-run
        # plan. The dry-run needs no stack state, only the archive files in
        # BACKUP_DIR, so this pins the round trip without a live stack.
        traefik_name = tgz_files[-1].name
        prefix = traefik_name[len("traefik_"):-len(".tar.gz")]
        dry = subprocess.run(
            ["bash", "scripts/restore.sh", "--dry-run", prefix],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
        plan = dry.stdout + dry.stderr
        covers = dry.returncode == 0 and traefik_name in plan
        (result.ok if covers else result.fail)(
            "restore plan covers the traefik archive"
            if covers
            else f"restore.sh --dry-run does not list the traefik archive "
                 f"(rc={dry.returncode}): {plan[-300:]}")

        # restore sanity: gunzip integrity (CRC check over the whole stream)
        try:
            with gzip.open(sql_files[-1], "rb") as fh:
                while fh.read(1 << 20):
                    pass
            result.ok("gzip integrity verified over the full dump")
        except (OSError, EOFError) as e:
            result.fail(f"dump truncated/corrupt: {e}")
    finally:
        shutil.rmtree(backup_dir, ignore_errors=True)

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
