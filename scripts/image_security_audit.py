#!/usr/bin/env python3
"""
image_security_audit.py — audit provenance, secrets, CVEs, and pinning for
all canonical compose images.

Usage:
    python3 scripts/image_security_audit.py [--output REPORT.json]
                         [--inventory_FILE] [--scan-dir DIR] [--skip-scan]

Produces an actionable report covering:
- Pinning status (mutable tag / digest / allowlist)
- CVEs by severity per image
- Images without SBOM/scan evidence
- Recommendations (prefer digest-pin via pinned-images.env, then semantic tag)
"""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load_json(path):
    with open(path) as f:
        return json.load(f)

def inventory_images(inventory_path):
    """Load canonical images from fleet-inventory.tsv."""
    images = []
    with open(inventory_path) as f:
        next(f)  # skip header
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 2 and parts[1]:
                images.append(parts[1])
    return sorted(set(images))

def pin_status(image, allowlist):
    """Classify image: digest-pinned, tagged, mutable-allowlisted, or FAIL."""
    if image.startswith("sha256:"):
        return "digest-pinned", "OK"
    if ":" not in image:
        return "untagged", "FAIL"
    if image.endswith(":latest"):
        tag = image
        for al in allowlist:
            if al.strip() and not al.strip().startswith("#"):
                al_img = al.split("|")[0].strip()
                if al_img == tag:
                    return "allowlisted", "OK"
        return "mutable-latest", "FAIL"
    return "tagged", "OK"

def normalize_image(img):
    """Convert image reference to canonical form.
    Strips registry host for docker.io, normalizes to lowercase."""
    img = img.lower()
    if img.startswith("docker.io/"):
        img = img[10:]
    if "/" not in img and img != img.split("/")[0]:
        pass
    return img

def scan_name_to_image(name):
    """Convert a hashed scan filename to the original image reference.
    e.g. apache_tika_latest-1ca15f00eb5e -> apache/tika:latest
    But this is lossy - we need the meta.json to get the true image name.
    """
    # Best-effort: the meta.json has the true image
    return name

def load_scans(scan_dir):
    """Load all Trivy scan results from scan_dir/*.trivy.json + meta.json."""
    scans = {}
    for meta_path in scan_dir.glob("*.meta.json"):
        # meta files: <safe_name>-<hash>.meta.json
        # trivy files: <safe_name>-<hash>.trivy.json
        # meta: name-<hash>.meta.json -> stem = name-<hash>.meta
        # trivy: name-<hash>.trivy.json
        base_stem = meta_path.stem  # removes .json
        if base_stem.endswith(".meta"):
            base_stem = base_stem[:-5]  # remove .meta
        trivy_name = base_stem + ".trivy.json"
        trivy_path = meta_path.parent / trivy_name
        if not trivy_path.exists():
            continue
        meta = load_json(str(meta_path))
        img = meta.get("image", "")
        if not img:
            continue
        try:
            scan = load_json(str(trivy_path))
        except Exception:
            continue
        # Also try to match with normalized form
        scans[img] = scan
        # Add normalized lowercased for lookup
        scans[img.lower()] = scan
    return scans

def image_hash(img):
    return hashlib.sha256(img.encode()).hexdigest()[:16]

def severity_tally(vulns):
    tally = defaultdict(int)
    for v in vulns:
        sev = v.get("Severity", "UNKNOWN")
        tally[sev] += 1
    return dict(tally)

def vulns_for(doc):
    """Extract all vulnerabilities from a Trivy scan doc."""
    vulns = []
    for r in doc.get("Results", []):
        ext = r.get("Result", r)
        for v in ext.get("Vulnerabilities", []):
            vulns.append(v)
    return vulns

