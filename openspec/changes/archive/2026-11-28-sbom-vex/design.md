# Design — SBOM / VEX for the Compose fleet

## Context

See `proposal.md` — Why. Current deployment has no committed image inventory, no SBOM artifacts, no vulnerability report, and no `:latest` gate. The repo already uses a layered test pyramid and CI secret/leak scans; this change extends those gates rather than replacing them.

## Goals / Non-Goals

**Goals:**
- Produce a deterministic canonical image inventory from the active Compose chain and live containers.
- Generate CycloneDX and SPDX SBOMs for every canonical image and one aggregate SBOM.
- Scan images/SBOMs for vulnerabilities and emit OpenVEX statements.
- Fail CI on unpinned `:latest`, unknown deployed images, unjustified CRITICAL/HIGH findings, stale VEX evidence, and public-artifact leaks.
- Keep the workflow reproducible and safe to run without private credentials.

**Non-Goals:**
- Kubernetes/Helm coverage; this repo is Docker Compose only.
- Runtime container hardening or CVE remediation in upstream images.
- Publishing signed attestations to a registry.
- Full package-level provenance for every transitive dependency beyond what the SBOM tools report.

## Decisions

### Inventory: Compose config + live inspect
Use the compose file chain for the canonical set, then `docker ps`/`docker inspect` to verify the live fleet. Add image digest from `docker image inspect` (or an emitted manifest) when `--resolve-local` is used. This keeps the canonical set tied to the deployed compose files, not a hand-maintained list.
- Implementation note (adopted): `docker compose config --images` requires a fully-interpolated environment — the chain uses `:?`-guarded secret vars (e.g. `PORTAL_OIDC_CLIENT_SECRET`, `LIVEKIT_API_SECRET`) that CI does not have, so it errors without a production `.env`. `scripts/compose_images.py` instead parses the same fragment globs the repo's other drift guards use (`docker-compose.yml`, `idm/*.yml`, `opencloud/*.yml`, `mail/*.yml`, `services/*.yml`, `monitoring/*.yml`, `profiles/*.yml`) and expands `${VAR:-default}` slices with the default, not the caller's environment. This is deterministic, CI-safe, and still authoritative over the compose chain — no hand-maintained list.
- Alternative: a committed TSV only. Rejected because it drifts from the compose chain.
- Gate: live deployed images must be a subset of the canonical set; unknown images fail.

### SBOM: Syft for CycloneDX/SPDX
Use Syft to generate both CycloneDX and SPDX documents from image archives. Generate one per image plus an aggregate SBOM assembled from the per-image documents. Validate JSON/SPDX syntax and non-empty component lists.
- Alternative: Trivy alone. Rejected because SPDX coverage is weaker and mixing scan and SBOM output complicates validation.
- Pin the Syft version in the script.

### Scanning and VEX: Trivy + OpenVEX
Use Trivy for vulnerability findings, mapping CRITICAL/HIGH findings to OpenVEX statements. A finding passes only if a VEX statement marks it `not_affected` with a documented justification. Store scan metadata and severity counts in the report.
- Alternative: Grype. Acceptable, but Trivy already matches the existing image-security work and supports SBOM/image scans.
- VEX generation should be explicit, not automatic: automatic `not_affected` claims would weaken the gate.

### Freshness: daily schedule + 24h limit
Run the workflow on relevant pushes and a daily schedule. Treat a committed report older than 24 hours as stale when the gate runs. Regenerate on any inventory digest change.
- Alternative: no committed report, only CI artifacts. Rejected because public evidence and delta comparison need a stable report.

### Pin gate: explicit tags/digest + allowlist
Reject bare `:latest` in deploy-time Compose references. Allow only documented mutable/rolling images in `scripts/no-latest-allowlist.txt`, with a remediation note. Prefer digest pins where available.
- Alternative: allow all `:latest` because upstream images are rolling. Rejected; that defeats reproducibility.

### Artifact location and hygiene
Write SBOM/VEX artifacts under `docs/sbom/` or a dedicated public artifact directory. Run the existing leak/secret scan over these artifacts. Do not include internal hostnames, RFC1918 IPs, registry credentials, or private tokens.
- Commit policy: the small authoritative reports are tracked — `fleet-inventory.tsv`, the aggregate `fleet.cdx.json`, `openvex.json` and `vex-report.json`. Heavy per-image CycloneDX/SPDX and Trivy scan JSON are gitignored (`docs/sbom/scan/`, `docs/sbom/*.cdx.json`, `*.spdx.json`) and regenerated + uploaded by the CI workflow. `check_sbom.py` enforces aggregate coverage of every inventory image on committed evidence alone, and validates any per-image SBOM that is present.
- Hygiene follows `scan_secrets.py` conventions: RFC1918/link-local addresses and split-horizon hostname markers (`*.internal`, `*.lan`, `*.corp`, `*.local`) are leaks; project brand/registry hostnames are public and tracked, so they are not flagged.
- Alternative: store reports outside the repo. Rejected because CI evidence and delta gates need committed history.

### Static coverage
Add `tests/00-static/check_sbom.py` to verify workflow steps, script existence, JSON validity, inventory coverage, pin gate, VEX freshness, and hygiene. Wire it into `tests/00-static/run.sh`.
- Alternative: rely only on CI workflow execution. Rejected because Layer 0 must catch removals before runtime.

## Risks / Trade-offs

- **Scan cost / image pulls** → Mitigation: cache image archives, run on a host/runner with registry access, and allow a bounded scan set; never require private credentials.
- **SBOM size and reproducibility** → Mitigation: deterministic sorting, digest-based filenames, and aggregate deduplication by digest.
- **False positives / VEX overclaiming** → Mitigation: manual triage for `not_affected`; require justification text and expiry.
- **Upstream mutable images** → Mitigation: allowlist with documented reasons and a recommendation to digest-pin.
- **Stale committed reports** → Mitigation: daily schedule, 24h freshness gate, and regeneration on inventory change.
- **CI failure on first clean baseline** → Mitigation: phase in as warning-only until the baseline passes, then make gates blocking.

## Migration Plan

1. Add inventory/SBOM/scan/VEX scripts and static checks.
2. Add the workflow with a non-blocking baseline mode.
3. Generate and commit the first reports; resolve false positives and pin gaps.
4. Flip gates to blocking.
5. Rollback: remove the workflow gate or set the baseline mode if scans become unreliable; committed reports remain harmless.

## Open Questions

None that change the specs or task breakdown. Exact scanner versions are pinned during implementation.
