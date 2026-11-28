# SBOM and VEX

This document is the operator runbook for the supply-chain gate. The
executable artifacts are normative: `scripts/`, `tests/00-static/check_sbom.py`,
`.github/workflows/sbom.yml`, `docs/sbom/fleet-inventory.tsv`, and the JSON
evidence under `docs/sbom/`.

## Scope

The canonical image set comes from the compose tree, not from the caller's
environment. `scripts/fleet-inventory.sh` expands compose `${VAR:-default}`
values and emits:

```text
service<TAB>image<TAB>digest<TAB>source
```

`--live` additionally checks that every running `docker ps` image is in the
canonical inventory. The gate does not claim to cover images that are not
referenced by the compose tree.

## Pinning policy

`scripts/check_no_latest.py` rejects bare `:latest` or untagged image
references unless the exact reference appears in
`scripts/no-latest-allowlist.txt` with a visible reason. Digest references
(`repo@sha256:...`) pass. Every allowlist entry must still exist in the compose
tree; stale exceptions fail the gate.

The allowlist is intentionally small and reviewed. Prefer explicit version
tags or digest pins. `docs/pin-images.md` describes the opt-in digest manifest
workflow for images whose compose variables support overrides.

## SBOM generation

`scripts/generate-sbom.sh` uses the pinned Syft version from the CI workflow to
write CycloneDX and SPDX documents per image, then
`scripts/aggregate_sbom.py` builds `docs/sbom/fleet.cdx.json`. The aggregate is
deterministically ordered and deduplicates by digest (or image reference when
the digest is not yet known). Images that cannot be resolved are recorded as
`openSME:sbom=missing` in baseline mode; blocking mode refuses a partial set.

## Vulnerability scanning and VEX

`scripts/scan-sbom.sh` runs the pinned Trivy version and writes raw scan JSON
plus metadata. `scripts/build_vex.py` deduplicates Trivy findings, emits
`docs/sbom/openvex.json`, and writes an actionable `docs/sbom/vex-report.json`.

Every `not_affected` statement must carry a justification in `sbom-triage.json`.
The gate is blocking when `SBOM_BASELINE=0`: any CRITICAL/HIGH finding without
VEX justification fails. The workflow currently defaults to `SBOM_BASELINE=1`
(warning-only) for the initial baseline; flip it to `0` after the findings are
triaged or fixed.

## Freshness and delta

`scripts/check_sbom_freshness.py` rejects reports older than 24 hours and
reports whose `inventory_digest` no longer matches the current inventory. The
CI workflow regenerates evidence on a daily schedule and on compose/Dockerfile/
nix/script changes. Committed evidence is validated by
`tests/00-static/check_sbom.py`; freshness is enforced in CI with
`SBOM_STRICT_FRESH=1`.

## Artifact hygiene

The static gate scans `docs/sbom/*.json` for RFC1918 addresses, internal
hostnames, credential-like tokens, and user-home paths. Do not commit internal
hostnames, private IPs, API keys, or absolute workstation paths into SBOM/VEX
evidence.

## Rollback

The gate is evidence-only: it does not edit compose files or restart services.
To roll back, restore the previous `docs/sbom/` evidence, keep the previous
`sbom-triage.json`, and re-run the workflow. If a failing gate blocks a deploy,
fix the image or add a justified VEX exception rather than disabling the gate.
