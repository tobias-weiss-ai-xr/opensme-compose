#!/usr/bin/env python3
"""
build_vex.py — build OpenVEX statements and an actionable report from Trivy JSON.

Inputs:
  docs/sbom/scan/*.trivy.json + *.meta.json
  sbom-triage.json

Outputs:
  docs/sbom/openvex.json
  docs/sbom/vex-report.json

Every `not_affected` statement must carry a justification. Any CRITICAL/HIGH
finding without a triage exception is `affected` and fails the gate unless
`--baseline` is used (warning-only migration mode).
"""

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRIAGE = ROOT / "sbom-triage.json"
GATE_SEVERITIES = {"CRITICAL", "HIGH"}
ALLOWED_STATUS = {"affected", "not_affected", "fixed", "under_investigation"}


def load_triage(path: Path) -> list[dict]:
    if not path.exists():
        return []
    doc = json.loads(path.read_text())
    exceptions = doc.get("exceptions", [])
    if not isinstance(exceptions, list):
        raise ValueError("sbom-triage.json: exceptions must be a list")
    for entry in exceptions:
        if not isinstance(entry, dict):
            raise ValueError("sbom-triage.json: exception must be an object")
        if entry.get("status") not in ALLOWED_STATUS:
            raise ValueError(
                f"sbom-triage.json: invalid status {entry.get('status')!r}"
            )
        if entry.get("status") == "not_affected" and not entry.get("justification"):
            raise ValueError(
                f"sbom-triage.json: not_affected requires justification "
                f"for {entry.get('vulnerability')!r}"
            )
    return exceptions


def scan_files(scan_dir: Path) -> list[dict]:
    docs = []
    for meta_path in sorted(scan_dir.glob("*.meta.json")):
        scan_path = meta_path.with_suffix("").with_suffix(".trivy.json")
        if not scan_path.exists():
            continue
        meta = json.loads(meta_path.read_text())
        scan = json.loads(scan_path.read_text())
        docs.append({"meta": meta, "scan": scan})
    return docs


def findings_for(doc: dict) -> list[dict]:
    image = doc["meta"].get("image", "")
    out = []
    for result in doc["scan"].get("Results", []) or []:
        for vuln in result.get("Vulnerabilities", []) or []:
            vid = vuln.get("VulnerabilityID")
            pkg = vuln.get("PkgName")
            installed = vuln.get("InstalledVersion")
            if not vid or not pkg:
                continue
            key = (image, vid, pkg, installed)
            out.append({
                "key": key,
                "image": image,
                "vulnerability": vid,
                "pkg": pkg,
                "installed": installed,
                "fixed": vuln.get("FixedVersion") or "",
                "severity": (vuln.get("Severity") or "UNKNOWN").upper(),
                "title": vuln.get("Title") or "",
            })
    # Trivy repeats the same package across language-specific result blocks.
    dedup = {}
    for f in out:
        dedup[f["key"]] = f
    return list(dedup.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="build OpenVEX/report")
    parser.add_argument("--scan-dir", default=str(ROOT / "docs/sbom/scan"))
    parser.add_argument("--inventory", default=str(ROOT / "docs/sbom/fleet-inventory.tsv"))
    parser.add_argument("--triage", default=str(DEFAULT_TRIAGE))
    parser.add_argument("--openvex", default=str(ROOT / "docs/sbom/openvex.json"))
    parser.add_argument("--report", default=str(ROOT / "docs/sbom/vex-report.json"))
    parser.add_argument("--baseline", action="store_true",
                        help="warn-only: CRITICAL/HIGH without VEX does not fail")
    args = parser.parse_args()

    scan_dir = Path(args.scan_dir)
    inventory = Path(args.inventory)
    inventory_digest = hashlib.sha256(inventory.read_bytes()).hexdigest() if inventory.exists() else "-"
    docs = scan_files(scan_dir)
    if not docs:
        print("build_vex: no scan artifacts found", file=sys.stderr)
        return 1

    try:
        triage = load_triage(Path(args.triage))
    except Exception as exc:
        print(f"build_vex: {exc}", file=sys.stderr)
        return 1

    exceptions = {}
    for entry in triage:
        exceptions[(entry.get("image"), entry.get("vulnerability"))] = entry

    findings = []
    for doc in docs:
        findings.extend(findings_for(doc))

    statements = []
    for f in findings:
        entry = exceptions.get((f["image"], f["vulnerability"]))
        status = entry["status"] if entry else (
            "affected" if f["severity"] in GATE_SEVERITIES else "under_investigation"
        )
        justification = entry.get("justification") if entry else None
        statement = {
            "vulnerability": {
                "name": f["vulnerability"],
                "description": f["title"],
            },
            "products": [{"@id": f["image"]}],
            "status": status,
        }
        if status == "not_affected":
            statement["justification"] = justification
        statements.append(statement)

    # Dedupe statements by (vulnerability, product, status).
    dedup = {}
    for st in statements:
        key = (st["vulnerability"]["name"], st["products"][0]["@id"], st["status"])
        dedup[key] = st
    statements = sorted(dedup.values(), key=lambda s: (
        s["vulnerability"]["name"], s["products"][0]["@id"]
    ))

    affected_gate = [
        f for f in findings
        if f["severity"] in GATE_SEVERITIES
        and exceptions.get((f["image"], f["vulnerability"]), {}).get("status") != "not_affected"
    ]

    now = datetime.now(timezone.utc).isoformat()
    openvex = {
        "@context": "https://openvex.dev/ns/v0.2.0",
        "id": f"urn:uuid:{uuid.uuid4()}",
        "timestamp": now,
        "author": "openSME supply-chain gate",
        "statements": statements,
    }
    Path(args.openvex).write_text(json.dumps(openvex, indent=2) + "\n")

    severity = Counter(f["severity"] for f in findings)
    per_image = {}
    for f in findings:
        per_image.setdefault(f["image"], Counter())[f["severity"]] += 1
    report = {
        "schema": 1,
        "generated_at": now,
        "inventory_digest": inventory_digest,
        "inventory_scan_count": len(docs),
        "finding_count": len(findings),
        "gate_findings": len(affected_gate),
        "severity_counts": dict(sorted(severity.items())),
        "per_image": {
            image: dict(sorted(counts.items()))
            for image, counts in sorted(per_image.items())
        },
        "statements": statements,
        "gate_findings_detail": [
            {
                "image": f["image"],
                "vulnerability": f["vulnerability"],
                "package": f["pkg"],
                "installed": f["installed"],
                "fixed": f["fixed"],
                "severity": f["severity"],
            }
            for f in sorted(affected_gate, key=lambda f: (
                f["severity"], f["vulnerability"], f["image"]
            ))
        ],
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")

    if affected_gate:
        if args.baseline:
            print(
                f"VEX gate: WARN — {len(affected_gate)} CRITICAL/HIGH finding(s) "
                f"without VEX; baseline mode is warning-only",
                file=sys.stderr,
            )
            return 0
        print(
            f"VEX gate: FAIL — {len(affected_gate)} CRITICAL/HIGH finding(s) "
            f"without VEX justification",
            file=sys.stderr,
        )
        return 1

    print(f"VEX gate: PASS — {len(findings)} finding(s), 0 un-triaged CRITICAL/HIGH")
    return 0


if __name__ == "__main__":
    sys.exit(main())
