## 1. Baseline contracts and gates

- [x] 1.1 Add `tests/00-static/check_sbom.py` with checks for the workflow gates, script existence, JSON validity, and public-artifact hygiene; wire it into `tests/00-static/run.sh`.
- [x] 1.2 Add `scripts/no-latest-allowlist.txt` and a pinning checker that rejects bare `:latest` except documented allowlist entries.
- [x] 1.3 Add `scripts/fleet-inventory.sh` to derive the canonical image set from `docker compose config --images` and validate live `docker ps` images are a subset.

## 2. SBOM generation

- [x] 2.1 Add `scripts/generate-sbom.sh` using a pinned Syft version to produce CycloneDX and SPDX documents per image.
- [x] 2.2 Add aggregate SBOM assembly with digest-based deduplication and deterministic ordering.
- [x] 2.3 Add JSON/SPDX validation that fails on empty component lists or digest mismatches.

## 3. Vulnerability scanning and VEX

- [x] 3.1 Add `scripts/scan-sbom.sh` using a pinned Trivy version to scan SBOMs/images and record severity counts.
- [x] 3.2 Add an OpenVEX report generator plus a triage file; require justification text for every `not_affected` statement.
- [x] 3.3 Add a freshness/delta gate: reject reports older than 24h and require regeneration when the inventory changes.

## 4. CI workflow

- [x] 4.1 Add `.github/workflows/sbom.yml` with triggers for Compose/Dockerfile/nix/script changes, pull requests, release, manual dispatch, and a daily schedule.
- [x] 4.2 Wire the inventory, pin, SBOM, scan, VEX, freshness, and hygiene gates into the workflow; fail on any gate violation.
- [x] 4.3 Upload SBOM/VEX artifacts and commit/latest report evidence with no internal references or secrets.

## 5. Tests, docs, and rollout

- [x] 5.1 Add static tests for missing gates, malformed SBOM/VEX JSON, unpinned images, stale reports, and leaked internal references.
- [x] 5.2 Add `docs/sbom/` documentation covering inventory scope, SBOM/VEX evidence, pinning allowlist, freshness, and rollback.
- [x] 5.3 Run the Layer 0 static suite and the SBOM/VEX workflow in warning-only mode; resolve baseline findings, then flip gates to blocking.
