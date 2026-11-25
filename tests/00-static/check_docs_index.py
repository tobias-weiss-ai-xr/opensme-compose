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
