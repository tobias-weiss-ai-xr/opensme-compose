#!/usr/bin/env python3
"""
check_sbom_freshness.py — reject SBOM/VEX reports older than 24h or tied to a
stale inventory fingerprint.

Usage:
  scripts/check_sbom_freshness.py [--report FILE] [--inventory FILE]
                                  [--max-hours 24] [--baseline]
Exit 0 pass, 1 fail. With --baseline, stale reports warn but do not fail.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> int:
    parser = argparse.ArgumentParser(description="freshness/delta gate")
    parser.add_argument("--report", default=str(ROOT / "docs/sbom/vex-report.json"))
    parser.add_argument("--inventory", default=str(ROOT / "docs/sbom/fleet-inventory.tsv"))
    parser.add_argument("--max-hours", type=float, default=24)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()

    report_path = Path(args.report)
    inventory_path = Path(args.inventory)
    failures = []

    if not report_path.exists():
        failures.append(f"report not found: {report_path}")
        report = {}
    else:
        try:
            report = json.loads(report_path.read_text())
        except Exception as exc:
            failures.append(f"report is not valid JSON: {exc}")
            report = {}

    if not inventory_path.exists():
        failures.append(f"inventory not found: {inventory_path}")
        current_digest = "-"
    else:
        current_digest = hashlib.sha256(inventory_path.read_bytes()).hexdigest()

    if report:
        if report.get("inventory_digest") != current_digest:
            failures.append(
                "inventory changed since report generation "
                f"(report={report.get('inventory_digest')}, current={current_digest})"
            )
        generated = report.get("generated_at")
        if not generated:
            failures.append("report has no generated_at timestamp")
        else:
            try:
                age = datetime.now(timezone.utc) - parse_iso(generated)
            except Exception as exc:
                failures.append(f"generated_at is invalid: {exc}")
                age = None
            if age and age.total_seconds() > args.max_hours * 3600:
                failures.append(
                    f"report is stale: {age.total_seconds() / 3600:.1f}h old "
                    f"(max {args.max_hours}h)"
                )

    if failures:
        if args.baseline:
            print("freshness gate: WARN (baseline mode)", file=sys.stderr)
            for failure in failures:
                print(f"  ! {failure}", file=sys.stderr)
            return 0
        print("freshness gate: FAIL", file=sys.stderr)
        for failure in failures:
            print(f"  ✗ {failure}", file=sys.stderr)
        return 1

    print("freshness gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
