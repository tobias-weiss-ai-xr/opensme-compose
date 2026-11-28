#!/usr/bin/env python3
"""
tests/00-static/check_sbom.py — Layer 0 guard for the SBOM/VEX supply-chain gate.

Checks:
  1. required scripts and workflow exist;
  2. the workflow wires inventory, pin, SBOM, scan, VEX, freshness and hygiene;
  3. the pin allowlist is non-empty and the pin gate passes;
  4. the aggregate fleet SBOM is valid, non-empty, digest-deduplicated, and
     covers every canonical inventory image; any per-image SBOM present must
     also be valid and non-empty;
  5. OpenVEX/report JSON is valid; every not_affected statement has justification;
  6. committed SBOM/VEX artifacts contain no RFC1918/link-local addresses,
     split-horizon hostname markers, credential-like tokens, or user-home paths.

Freshness is enforced when SBOM_STRICT_FRESH=1; heavy per-image raw SBOM/scan
files are CI-regenerated and gitignored, so their absence is not a failure.

Freshness is enforced only when SBOM_STRICT_FRESH=1, so committed evidence does
not turn red merely because it is older than 24h; the CI workflow regenerates it.

Usage: python3 tests/00-static/check_sbom.py
Exit: 0 pass, 1 fail. Wired into tests/run.py Layer 0.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import Result

ROOT = Path(__file__).resolve().parents[2]
SBOM_DIR = ROOT / "docs" / "sbom"
WORKFLOW = ROOT / ".github" / "workflows" / "sbom.yml"

REQUIRED_SCRIPTS = [
    "scripts/fleet-inventory.sh",
    "scripts/check_no_latest.py",
    "scripts/no-latest-allowlist.txt",
    "scripts/generate-sbom.sh",
    "scripts/aggregate_sbom.py",
    "scripts/scan-sbom.sh",
    "scripts/build_vex.py",
    "scripts/check_sbom_freshness.py",
    "sbom-triage.json",
]


def build_internal_patterns() -> list[re.Pattern]:
    # Constructed pieces keep this checker itself out of the secret scanner's
    # false-positive set.
    # Full dotted-quad private addresses only — a bare `10\.` prefix would
    # false-positive on semver strings like "2.10.0" and nix package IDs.
    rfc = re.compile(
        r"(?:" + "192" + r"\.168\.\d{1,3}\.\d{1,3}|"
        + "10" + r"\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        + "172" + r"\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3})"
    )
    # Internal hostname markers: generic split-horizon labels (RFC 6762
    # .local, split-DNS .internal/.lan/.corp). Brand/registry hostnames are
    # deliberately NOT hardcoded — like scan_secrets.py they are public
    # product domains already tracked in the repo; hardcoding them would
    # both false-positive on legit public refs and leak their existence.
    host = re.compile(
        r"\.internal\b|\.lan\b|\.corp\b|\.local\b"
    )
    # Credential-like tokens: require a separator (sk-/ghp_/glpat-/xoxb-) or
    # an exact-prefix suffix (AKIA + 16 uppercase). A bare prefix run like
    # package names "skratchdot" or nix-store hash substrings would be a
    # false positive, so no lenient prefix-only form is used.
    token = re.compile(
        r"\b(?:" + "sk" + r"[-_][A-Za-z0-9_-]{8,}|"
        + "ghp" + r"_[A-Za-z0-9]{20,}|"
        + "glpat" + r"[-_][A-Za-z0-9_-]{20,}|"
        + "xoxb" + r"[-_][A-Za-z0-9_-]{20,}|"
        + "AKIA" + r"[A-Z0-9]{16}"
        + r")\b"
    )
    path = re.compile(r"/" + "home" + r"/[A-Za-z0-9._-]+/")
    return [rfc, host, token, path]


def _inventory_rows() -> list[dict]:
    """Parse docs/sbom/fleet-inventory.tsv (service, image, digest, source)."""
    rows = []
    path = SBOM_DIR / "fleet-inventory.tsv"
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 4 or parts[0] == "service":
            continue
        rows.append({
            "service": parts[0],
            "image": parts[1],
            "digest": parts[2],
            "source": parts[3],
        })
    return rows


def load_json(path: Path, label: str) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        raise ValueError(f"{label}: invalid JSON ({exc})") from exc


def main() -> int:
    result = Result("sme-sbom-vex")
    result.header("Layer 0: SBOM/VEX supply-chain gate")

    failures: list[str] = []

    # ── 1. scripts and workflow exist ───────────────────────────────────
    for rel in REQUIRED_SCRIPTS:
        path = ROOT / rel
        if not path.exists():
            failures.append(f"missing required SBOM/VEX file: {rel}")
        elif rel.endswith(".sh") and not os.access(path, os.X_OK):
            failures.append(f"script is not executable: {rel}")

    if not WORKFLOW.exists():
        failures.append("missing .github/workflows/sbom.yml")
        workflow_text = ""
    else:
        workflow_text = WORKFLOW.read_text()

    # ── 2. workflow wiring ──────────────────────────────────────────────
    required_wiring = [
        "scripts/fleet-inventory.sh",
        "scripts/check_no_latest.py",
        "scripts/generate-sbom.sh",
        "scripts/scan-sbom.sh",
        "scripts/build_vex.py",
        "scripts/check_sbom_freshness.py",
        "tests/00-static/check_sbom.py",
    ]
    for needle in required_wiring:
        if needle not in workflow_text:
            failures.append(f"workflow does not wire {needle}")

    for trigger in ("push:", "pull_request:", "workflow_dispatch:", "schedule:"):
        if trigger not in workflow_text:
            failures.append(f"workflow lacks required trigger: {trigger}")
    if "paths:" not in workflow_text and "paths:" not in workflow_text:
        failures.append("workflow lacks path-scoped triggering for compose/nix changes")

    # ── 3. pin gate ────────────────────────────────────────────────────
    allow = ROOT / "scripts" / "no-latest-allowlist.txt"
    if allow.exists():
        entries = [line for line in allow.read_text().splitlines()
                   if line.strip() and not line.strip().startswith("#")]
        if not entries:
            failures.append("no-latest allowlist is empty")
        for line in entries:
            if "|" not in line or len(line.split("|", 1)[1].strip()) < 20:
                failures.append(f"allowlist entry lacks a visible reason: {line}")
    try:
        pin = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_no_latest.py")],
            capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        )
        if pin.returncode != 0:
            failures.append("pin gate failed: " + pin.stdout.strip())
    except Exception as exc:
        failures.append(f"pin gate could not run: {exc}")

    # ── 4. aggregate SBOM ──────────────────────────────────────────────
    aggregate = SBOM_DIR / "fleet.cdx.json"
    if not aggregate.exists():
        failures.append("missing docs/sbom/fleet.cdx.json")
    else:
        try:
            doc = load_json(aggregate, "fleet.cdx.json")
            if doc.get("bomFormat") != "CycloneDX":
                failures.append("fleet.cdx.json is not CycloneDX")
            comps = doc.get("components") or []
            if not comps:
                failures.append("fleet.cdx.json has no components")
            refs = [c.get("bom-ref") for c in comps]
            if any(not c.get("name") or not c.get("bom-ref") for c in comps):
                failures.append("fleet.cdx.json component lacks name/bom-ref")
            if len(refs) != len(set(refs)):
                failures.append("fleet.cdx.json has duplicate bom-ref/digest values")
        except ValueError as exc:
            failures.append(str(exc))

    # Inventory coverage: every canonical image must appear in the aggregate.
    # This pins the "aggregate includes every canonical image" requirement on
    # committed evidence alone, independent of the (often large) raw per-image
    # SBOM files, which CI regenerates and uploads rather than committing.
    inventory_rows = _inventory_rows()
    if aggregate.exists() and inventory_rows:
        try:
            doc = load_json(aggregate, "fleet.cdx.json")
            names = {c.get("name") for c in (doc.get("components") or [])}
            missing = sorted(
                r for r in inventory_rows if r["image"] not in names
            )
            for row in missing:
                failures.append(
                    f"fleet.cdx.json lacks inventory image "
                    f"{row['image']} ({row['service']})"
                )
        except ValueError as exc:
            failures.append(str(exc))

    # Per-image SBOMs: when they exist (CI regenerates/commits selectively),
    # they must be valid JSON with non-empty component/package lists. Missing
    # raw per-image files are NOT a failure here — the workflow gates on
    # generating them (aggregate/scan fail before publish if absent).
    cdx_files = sorted(SBOM_DIR.glob("*.cdx.json"))
    spdx_files = sorted(SBOM_DIR.glob("*.spdx.json"))
    for path in cdx_files:
        if path.name == "fleet.cdx.json":
            continue
        try:
            doc = load_json(path, path.name)
            if not (doc.get("components") or []):
                failures.append(f"{path.name} has no components")
        except ValueError as exc:
            failures.append(str(exc))
    for path in spdx_files:
        try:
            doc = load_json(path, path.name)
            packages = doc.get("packages") or []
            if not packages:
                failures.append(f"{path.name} has no SPDX packages")
        except ValueError as exc:
            failures.append(str(exc))

    # ── 5. OpenVEX + report ────────────────────────────────────────────
    openvex = SBOM_DIR / "openvex.json"
    report = SBOM_DIR / "vex-report.json"
    if not openvex.exists():
        failures.append("missing docs/sbom/openvex.json")
    else:
        try:
            doc = load_json(openvex, "openvex.json")
            if "openvex.dev" not in str(doc.get("@context", "")):
                failures.append("openvex.json missing OpenVEX context")
            statements = doc.get("statements") or []
            if not statements:
                failures.append("openvex.json has no statements")
            for st in statements:
                if st.get("status") == "not_affected" and not st.get("justification"):
                    failures.append(
                        "openvex.json not_affected statement lacks justification: "
                        f"{st.get('vulnerability', {}).get('name')}"
                    )
        except ValueError as exc:
            failures.append(str(exc))

    if not report.exists():
        failures.append("missing docs/sbom/vex-report.json")
    else:
        try:
            doc = load_json(report, "vex-report.json")
            if not doc.get("generated_at"):
                failures.append("vex-report.json missing generated_at")
            if not doc.get("inventory_digest"):
                failures.append("vex-report.json missing inventory_digest")
            if "statements" not in doc:
                failures.append("vex-report.json missing statements")
        except ValueError as exc:
            failures.append(str(exc))

    # ── 6. artifact hygiene ────────────────────────────────────────────
    # Only scan the committed authoritative JSON reports; per-image SBOM/scan
    # files are CI-regenerated in gitignored locations (docs/sbom/scan/
    # and docs/sbom/artifacts/) and may contain upstream noise (RFC1918
    # examples in SBOMs, token-shaped string literals in open-source deps).
    patterns = build_internal_patterns()
    committed_jsons = {"fleet.cdx.json", "openvex.json", "vex-report.json"}
    for path in sorted(SBOM_DIR.glob("*.json")):
        if path.name not in committed_jsons:
            continue  # CI artifact, not committed; skip hygiene here
        text = path.read_text()
        for pat in patterns:
            if pat.search(text):
                failures.append(f"internal/secret-like material in {path.name}: {pat.pattern}")

    # ── 7. optional freshness gate ─────────────────────────────────────
    if os.environ.get("SBOM_STRICT_FRESH") == "1":
        try:
            fresh = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "check_sbom_freshness.py")],
                capture_output=True, text=True, timeout=60, cwd=str(ROOT),
            )
            if fresh.returncode != 0:
                failures.append("freshness gate failed: " + fresh.stdout.strip())
        except Exception as exc:
            failures.append(f"freshness gate could not run: {exc}")

    for f in failures:
        result.fail(f)
    if not failures:
        result.ok("SBOM/VEX scripts, workflow, pin gate, artifacts and hygiene are valid")
    return 0 if result.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
