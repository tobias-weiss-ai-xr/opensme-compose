# Docs-as-contract Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce that every top-level doc is indexed and every index link resolves, via a Layer-0 static guard.

**Architecture:** One Python check using the existing `Result`/`ROOT` harness, registered in `tests/run.py`, with a mutation in `check_mutations.py` proving the guard has teeth. Index rows are updated so the gate starts green.

**Tech Stack:** Python 3 (`re`, `pathlib`), pytest-free `Result` harness, Markdown.

---

### Task 1: RED — add the guard and register it

**Files:**
- Create: `tests/00-static/check_docs_index.py`
- Modify: `tests/run.py:60-66`

- [x] **Step 1: Write the check**

```python
#!/usr/bin/env python3
"""
tests/00-static/check_docs_index.py — docs-as-contract guard.

  A. Every top-level docs/*.md is linked from docs/README.md or the README
     `## Documentation` section.
  B. Every .md link in those two places resolves to a real file.

Subdirectories are excluded by design: docs/perf/, docs/audits/ and
docs/superpowers/ keep their own indexing conventions (see the design doc).
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

ROOT = Path(__file__).resolve().parents[2]
LINK_RE = re.compile(r"\]\(([^)]+)\)")


def link_targets(text: str, base: Path) -> dict[str, tuple[Path, str]]:
    """raw markdown link -> (resolved absolute path, repo-relative posix path)."""
    out: dict[str, tuple[Path, str]] = {}
    for raw in LINK_RE.findall(text):
        if raw.startswith(("http://", "https://", "mailto:")):
            continue
        path = raw.split("#", 1)[0]
        if not path.lower().endswith(".md"):
            continue
        resolved = (base / path).resolve()
        out[raw] = (resolved, resolved.relative_to(ROOT).as_posix())
    return out


def main() -> int:
    room = Result("docs-index")
    room.header("Layer 0: docs index contract (docs-as-contract)")
    docs_dir = ROOT / "docs"
    documented = sorted(
        p.name for p in docs_dir.glob("*.md") if p.name != "README.md"
    )

    docs_index = (docs_dir / "README.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    parts = readme.split("## Documentation", 1)
    readme_section = parts[1].split("\n## ", 1)[0] if len(parts) == 2 else ""

    index_links = link_targets(docs_index, docs_dir)
    readme_links = link_targets(readme_section, ROOT)
    linked = {rel for _, rel in index_links.values()} | {
        rel for _, rel in readme_links.values()
    }

    room.info(f"Check 1: every top-level doc is indexed ({len(documented)} documents)")
    for name in documented:
        rel = f"docs/{name}"
        (room.ok if rel in linked else room.fail)(
            f"{rel}: " + ("indexed" if rel in linked else "MISSING from both indexes")
        )

    room.info("Check 2: every index link resolves")
    for label, links in (("docs/README.md", index_links), ("README.md", readme_links)):
        for raw, (resolved, rel) in sorted(links.items()):
            ok = resolved.exists()
            (room.ok if ok else room.fail)(
                f"{label} -> {raw}: " + ("exists" if ok else f"BROKEN ({rel})")
            )

    return 0 if room.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 2: Register it in the Layer-0 runner**

In `tests/run.py`, insert directly after the `Portal cloud-file bridge (contract)` line:

```python
            ("Docs index contract (docs-as-contract)", "tests/00-static/check_docs_index.py"),
```

- [x] **Step 3: Run RED and confirm the 9 known gaps fail**

Run: `python3 tests/00-static/check_docs_index.py`
Expected: exit 1, nine `MISSING from both indexes` failures (OPENCLOUD-CAMPAIGNS-SUMMARY, nix, opencloud-integration-matrix, opencloud-testing, opencloud, operaton-sso, portal-migration, registry-mirror, vc), all link-validity checks OK.

### Task 2: GREEN — complete the indexes

**Files:**
- Modify: `docs/README.md` (Operations / Quality tables)

- [x] **Step 1: Add the missing operation docs**

In `docs/README.md`, under `### Operations` add these rows before the closing `|` table row block:

```markdown
| [opencloud.md](opencloud.md) | OpenCloud deployment: OIDC, DAV, Office integration and the production S3 overlay |
| [opencloud-integration-matrix.md](opencloud-integration-matrix.md) | OpenCloud integration campaign matrix (tasks, results, skipped items) |
| [opencloud-testing.md](opencloud-testing.md) | OpenCloud integration test protocol and observed results |
| [operaton-sso.md](operaton-sso.md) | Operaton BPMN login via Zitadel OIDC |
| [registry-mirror.md](registry-mirror.md) | Local registry mirror / pull-through cache setup |
| [nix.md](nix.md) | Nix-built replacement images, local tags and CI publication to ghcr.io |
| [vc.md](vc.md) | LiveKit video-meeting service, room creation and edge routing |
| [portal-migration.md](portal-migration.md) | Portal migration notes: Next.js frontend ahead of the Rust catalog |
```

- [x] **Step 2: Add the campaign summary under Architecture & design**

```markdown
| [OPENCLOUD-CAMPAIGNS-SUMMARY.md](OPENCLOUD-CAMPAIGNS-SUMMARY.md) | OpenCloud integration campaigns: results, lessons learned and remaining future work |
```

- [x] **Step 3: Run GREEN**

Run: `python3 tests/00-static/check_docs_index.py`
Expected: exit 0, zero failures.

### Task 3: Teeth, documentation, close the backlog item

**Files:**
- Modify: `tests/00-static/check_mutations.py`, `docs/VALIDATION.md`, `docs/ROADMAP.md`

- [x] **Step 1: Add the mutation**

Insert as the first entry in `MUTATIONS` in `tests/00-static/check_mutations.py`:

```python
    {
        "name": "docs-index-sso-row-removed",
        "file": "docs/README.md",
        "old": "[SSO.md](SSO.md)",
        "new": "[SSO.md](SSO_MUT.md)",
        "check": "tests/00-static/check_docs_index.py",
        "why": "docs-as-contract — dropping an index row silently orphans an operator doc",
    },
```

- [x] **Step 2: Inventory line in docs/VALIDATION.md**

Add inside the `CHECK-INVENTORY-BEGIN/END` fence, after the `check_cloud_bridge.py` line:

```markdown
- [x] `check_docs_index.py` — docs-as-contract: every top-level docs/*.md is linked from docs/README.md or the README documentation table, and every link in both places resolves
```

- [x] **Step 3: Close the Roadmap backlog item**

Replace the `Docs-as-contract` bullet in `docs/ROADMAP.md` with:

```markdown
- **Docs-as-contract** — enforced by `tests/00-static/check_docs_index.py` (Layer 0): every top-level doc is indexed and every index link resolves.
```

- [x] **Step 4: Full verification**

Run: `python3 tests/run.py --static`
Expected: 32 passed / 0 failed. `check_suite_coverage.py` and `check_mutations.py` must both be green; the new mutation must be reported as caught.

- [x] **Step 5: Commit**

```bash
git add tests/00-static/check_docs_index.py tests/run.py tests/00-static/check_mutations.py docs/VALIDATION.md docs/README.md docs/ROADMAP.md docs/superpowers/plans/2026-11-25-docs-as-contract.md
git commit -m "test(layer0): enforce docs index coverage and link validity"
```
