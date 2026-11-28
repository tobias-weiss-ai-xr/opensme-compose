# SBOM / VEX supply-chain security for the openSME fleet

## Why

The openSME Compose deployment ships a mix of upstream and locally built images, but there is no reproducible inventory of what is deployed, no machine-readable SBOM, and no VEX-backed vulnerability gate. Without that evidence, image drift, unpinned `latest` tags, and CVE regressions can reach production unnoticed.

## What Changes

- Add a **canonical deployed-image inventory** derived from the Compose file chain and live container state.
- Generate **CycloneDX and SPDX SBOMs** for every deployed image, plus an aggregate SBOM for the fleet.
- Run **Trivy/Grype vulnerability scans** against the SBOM/images and emit **OpenVEX** statements.
- Add a **VEX report and history** with a freshness/delta gate so stale or unjustified exceptions cannot pass.
- Enforce **digest/tag pinning**, rejecting bare `:latest` outside an explicit allowlist.
- Add CI validation for SBOM/VEX JSON, inventory completeness, and secret/internal-reference hygiene.
- Add static contract tests so the security workflow cannot silently regress.

## Capabilities

### New Capabilities

- `supply-chain-security`: SBOM generation, vulnerability scanning, VEX reporting, image pinning, fleet inventory, and CI gates for the Docker Compose deployment.

### Modified Capabilities

None.

## Impact

- `.github/workflows/` gains an SBOM/VEX workflow.
- New scripts for inventory, SBOM generation, scanning, VEX reporting, and pinning checks.
- New static tests under `tests/00-static/`.
- Compose/nix image references must become digest- or tag-pinned, with mutable lines documented.
- CI runtime may increase; scans use public registries and no secrets.
- Reports live in `docs/sbom/` or a dedicated artifact directory and must not contain internal hostnames, IPs, or credentials.
