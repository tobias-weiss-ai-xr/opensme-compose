#!/usr/bin/env python3
"""
tests/00-static/check_tasks.py — Layer 0 validation of the portal-native
team-tasks backend (portal-tasks capability).

Pins statically:

  T1 — tasks.rs store contract: TASKS_DATA_PATH env, MAX_TASKS = 500 cap,
       cap eviction removes DONE tasks only (never open), may_complete
       permission helper, apply_filter, users_from.
  T2 — main.rs wiring: `mod tasks;`, AppConfig carries the task store, and
       all five /api/tasks routes are registered on the PROTECTED router
       (before the require_session route_layer).
  T3 — author is stamped from the session in handle_tasks_create and the
       TaskCreate payload has no client-supplied author field.
  T4 — permission guard: PATCH status changes and DELETE are wrapped in
       may_complete → 403 (forbidden_tasks) for unrelated members.

Pure file inspection — no docker, runs on any fresh checkout.

Usage: python3 tests/00-static/check_tasks.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result, ROOT

PORTAL_SRC = ROOT / "portal" / "src"
TASKS_RS = PORTAL_SRC / "tasks.rs"
MAIN_RS = PORTAL_SRC / "main.rs"

TASK_ROUTES = [
    '.route("/api/tasks", get(handle_tasks_list))',
    '.route("/api/tasks", post(handle_tasks_create))',
    '.route("/api/tasks/users", get(handle_tasks_users))',
    '.route("/api/tasks/:id", patch(handle_tasks_patch))',
    '.route("/api/tasks/:id", delete(handle_tasks_delete))',
]


def main():
    result = Result("portal-tasks")
    result.header("Layer 0: portal-tasks backend validation")
    failures = []

    tasks = TASKS_RS.read_text() if TASKS_RS.exists() else ""
    main = MAIN_RS.read_text() if MAIN_RS.exists() else ""

    # ── T1: store contract ────────────────────────────────────────────
    if not TASKS_RS.exists():
        failures.append("T1: portal/src/tasks.rs is missing — the task store module must exist")
    else:
        if not re.search(r'pub const ENV_DATA_PATH:\s*&str\s*=\s*"TASKS_DATA_PATH"', tasks):
            failures.append("T1: tasks.rs must persist via TASKS_DATA_PATH (ENV_DATA_PATH)")
        if not re.search(r"pub const MAX_TASKS:\s*usize\s*=\s*500", tasks):
            failures.append("T1: the store cap constant MAX_TASKS = 500 must be declared")
        if not re.search(r"\bpub fn may_complete\b", tasks):
            failures.append("T1: may_complete (assignee-or-author permission) must exist")
        if not re.search(r"\bpub fn apply_filter\b", tasks):
            failures.append("T1: apply_filter (all/mine/open/done) must exist")
        if not re.search(r"\bpub fn users_from\b", tasks):
            failures.append("T1: users_from (known team members) must exist")
        # cap eviction: oldest DONE first, never open
        if not re.search(r"position\(\|t\|\s*t\.status\s*==\s*TaskStatus::Done\)", tasks):
            failures.append("T1: cap eviction must target DONE tasks (TaskStatus::Done)")
        if not re.search(r"None\s*=>\s*break", tasks):
            failures.append("T1: eviction must stop when no done task exists — open tasks are never evicted")

    # ── T2: main.rs wiring, routes on the protected router ────────────
    if not re.search(r"\bmod tasks;", main):
        failures.append("T2: main.rs must declare `mod tasks;`")
    if not re.search(r"task_store:\s*Arc<tasks::TaskStore>", main):
        failures.append("T2: AppConfig must carry task_store: Arc<tasks::TaskStore>")
    fw = re.search(r"\bsession::require_session\b", main)
    firewall_pos = fw.start() if fw else -1
    if firewall_pos == -1:
        failures.append("T2: the session firewall (require_session route_layer) is missing from main.rs")
    for route in TASK_ROUTES:
        positions = [m.start() for m in re.finditer(re.escape(route), main)]
        if not positions:
            failures.append(f"T2: route missing from main.rs: {route}")
        elif firewall_pos != -1 and any(p > firewall_pos for p in positions):
            failures.append(f"T2: route registered OUTSIDE the protected router (after require_session): {route}")
    if "tasks_store_from_env" not in main or "ENV_MAX_TASKS" not in main:
        failures.append("T2: tasks_store_from_env must read TASKS_DATA_PATH/TASKS_MAX_TASKS")

    # ── T3: author stamped from session, never client-supplied ────────
    m = re.search(r"fn handle_tasks_create\([\s\S]*?\n\}\n", main)
    if not m:
        failures.append("T3: handle_tasks_create is missing")
    elif not re.search(r"session\.email", m.group(0)):
        failures.append("T3: handle_tasks_create must stamp the author from the session (session.email)")
    create_struct = re.search(r"pub struct TaskCreate\s*\{([\s\S]*?)\n\}", tasks)
    if create_struct and re.search(r"\bpub author\b", create_struct.group(1)):
        failures.append("T3: TaskCreate must NOT accept a client-supplied author field")

    # ── T4: permission guard on status change + delete ────────────────
    if not re.search(r"patch\.status\.is_some\(\)\s*&&\s*!tasks::may_complete\(&session\.email", main):
        failures.append("T4: PATCH with a status change must be guarded by may_complete (403 otherwise)")
    if not re.search(r"\bif\s+!tasks::may_complete\(&session\.email,&?\s*&session\.name", main) and \
       not re.search(r"if\s+!tasks::may_complete\(&session\.email, &session\.name, &task\)", main):
        failures.append("T4: DELETE must be guarded by may_complete (403 otherwise)")
    if "forbidden_tasks" not in main:
        failures.append("T4: the 403 response helper (forbidden_tasks) must exist")

    if failures:
        for f in failures:
            result.fail(f)
    else:
        result.ok(
            "tasks store contract (cap 500, done-only eviction, may_complete), "
            "5 protected /api/tasks routes, session-stamped author, 403 guards"
        )

    return result.summary()


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