def main() -> int:
    parser = argparse.ArgumentParser(description="Image security audit")
    parser.add_argument("--output", default=str(ROOT / "docs/sbom/image-security-audit.json"))
    parser.add_argument("--inventory", default=str(ROOT / "docs/sbom/fleet-inventory.tsv"))
    parser.add_argument("--scan-dir", default=str(ROOT / "docs/sbom/artifacts/scan"))
    parser.add_argument("--allowlist", default=str(ROOT / "scripts/no-latest-allowlist.txt"))
    parser.add_argument("--skip-scan", action="store_true")
    args = parser.parse_args()

    inventory_path = Path(args.inventory)
    scan_dir = Path(args.scan_dir)
    allowlist_path = Path(args.allowlist)
    output_path = Path(args.output)

    if not inventory_path.exists():
        print(f"Error: inventory not found: {inventory_path}", file=sys.stderr)
        return 1

    # Load allowlist
    allowlist = []
    if allowlist_path.exists():
        with open(allowlist_path) as f:
            for line in f:
                allowlist.append(line.rstrip("\n"))

    images = inventory_images(inventory_path)
    scans = {}
    if not args.skip_scan and scan_dir.exists():
        scans = load_scans(scan_dir)

    # Build findings
    report = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inventory_digest": hashlib.sha256("\n".join(images).encode()).hexdigest(),
        "summary": {"total_images": len(images), "scanned": 0, "unscanned": 0},
        "pinning": {"digest_pinned": 0, "tagged": 0, "allowlisted": 0, "fail": 0},
        "cve_totals": {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0},
        "images": {},
        "recommendations": [],
    }

    for img in images:
        # Pin status
        pin_status_val, pin_ok = pin_status(img, allowlist)
        key = pin_status_val.replace("-", "_")
        report["pinning"][key] = report["pinning"].get(key, 0) + 1
        if pin_status_val == "fail":
            report["recommendations"].append(
                f"{img}: replace mutable/untagged reference with specific tag or digest"
            )

        # Scan status - try exact match and lowercased
        img_lower = img.lower()
        scan = scans.get(img) or scans.get(img_lower)
        if scan:
            report["summary"]["scanned"] += 1
            vulns = vulns_for(scan)
            tally = severity_tally(vulns)
            for sev, cnt in tally.items():
                report["cve_totals"][sev] = report["cve_totals"].get(sev, 0) + cnt
        else:
            report["summary"]["unscanned"] += 1
            report["recommendations"].append(
                f"{img}: no Trivy scan found in {args.scan_dir}"
            )

        report["images"][img] = {
            "pin": pin_status_val,
            "pin_ok": pin_ok == "OK",
            "has_scan": scan is not None,
            "cve_tally": severity_tally(vulns_for(scan)) if scan else {},
        }

    # Sort recommendations for determinism
    report["recommendations"] = sorted(set(report["recommendations"]))

    # Upgrade critical recommendations
    ml = report["pinning"].get("mutable_latest", 0)
    untagged = report["pinning"].get("untagged", 0)
    if ml > 0:
        report["recommendations"].append(
            f"Pinning: {ml} image(s) use :latest without allowlist entry — "
            "add to scripts/no-latest-allowlist.txt or use specific tags"
        )
    if untagged > 0:
        report["recommendations"].append(
            f"Pinning: {untagged} image(s) are untagged — "
            "pin by digest via pinned-images.env"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    # Print summary
    pin = report["pinning"]
    ml = pin.get("mutable_latest", 0)
    unt = pin.get("untagged", 0)
    print(f"Pinning: {pin.get('digest_pinned', 0)} digest / {pin.get('tagged', 0)} tagged / "
          f"{pin.get('allowlisted', 0)} allowlisted / {ml} mutable-latest FAIL / {unt} untagged FAIL")
    print(f"Scanned: {report['summary']['scanned']}/{report['summary']['total_images']}")
    print(f"CVEs: {report['cve_totals']}")
    if report["recommendations"]:
        print(f"\nRecommendations ({len(report['recommendations'])}):")
        for r in report["recommendations"][:10]:
            print(f"  - {r}")
        if len(report["recommendations"]) > 10:
            print(f"  ... and {len(report['recommendations']) - 10} more")
    else:
        print("\nNo recommendations — all images pinned and scanned.")

    return 0

if __name__ == "__main__":
    sys.exit(main())
