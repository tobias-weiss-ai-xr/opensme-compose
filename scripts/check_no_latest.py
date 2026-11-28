#!/usr/bin/env python3
"""
check_no_latest.py — pin gate for canonical compose images.

Rejects bare `:latest` (or untagged) image references unless the exact
reference is listed in scripts/no-latest-allowlist.txt with a reason.
Digest references (`repo@sha256:...`) are accepted. Every allowlist entry
must still exist in the compose tree (stale exceptions fail the gate).

Exit 0 = pass, 1 = fail.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from compose_images import collect  # noqa: E402

ALLOWLIST = ROOT / "scripts" / "no-latest-allowlist.txt"


def load_allowlist() -> dict[str, str]:
    entries: dict[str, str] = {}
    for lineno, raw in enumerate(ALLOWLIST.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            raise ValueError(f"{ALLOWLIST}:{lineno}: expected `image | reason`")
        image, reason = line.split("|", 1)
        image = image.strip()
        reason = reason.strip()
        if not image:
            raise ValueError(f"{ALLOWLIST}:{lineno}: empty image reference")
        if len(reason) < 20:
            raise ValueError(f"{ALLOWLIST}:{lineno}: reason too short")
        entries[image] = reason
    return entries


def tag_of(image: str) -> str:
    leaf = image.rsplit("/", 1)[-1]
    return leaf.rsplit(":", 1)[1] if ":" in leaf else ""


def main() -> int:
    try:
        allow = load_allowlist()
    except Exception as exc:
        print(f"pin gate: {exc}", file=sys.stderr)
        return 1

    rows = collect()
    actual = {r["image"] for r in rows}
    failures: list[str] = []

    for row in rows:
        image = row["image"]
        if "@sha256:" in image:
            continue
        tag = tag_of(image)
        if tag == "" or tag == "latest":
            if image not in allow:
                failures.append(
                    f'{row["file"]}: {row["service"]} uses mutable image '
                    f'"{image}" — pin a tag/digest or add an allowlist entry'
                )

    for image in sorted(allow):
        if image not in actual:
            failures.append(
                f'allowlist entry "{image}" no longer appears in the compose tree'
            )

    if failures:
        print("pin gate: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"  ✗ {failure}", file=sys.stderr)
        return 1

    mutable = sum(1 for r in rows if tag_of(r["image"]) == "latest")
    print(
        f"pin gate: PASS — {len(rows)} canonical images; "
        f"{mutable} allowlisted mutable image(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
