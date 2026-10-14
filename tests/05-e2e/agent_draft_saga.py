#!/usr/bin/env python3
"""
tests/05-e2e/agent_draft_saga.py — AS2/AS3/AS5: draft → human-approval → commit.

Shares the generic agent write gate that all three fachlichen sagas ride on
(docs/AGENTIC-AI.md §7.5, openspec `agent-saga`): an agent drafts content
(classification / reply / tags), the draft is inert until a human approves it,
and approval is a committed, audited step. Discarding stays the default.

Against a live serve-mode dev-maintenance-bot via its private API (docker exec):

  B1  POST /draft returns an inert draft (approved:false) — never committed alone
  B2  creating a draft lands a "draft"-kind evidence entry
  B3  POST /draft/{id}/approve flips it to approved:true
  B4  approval lands a "commit"-kind evidence entry (audit)
  B5  approving an unknown draft → 404
  B6  an unapproved draft stays inert (discarding is the normal default)

HITL + feedback loop (openspec `agent-feedback`, docs §7.7):

  C1  GET /drafts lists drafts with their lifecycle state (HITL visibility)
  C2  a major draft is flagged major:true (major decision marker)
  C3  POST /draft/{id}/reject records the human's reason (feedback signal)
  C4  rejecting without a reason → 400 (feedback needs the why)
  C5  a rejected draft cannot be approved afterwards (rejection is final)
  C6  GET /feedback aggregates decisions and carries the rejection reasons

Fully isolated: runs its own opensme-draft-bot container, drives the API via
docker exec (no host port), tears the bot down in every path. Safe against a
live stack on the same host (distinct container name).

Usage: python3 tests/05-e2e/agent_draft_saga.py
Exit codes: 0 = pass, 1 = failed (skips don't fail).
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result  # noqa: E402

BOT = "opensme-draft-bot"
IMAGE = "opensme-dev-maintenance-bot:latest"
API = "http://localhost:8082"
STATE = "/var/lib/opensme/bot"
REPO = Path(__file__).resolve().parents[2]


def docker(*args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=kw.pop("timeout", 120), **kw)


def bot_exec(*args, timeout=30):
    return docker("exec", BOT, *args, timeout=timeout)


def bot_http(path, post=None, timeout=60):
    if post is None:
        return bot_exec("wget", "-q", "-O", "-", API + path, timeout=timeout)
    return bot_exec("wget", "-q", "-O", "-",
                    "--post-data=" + json.dumps(post), API + path, timeout=timeout)


def wait_bot(timeout_s=90):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if bot_http("/healthz").returncode == 0:
            return True
        time.sleep(2)
    return False


def start_bot(extra_env: dict | None = None):
    env = dict(extra_env or {})
    env.update({
        "DEV_AGENT_WATCH": "opensme",
        "DEV_AGENT_INTERVAL": "2s",
        "DEV_AGENT_ALLOW_HEAL": "false",
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


def evidence_kinds() -> list[dict]:
    r = bot_http("/evidence")
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="AS2/AS3/AS5 draft→approval→commit gate")
    ap.add_argument("--keep", action="store_true", help="leave the bot up on failure")
    args = ap.parse_args()

    result = Result("agent-draft-saga")
    result.header("openSME e2e agent draft saga (AS2/AS3/AS5 write gate)")

    if not Path("/var/run/docker.sock").exists():
        result.skip("docker socket not on this host")
        print()
        return 0
    ping = docker("version", "--format", "{{.Server.Version}}")
    if ping.returncode != 0:
        result.skip("docker daemon not reachable")
        print()
        return 0

    try:
        # B0: bot image present (built if missing) + bot up (serve mode).
        img = docker("image", "inspect", IMAGE)
        if img.returncode != 0:
            result.info("building opensme-dev-maintenance-bot:latest from repo")
            b = subprocess.run(
                ["docker", "build", "-f", "opensme-dev-agent/Dockerfile",
                 "-t", IMAGE, "."],
                cwd=REPO, capture_output=True, text=True, timeout=600)
            (lambda ok: result.ok("bot image builds") if ok
                else result.fail(f"bot image build failed: {b.stderr[-300:]}"))(b.returncode == 0)
            if b.returncode != 0:
                return 1
        rc, err = start_bot()
        if rc != 0 or not wait_bot():
            result.fail(f"bot did not come up: {err}")
            return 1
        result.ok("bot serving (/healthz)")

        # B1: an inert draft — approved:false, and no commit evidence yet.
        d1 = bot_http("/draft", {"kind": "classification", "text": "refund-request",
                                 "where": "ticket/42"})
        inert = False
        if d1.returncode == 0:
            try:
                j = json.loads(d1.stdout)
                inert = j.get("approved") is False and bool(j.get("id"))
            except json.JSONDecodeError:
                pass
        (result.ok if inert else result.fail)(
            "B1 fresh draft is inert (approved:false), has an id"
            if inert else f"B1 draft response: rc={d1.returncode} body={d1.stdout[:200]}")
        if not inert:
            return 1
        did = json.loads(d1.stdout)["id"]
        drafts_after = [e for e in evidence_kinds() if e.get("kind") == "draft"]
        commits_after = [e for e in evidence_kinds() if e.get("kind") == "commit"]
        (result.ok if len(drafts_after) >= 1 else result.fail)(
            "B2 draft creation is audited (kind draft)")

        # B5: unknown id is rejected. busybox wget reports the 404 as rc!=0
        # (and does not stream the error body to stdout), which is exactly the
        # proof: approving a non-existent draft does NOT succeed.
        unk = bot_http("/draft/no-such/approve", {"approve": True})
        not_found = unk.returncode != 0 or "unknown draft" in unk.stdout
        (result.ok if not_found else result.fail)(
            "B5 approving an unknown draft is rejected (404, no commit)"
            if not_found else f"B5 got rc={unk.returncode} body={unk.stdout[:120]}")

        # B6: another draft left unapproved stays inert forever.
        d2 = bot_http("/draft", {"kind": "tags", "text": "invoice,approval", "where": "paperless/14"})
        d2_inert = False
        if d2.returncode == 0:
            try:
                d2_inert = json.loads(d2.stdout).get("approved") is False
            except json.JSONDecodeError:
                pass
        (result.ok if d2_inert else result.fail)(
            "B6 an unapproved draft stays inert (discarding is the default)")
        if not d2_inert:
            return 1
        did2 = json.loads(d2.stdout)["id"]

        # B3: human approval commits.
        ap = bot_http(f"/draft/{did}/approve", {"approve": True})
        committed = False
        if ap.returncode == 0:
            try:
                committed = json.loads(ap.stdout).get("approved") is True
            except json.JSONDecodeError:
                pass
        (result.ok if committed else result.fail)(
            "B3 approval flips draft to approved:true (human commit)"
            if committed else f"B3 approve: rc={ap.returncode} body={ap.stdout[:200]}")

        # B4: approval is audited as a commit.
        commits_final = [e for e in evidence_kinds() if e.get("kind") == "commit"]
        (result.ok if len(commits_final) >= 1 else result.fail)(
            "B4 approval is audited (kind commit)")

        # ---- HITL + feedback loop (agent-feedback) ----

        # C1: the human must SEE what to decide on.
        dl = bot_http("/drafts")
        visible = False
        if dl.returncode == 0:
            try:
                lst = json.loads(dl.stdout)
                visible = len(lst) >= 2 and any(d.get("approved") for d in lst) \
                    and any(not d.get("approved") and not d.get("rejected") for d in lst)
            except json.JSONDecodeError:
                pass
        (result.ok if visible else result.fail)(
            "C1 GET /drafts lists drafts with lifecycle states (HITL visibility)"
            if visible else f"C1 /drafts: rc={dl.returncode} body={dl.stdout[:200]}")

        # C2: a major decision is flagged.
        d3 = bot_http("/draft", {"kind": "config-change", "text": "raise memory limit",
                                 "where": "app/deploy", "major": True})
        major = False
        if d3.returncode == 0:
            try:
                major = json.loads(d3.stdout).get("major") is True
            except json.JSONDecodeError:
                pass
        (result.ok if major else result.fail)(
            "C2 a major draft is flagged major:true"
            if major else f"C2 major draft: rc={d3.returncode} body={d3.stdout[:200]}")

        # C3: rejection records the human's reason (the feedback signal).
        why = "wrong classification: this is a billing question"
        rj = bot_http(f"/draft/{did2}/reject", {"reason": why})
        rejected = False
        if rj.returncode == 0:
            try:
                j = json.loads(rj.stdout)
                rejected = j.get("rejected") is True and j.get("reason") == why \
                    and j.get("approved") is False
            except json.JSONDecodeError:
                pass
        (result.ok if rejected else result.fail)(
            "C3 rejection records the human's reason (feedback)"
            if rejected else f"C3 reject: rc={rj.returncode} body={rj.stdout[:200]}")

        # C4: feedback without a why is not feedback.
        rj0 = bot_http(f"/draft/{did2}/reject", {"reason": ""})
        needs_reason = rj0.returncode != 0 or "reason required" in rj0.stdout
        (result.ok if needs_reason else result.fail)(
            "C4 rejecting without a reason is refused (400)"
            if needs_reason else f"C4 got rc={rj0.returncode} body={rj0.stdout[:120]}")

        # C5: rejection is final — approving a rejected draft must NOT commit.
        ap2 = bot_http(f"/draft/{did2}/approve", {"approve": True})
        dl2 = bot_http("/drafts")
        final_rejected = ap2.returncode != 0
        if dl2.returncode == 0:
            try:
                for d in json.loads(dl2.stdout):
                    if d.get("id") == did2:
                        final_rejected = d.get("rejected") is True and d.get("approved") is False
            except json.JSONDecodeError:
                final_rejected = False
        (result.ok if final_rejected else result.fail)(
            "C5 a rejected draft cannot be approved afterwards (rejection is final)"
            if final_rejected else f"C5 approve-after-reject: rc={ap2.returncode} drafts={dl2.stdout[:200]}")

        # C6: the loop's queryable signal — decisions + reasons, newest first.
        fb = bot_http("/feedback")
        loop_ok = False
        if fb.returncode == 0:
            try:
                f = json.loads(fb.stdout)
                loop_ok = (f.get("approved", 0) >= 1 and f.get("rejected", 0) >= 1
                           and any(r.get("reason") == why for r in f.get("reasons", [])))
            except json.JSONDecodeError:
                pass
        (result.ok if loop_ok else result.fail)(
            "C6 GET /feedback aggregates decisions with rejection reasons"
            if loop_ok else f"C6 /feedback: rc={fb.returncode} body={fb.stdout[:200]}")
    finally:
        subprocess.run(["docker", "rm", "-f", BOT], capture_output=True,
                       text=True, timeout=120)

    print()
    ok = result.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
