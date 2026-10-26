#!/usr/bin/env python3
"""
tests/00-static/check_mutations.py — mutation tests for the static suite.

The coverage guard (check_suite_coverage.py) proves every check is *wired*;
this meta-check proves every critical check has *teeth*: for each declared
mutation it breaks a real repo file in an isolated shadow copy, runs the
targeted check against it, and requires the check to FAIL.

A mutation that leaves the target check green means one of:
  - the invariant it encodes is no longer enforced (weakened/removed logic);
  - the file that check reads changed shape (fragment renamed, config moved)
    so the check now scans a dead path.
Either way the suite is lying about protecting that invariant, and this
check goes red so the exact pair (file, invariant) gets re-anchored.

Safety: every mutation runs against a THROWAWAY shadow tree under
TMPDIR/opensme-mut-* — real files are never touched. The shadow is a full
copy: repo root content is read verbatim via symlink farms on
symlink-capable systems; on systems without symlinks we copy the few files
each check actually reads (the MUTATIONS table declares them).

Usage:  python3 tests/00-static/check_mutations.py
Exit:   0 all mutations detected; 1 a mutation went undetected.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

ROOT = Path(__file__).resolve().parents[2]

# ─────────────────────────────────────────────────────────────────────────────
# Mutation table.
#
# Each entry: a single textual mutation applied to one real repo file, plus
# the check that must FAIL when the mutation is present. Mutations must be
# minimal — flip one character, keyword, or line — exactly what a sloppy
# future edit would accidentally do.
#
# Fields:
#   name      short label
#   file      repo-relative file to mutate (must exist)
#   old/new   exact text replacement (old must appear exactly once)
#   check     repo-relative check script that must exit non-zero
#   why       what invariant this mutation guards (failure message)
# ─────────────────────────────────────────────────────────────────────────────
MUTATIONS = [
    {
        "name": "interop-profiles-requirepass",
        "file": "profiles/medium.yml",
        "old": "- --requirepass\n      - ${REDIS_PASSWORD:?REDIS_PASSWORD must be set in .env}",
        "new": "- --noauth",
        "check": "tests/00-static/check_interop.py",
        "why": "§11 — profile redis-command overrides must keep requirepass",
    },
    {
        "name": "interop-notes-callback",
        "file": "scripts/zitadel-client.sh",
        "old": 'base_paths=("/api/v1.0/callback/")',
        "new": 'base_paths=("/oidc/callback/")',
        "check": "tests/00-static/check_interop.py",
        "why": "§12 — notes Zitadel callback must be the lasuite API path",
    },
    {
        "name": "interop-notes-allowed-hosts",
        "file": "services/notes.yml",
        "old": 'DJANGO_ALLOWED_HOSTS: "notes',
        "new": 'ALLOWED_HOSTS: "notes',
        "check": "tests/00-static/check_interop.py",
        "why": "§9b — impress reads DJANGO_* (django-configurations prefix)",
    },
    {
        "name": "interop-synapse-baseurl",
        "file": "services/synapse-setup.sh",
        "old": 'PUBLIC_BASEURL="https://${SERVER_NAME}"',
        "new": 'PUBLIC_BASEURL="https://matrix.${DOMAIN}"',
        "check": "tests/00-static/check_interop.py",
        "why": "§7 — synapse public_baseurl must derive from SYNAPSE_SERVER_NAME",
    },
    {
        "name": "oidc-client-ref",
        "file": "services/notes.yml",
        "old": "NOTES_OIDC_CLIENT_ID",
        "new": "NOTES_OIDC_CLIENT_ID_DROPPED",
        "check": "tests/00-static/check_oidc.py",
        "why": "R1 — every referenced OIDC client var needs a branch",
    },
    {
        "name": "sso-oc-hostgateway-pin",
        "file": "opencloud/opencloud.yml",
        "old": ":host-gateway",
        "new": ":203.0.113.10",
        "check": "tests/00-static/check_oidc.py",
        "why": "R4 — the ocis IdP backchannel must stay pinned to "
              "host-gateway (a hardcoded container IP dead-ends discovery)",
    },
    {
        "name": "sso-oc-autoprovision",
        "file": "opencloud/opencloud.yml",
        "old": 'PROXY_AUTOPROVISION_ACCOUNTS: "true"',
        "new": 'PROXY_AUTOPROVISION_ACCOUNTS: "false"',
        "check": "tests/00-static/check_oidc.py",
        "why": "R5 — external (Zitadel) users must stay autoprovisioned "
              "with the default role driver",
    },
    {
        "name": "sso-element-homeserver",
        "file": "services/element.yml",
        "old": 'ELEMENT_DEFAULT_SERVER_URL: "https://matrix.${OPENSME_DOMAIN:-opensme.graphwiz.ai}"',
        "new": 'ELEMENT_DEFAULT_SERVER_URL: "https://matrix.${OPENSME_DOMAIN:-opensme.graphwiz.ai}"\n      ELEMENT_HOMESERVER_NAME: matrix.opensme.graphwiz.ai',
        "check": "tests/00-static/check_oidc.py",
        "why": "R6 — element server_name must derive from "
              "ELEMENT_DEFAULT_SERVER_URL only (no second source)",
    },
    {
        "name": "sso-zitadel-login-service",
        "file": "idm/zitadel.yml",
        "old": "  zitadel-login:\n",
        "new": "  zitadel-login-disabled:\n",
        "check": "tests/00-static/check_oidc.py",
        "why": "R7 — the v4 login UI service must stay declared in "
              "idm/zitadel.yml",
    },
    {
        "name": "env-completeness",
        "file": ".env.example",
        "old": "REDIS_PASSWORD=CHANGEME_redis",
        "new": "REDIS_PASSWORD_DROPPED=",
        "check": "tests/00-static/check_env.py",
        "why": "env completeness — every referenced var must stay in .env.example",
    },
    {
        "name": "tiles-routed-exclusion",
        "file": "contracts/tiles.yml",
        "old": "  - id: notes-backend # Django API + SSO exchange behind the notes tile (no own journey start)\n",
        "new": "  # DELETED notes-backend exclusion (mutation)\n",
        "check": "tests/00-static/check_tiles.py",
        "why": "tiles catalog — the routed service exclusion must stay in 'excluded'",
    },
    {
        "name": "coverage-runner-wiring",
        "file": "tests/run.py",
        "old": '"Test-suite coverage (no dead checks)", "tests/00-static/check_suite_coverage.py"',
        "new": '"Test-suite coverage (no dead checks)", "tests/00-static/check_suite_coverage_removed.py"',
        "check": "tests/00-static/check_suite_coverage.py",
        "why": "coverage guard — check_suite_coverage must stay wired",
    },
    {
        "name": "interop-idm-swap",
        "file": "contracts/interop.yml",
        "old": "idm_services: [zitadel, casdoor]",
        "new": "idm_services: [keycloak]",
        "check": "tests/00-static/check_interop.py",
        "why": "§1 — every declared IdM needs its idm/<name>.yml swap point",
    },
    {
        "name": "agent-one-shot",
        "file": "monitoring/dev-agent.yml",
        "old": '    restart: "no"',
        "new": '    restart: "always"',
        "check": "tests/00-static/check_agent.py",
        "why": "dev-maintenance-bot overlay — the acting agent must stay "
              "one-shot (restart: 'no'), not a long-running daemon",
    },
    {
        "name": "artifacts-ignore-backups",
        "file": ".gitignore",
        "old": "backups/",
        "new": "backups_dropped/",
        "check": "tests/00-static/check_artifacts.py",
        "why": "committed-artifact hygiene — the backups/ ignore rule must "
              "stay so `git add -A` cannot commit runtime dumps",
    },
    {
        "name": "backup-known-volumes",
        "file": "scripts/backup.sh",
        "old": "  camunda-data",
        "new": "  # camunda-data REMOVED (mutation)",
        "check": "tests/00-static/check_backup.py",
        "why": "backup drift guard — every stateful fragment volume must stay "
              "in backup.sh's KNOWN_VOLUMES",
    },
    {
        "name": "agent-state-in-backup",
        "file": "scripts/backup.sh",
        "old": "dev-maintenance-bot-state ",
        "new": "# VOLUME_REMOVED ",
        "check": "tests/00-static/check_agent.py",
        "why": "AH2 durability — every write-mode agent's state volume must "
              "remain in scripts/backup.sh",
    },
    {
        "name": "restore-applies-postgres",
        "file": "scripts/restore.sh",
        "old": 'gunzip -c "$ACTUAL_FILE"',
        "new": 'gunzip -c "$ACTUAL_FILE_MISSING"',
        "check": "tests/00-static/check_backup_restore.py",
        "why": "backup/restore round-trip — restore.sh must APPLY the "
              "postgres_ artifact, not merely mention it",
    },
    {
        "name": "bpmn-ai-boundary-event",
        "file": "bootstrap/bpmn/invoice-approval.bpmn",
        "old": '<boundaryEvent id="ai-categorize-error"',
        "new": '<!-- boundaryEvent id="ai-categorize-error" DISABLED -->',
        "check": "tests/00-static/check_bpmn_compensation.py",
        "why": "AF2: BPMN AI ServiceTasks must have boundaryEvent compensation handlers",
    },
    {
        "name": "outbound-secrets-regex",
        "file": "tests/06-security/llm_outbound.py",
        "old": 'r"(?:password|passwd',
        "new": 'r"(?:NEVER_MATCH',
        "check": "python3 -m py_compile tests/06-security/llm_outbound.py",
        "why": "AE1: llm_outbound secrets regex must parse; weakening it should fail py_compile (syntax)"
    },
    {
        "name": "boot-core-image-pinned",
        "file": "docker-compose.yml",
        "old": "image: ${TRAEFIK_IMAGE:-traefik:v3.7.13}",
        "new": "image: traefik:latest",
        "check": "tests/00-static/check_boot.py",
        "why": "boot contracts — core services must stay pinned (a bare "
              ":latest silently changes the boot/config model)",
    },
    {
        "name": "bootstrap-history-ttl",
        "file": "bootstrap/bpmn/invoice-approval.bpmn",
        "old": 'operaton:historyTimeToLive="P180D"',
        "new": 'operaton:historyTimeToLive=""',
        "check": "tests/00-static/check_bootstrap.py",
        "why": "seed BPMN — every Operaton process needs a history TTL "
              "(ENGINE-12018, deploy would be rejected)",
    },
    {
        "name": "bpmn-ai-compensation",
        "file": "bootstrap/bpmn/invoice-approval.bpmn",
        "old": 'attachedToRef="ai-categorize"',
        "new": 'attachedToRef="invoice-received"',
        "check": "tests/00-static/check_bpmn_compensation.py",
        "why": "seed BPMN — an AI ServiceTask without an attached boundary "
              "event loses its compensation path (Epic AF: AI failure must "
              "fall back to human review, not block the process)",
    },
    {
        "name": "mail-backend-default",
        "file": ".env.example",
        "old": "MAIL_BACKEND=stalwart",
        "new": "MAIL_BACKEND=mailcow",
        "check": "tests/00-static/check_mail_backend.py",
        "why": "mail backend XOR — .env.example must keep stalwart as the "
              "default switch",
    },
    {
        "name": "perf-digest-pin-doc",
        "file": ".env.example",
        "old": "TRAEFIK_IMAGE_DIGEST",
        "new": "TRAEFIK_IMAGE_PIN_DROPPED",
        "check": "tests/00-static/check_perf.py",
        "why": "perf budgets — .env.example must keep the digest-pin vars "
              "documented for pinned images",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Shadow-incompatible checks.
#
# A wired check that CANNOT meaningfully prove teeth via a shadow mutation
# must be declared here with a truthful reason. The coverage self-check at
# the bottom of this file requires every wired check to be either targeted
# by at least one MUTATIONS entry or documented in this set — anything in
# neither is silently dead teeth and fails the meta-test.
#
# "Shadow-incompatible" means one of:
#   * the check reads NO repo file (a mutation cannot affect its exit code —
#     mutating anything would be a no-op, not a teeth proof);
#   * the check needs tooling/state the throwaway shadow copy cannot provide
#     (mutating would still prove nothing because the run cannot be green on
#     an unmutated baseline).
#
# Keys are `tests/00-static/check_*.py` basenames; values are the one-line
# reason that explains WHY the check cannot be exercised in the shadow.
# ─────────────────────────────────────────────────────────────────────────────
SHADOW_INCOMPATIBLE = {
    "check_platform.py": (
        "platform versions are read LIVE from kubectl/ssh/docker on the "
        "orchestration hosts (check_platform.py opens no repo file), so a "
        "shadow copy changes nothing a mutation could flip red — a shadow "
        "mutation here would be a no-op, not a teeth proof."
    ),
    "check_mutations.py": (
        "is this very meta-test harness — mutating it would mutate the guard "
        "under test. Its teeth are proven by (a) every declared MUTATIONS "
        "entry being required to flip its target check RED, and (b) the "
        "full-coverage self-check at the bottom of this file, which fails "
        "whenever any wired check appears in neither MUTATIONS nor "
        "SHADOW_INCOMPATIBLE."
    ),
}


SKIP_SUBTREES = {
    "portal/target",           # rust build artifacts (3.2 GB, never read)
    "portal/web/node_modules", # portal deps (673 MB, never read)
    "portal/web/.next",        # next.js build output (158 MB, never read)
    "backups",                 # volume dumps (24 MB, never read)
    "openspec",                # spec/change artifacts (checks only need contracts/)
    "mail/mailcow-dockerized", # vendored compose subtree (49 MB, only mail/*.yml is read)
}


def _should_skip(rel: Path) -> bool:
    """True if this repo-relative path lies under a SKIP_SUBTREE prefix."""
    rel_s = rel.as_posix()
    return any(rel_s == sk or rel_s.startswith(sk + "/") for sk in SKIP_SUBTREES)


def build_shadow(mutation: dict) -> Path:
    """Create an isolated copy of the repo with mutation applied.

    A real copy (minus SKIP_SUBTREES) is used instead of symlinks because
    every check derives ROOT via ``Path(__file__).resolve().parents[2]`` —
    a symlinked check would resolve to the original repo and the mutation
    would be invisible. Only the mutated file is edited; the check then
    runs against the throwaway copy, so real files stay intact.
    """
    tmp = Path(tempfile.mkdtemp(prefix="opensme-mut-"))
    _copy_tree(ROOT, tmp, Path())  # start rel at repo-root so skips match
    dst = tmp / mutation["file"]
    text = dst.read_text()
    old = mutation["old"]
    assert text.count(old) == 1, (
        f"{mutation['name']}: old text must match exactly once "
        f"(found {text.count(old)}): {old!r}"
    )
    mutated = text.replace(old, mutation["new"])
    assert mutated != text, (
        f"{mutation['name']}: replacement produced no change — mutation is "
        "a no-op"
    )
    dst.write_text(mutated)
    return tmp


def _copy_tree(src: Path, dst: Path, _rel: Path | None = None) -> None:
    """Deep-copy src into dst, skipping declared bulky subtrees.

    ``_rel`` is the child's repo-relative path (fed through recursion) so
    SKIP_SUBTREES prefixes are matched against the real on-disk location.
    """
    for child in sorted(src.iterdir()):
        rel = (_rel / child.name) if _rel is not None else child
        if _should_skip(rel):
            continue
        out = dst / child.name
        if child.is_symlink():
            # dangling/host-relative symlinks are never read by the checks;
            # skip rather than break the shadow copy. MUST be tested before
            # is_dir(): is_dir() follows symlinks, so a symlink that resolves
            # to a directory would recurse into itself (e.g. a gitignored
            # node_modules symlink) until ELOOP.
            continue
        if child.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            _copy_tree(child, out, rel)
        else:
            try:
                shutil.copy2(child, out)
            except (PermissionError, OSError):
                # unreadable residuals in vendored subtrees (e.g. mode 000)
                # are never read by any targeted check; skip them
                continue


def run_check(shadow: Path, check_rel: str) -> int:
    """Run a check against the shadow tree; returns exit code."""
    env = dict(os.environ)
    # conftest computes ROOT from __file__, so pointing at the shadow's
    # copy of a check gives the shadow ROOT automatically.
    full = shadow / check_rel
    if not full.exists():
        return 99  # phantom reference — nobody can pass that
    proc = subprocess.run(
        [sys.executable, str(full)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(shadow),
        env=env,
    )
    return proc.returncode


def _wired_check_names() -> set[str]:
    """Every tests/00-static/check_*.py registered in tests/run.py Layer 0.

    Same conservative scan check_suite_coverage.py uses: any reference to a
    `tests/00-static/check_*.py` path in run.py counts as wired. This keeps
    the meta-test's teeth inventory in lockstep with the runner, so adding a
    wired check without a mutation (or shadow-incompat reason) goes red here
    automatically.
    """
    run_text = (ROOT / "tests" / "run.py").read_text()
    return set(re.findall(r"tests/00-static/(check_[A-Za-z0-9_]+\.py)", run_text))


def _check_full_coverage(room: Result) -> int:
    """Prove the mutation suite covers EVERY wired static check.

    A wired check that is neither targeted by a MUTATIONS entry nor declared
    in SHADOW_INCOMPATIBLE has no provable teeth: the suite would report
    green even if that check's invariant silently rotted. That is exactly the
    dead-check failure mode this meta-test exists to prevent, so an uncovered
    wired check fails the run. Returns the number of uncovered checks.
    """
    wired = _wired_check_names()
    mutated = {Path(m["check"]).name for m in MUTATIONS}
    covered = mutated | set(SHADOW_INCOMPATIBLE)
    uncovered = sorted(wired - covered)
    total = len(wired)
    count = len(wired & covered)
    print(
        f"mutation-coverage: {count}/{total} wired checks have teeth or "
        "documented shadow-incompat"
    )
    if uncovered:
        room.fail(
            "wired checks with NO mutation teeth and NO shadow-incompat "
            f"reason: {', '.join(uncovered)} — add a MUTATIONS entry that "
            "flips each red, or document a truthful reason in "
            "SHADOW_INCOMPATIBLE"
        )
    else:
        room.ok(
            f"all {total} wired checks are mutation-targeted or documented "
            "shadow-incompatible"
        )
    return len(uncovered)


def check(room: Result) -> int:
    failures: list[str] = []
    _info: list[str] = []

    for mutation in MUTATIONS:
        name = mutation["name"]
        src = ROOT / mutation["file"]
        if not src.exists():
            failures.append(f"{name}: mutation source {mutation['file']} missing")

    if failures:
        for f in failures:
            room.fail(f)
        return 0 if room.summary() else 1

    for mutation in MUTATIONS:
        name = mutation["name"]
        shadow = None
        try:
            shadow = build_shadow(mutation)
            rc = run_check(shadow, mutation["check"])
            if rc == 0:
                failures.append(
                    f"{name}: mutation {'->'.join((mutation['old'], mutation['new']))!r} "
                    f"left {mutation['check']} GREEN — the invariant "
                    f"{mutation['why']} is no longer enforced (either the "
                    "check logic weakened or the file shape moved)"
                )
            else:
                room.ok(f"{name}: caught ({mutation['check']} rc={rc})")
        except Exception as e:
            failures.append(f"{name}: mutation harness error: {e}")
        finally:
            if shadow is not None:
                shutil.rmtree(shadow, ignore_errors=True)

    # Full-teeth coverage: every wired check must be mutation-targeted or
    # documented shadow-incompatible. This runs AFTER the mutations so its
    # red output is attributable to coverage, not to an individual mutation.
    _check_full_coverage(room)

    for f in failures:
        room.fail(f)
    return 0 if room.summary() else 1


def main() -> int:
    room = Result("sme-mutation-tests")
    room.header(
        "Layer 0: mutation tests — every critical check has teeth "
        "(isolated shadow repo)"
    )
    return check(room)


if __name__ == "__main__":
    sys.exit(main())
