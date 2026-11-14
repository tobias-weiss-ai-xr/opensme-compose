#!/usr/bin/env python3
"""
tests/00-static/check_notes_broker.py — Layer 0 guard for the notes (impress)
celery broker authentication contract.

The impress image defaults CELERY_BROKER_URL to a passwordless
redis://redis:6379/0 while our redis requires AUTH, so every document
creation (POST /api/v1.0/documents/) died with redis AuthenticationError ->
kombu OperationalError -> HTTP 500 on /docs/new/ (2026-11-14 outage).

Two traps are pinned here so they cannot silently regress:

  B1 — env-var NAME: django-configurations' values.Value reads env vars with
       the 'DJANGO_' prefix by default, so a plain CELERY_BROKER_URL entry
       in compose is silently ignored (the image default survives). The
       override MUST be named DJANGO_CELERY_BROKER_URL. (REDIS_URL only
       works because settings declares it explicitly with environ_name.)
  B2 — AUTH: the value MUST carry REDIS_PASSWORD (no passwordless default).
       A value of redis://redis:6379/0 re-introduces the outage.

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_notes_broker.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

NOTES_YML = ROOT / "services" / "notes.yml"
SERVICE = "notes-backend"


def check(room: Result) -> int:
    room.header("Layer 0: notes celery broker auth contract")
    if not NOTES_YML.is_file():
        room.fail(f"missing {NOTES_YML.relative_to(ROOT)}")
        return 1

    import yaml
    data = yaml.safe_load(NOTES_YML.read_text()) or {}
    svc = (data.get("services") or {}).get(SERVICE) or {}
    if not svc:
        room.fail(f"{NOTES_YML.relative_to(ROOT)}: service '{SERVICE}' missing")
        return 1

    env = svc.get("environment") or {}
    if isinstance(env, list):
        env = {k: v for item in env for k, _, v in [item.partition("=")]}

    # B1: the correctly-named key must exist; the ignored name must not.
    if "DJANGO_CELERY_BROKER_URL" in env:
        room.ok("B1: DJANGO_CELERY_BROKER_URL is declared (correct name)")
    else:
        room.fail(
            "B1: services/notes.yml notes-backend must declare "
            "DJANGO_CELERY_BROKER_URL — a plain CELERY_BROKER_URL is "
            "silently ignored by django-configurations (DJANGO_ prefix) and "
            "the image's passwordless default survives"
        )
    if "CELERY_BROKER_URL" in env:
        room.fail(
            "B1: services/notes.yml declares CELERY_BROKER_URL, which "
            "django-configurations ignores (DJANGO_ prefix) — remove it in "
            "favour of DJANGO_CELERY_BROKER_URL"
        )
    else:
        room.ok("B1: no ignored CELERY_BROKER_URL entry")

    value = str(env.get("DJANGO_CELERY_BROKER_URL", "")).strip()
    if not value:
        room.fail("B2: DJANGO_CELERY_BROKER_URL is empty")
        return 0 if room.summary() else 1

    # B2: must carry REDIS_PASSWORD (no passwordless default).
    if "REDIS_PASSWORD" in value:
        room.ok("B2: broker URL references ${REDIS_PASSWORD}")
    else:
        room.fail(
            "B2: DJANGO_CELERY_BROKER_URL must carry ${REDIS_PASSWORD} — "
            "redis requires AUTH; a passwordless URL re-introduces the "
            "/docs/new 500 outage"
        )
    if re.search(r"redis://[^@]*redis:6379/0\s*$", value) and "REDIS_PASSWORD" not in value:
        room.fail("B2: broker URL is the passwordless image default")
    else:
        room.ok("B2: broker URL is not the passwordless default")

    return 0 if room.summary() else 1


def main() -> int:
    return check(Result("notes-celery-broker"))


if __name__ == "__main__":
    sys.exit(main())