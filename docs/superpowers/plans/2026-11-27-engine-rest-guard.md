# Engine-REST public-block guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pin the existing public `/engine-rest` block in Layer 0 so it cannot regress.

**Architecture:** One static check reads `services/camunda.yml` and `bootstrap/README.md`; a mutation flips the block router service back to `camunda`. No runtime changes.

**Tech Stack:** Python static guard, pytest-style `Result` helper, mutation harness.

---

### Task 1: Add the Layer-0 guard

**Files:**
- Create: `tests/00-static/check_engine_rest_guard.py`
- Modify: `tests/run.py`, `docs/VALIDATION.md`, `tests/00-static/check_mutations.py`

- [ ] **Step 1: Write `check_engine_rest_guard.py`**

Read `services/camunda.yml` and `bootstrap/README.md`. Use `Result("engine-rest-guard")` and check:

```python
check("UNAUTHENTICATED" in compose, "E1 security note present")
check("PathPrefix(`/engine-rest`)" in block_rule,
      "E2 block router matches /engine-rest")
check('"traefik.http.routers.camunda-rest-block.entrypoints=websecure"' in compose,
      "E3 block router on websecure")
check('"traefik.http.routers.camunda-rest-block.priority=10000"' in compose,
      "E4 block router has priority 10000")
check('"traefik.http.routers.camunda-rest-block.service=noop@internal"' in compose,
      "E5 block router stops at noop@internal")
check("PathPrefix(`/engine-rest`)" not in normal_rule,
      "E6 ordinary camunda router does not expose engine-rest")
check("BLOCKED" in readme, "E7 bootstrap docs state the public block")
return 0 if result.summary() else 1
```

Derive `block_rule` from the line containing
`traefik.http.routers.camunda-rest-block.rule`, and `normal_rule` from the
line containing `traefik.http.routers.camunda.rule`.

- [ ] **Step 2: Wire the guard**

Register in `tests/run.py` after the CalDAV check:

```python
("Operaton engine-rest public block", "tests/00-static/check_engine_rest_guard.py"),
```

Add an inventory line in `docs/VALIDATION.md` after `check_caldav_portal.py`:

```markdown
- [x] `check_engine_rest_guard.py` — Operaton public `/engine-rest` block: security note,
      high-priority `camunda-rest-block` router with `noop@internal`, ordinary router
      host-only, bootstrap docs state internal-only access
```

- [ ] **Step 3: Add mutation teeth**

In `tests/00-static/check_mutations.py`, add after the polls mutation:

```python
{
    "name": "camunda-rest-block-service-removed",
    "file": "services/camunda.yml",
    "old": '"traefik.http.routers.camunda-rest-block.service=noop@internal"',
    "new": '"traefik.http.routers.camunda-rest-block.service=camunda"',
    "check": "tests/00-static/check_engine_rest_guard.py",
    "why": "public engine-rest must stay blocked; restoring the camunda service would expose the unauthenticated API",
},
```

- [ ] **Step 4: Verify and commit**

Run:

```bash
python3 tests/00-static/check_engine_rest_guard.py
python3 tests/run.py --static
python3 tests/00-static/check_mutations.py
```

Expected: guard passes, full static suite passes, mutation suite passes.

Commit:

```bash
git add tests/00-static/check_engine_rest_guard.py tests/run.py docs/VALIDATION.md tests/00-static/check_mutations.py
git commit -m "test(layer0): pin public engine-rest block"
```

### Task 2: Closeout

- [ ] Mark all Task 1 steps `[x]`.
- [ ] Re-run full static + mutation suite.
- [ ] Commit docs closeout and push `main`.
