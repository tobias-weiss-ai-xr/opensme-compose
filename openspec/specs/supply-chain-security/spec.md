# supply-chain-security Specification

## Purpose

Provides reproducible supply-chain evidence for the Docker Compose fleet: what images are deployed, what is inside them, which vulnerabilities are present, and which exceptions are justified by VEX.

## Requirements

### Requirement: Canonical deployed-image inventory
The system SHALL produce a canonical inventory of every image referenced by the active Compose file chain and every running Compose container. Each entry MUST identify the service, image reference, digest where available, and source (upstream or locally built). The inventory MUST be usable to reject deployed images that are not in the canonical set.

#### Scenario: Live deployment matches inventory
- **WHEN** the inventory is generated from the active Compose chain and the running fleet
- **THEN** every deployed image appears in the canonical inventory and the deployed-subset gate passes

#### Scenario: Unknown deployed image
- **WHEN** a running container uses an image absent from the canonical inventory
- **THEN** the gate fails and reports the unknown service/image

### Requirement: SBOM generation for every deployed image
The system SHALL generate a CycloneDX SBOM and an SPDX SBOM for every canonical image, plus an aggregate fleet SBOM. SBOM output MUST include package components, licenses, and the image digest/reference, and MUST be valid JSON or SPDX document syntax.

#### Scenario: Per-image SBOM is produced
- **WHEN** the SBOM job runs over the canonical inventory
- **THEN** each image has a CycloneDX and SPDX artifact whose component list is non-empty and whose image digest matches the inventory

#### Scenario: Aggregate SBOM includes all images
- **WHEN** the aggregate SBOM is validated
- **THEN** it contains every canonical image and no duplicate image digest

### Requirement: Vulnerability scan with VEX-backed gate
The system SHALL scan the generated SBOMs or images for vulnerabilities and emit OpenVEX statements. The gate MUST fail on CRITICAL or HIGH findings unless a valid VEX statement marks the finding as not affected with a documented justification. The report MUST include severity counts and scan metadata.

#### Scenario: Critical finding without VEX
- **WHEN** the scan finds a CRITICAL vulnerability and no VEX statement excuses it
- **THEN** the CI gate fails and lists the affected image and finding

#### Scenario: Justified exception
- **WHEN** a HIGH finding is marked `not_affected` in a valid VEX statement
- **THEN** the gate passes and records the VEX justification in the report

### Requirement: VEX report freshness and delta gate
The system SHALL maintain a machine-readable VEX report and a history of prior reports. A committed report MUST be regenerated when the inventory changes, and MUST fail the gate when it is stale or when a previously justified exception disappears without replacement evidence.

#### Scenario: Inventory change requires refresh
- **WHEN** an image digest or service image changes
- **THEN** the existing VEX report is considered stale and CI requires regeneration

#### Scenario: Stale VEX report
- **WHEN** the report is older than the configured freshness limit
- **THEN** the gate fails before deployment artifacts are published

### Requirement: Image reference pinning
Deploy-time Compose image references MUST use explicit version tags or digest pins. Bare `:latest` MUST be rejected unless the image is listed in a documented allowlist with a remediation note. Mutable rolling lines MUST be documented separately.

#### Scenario: Bare latest rejected
- **WHEN** a Compose service uses an image ending in `:latest`
- **THEN** the pinning gate fails and names the service and image

#### Scenario: Allowlisted mutable image
- **WHEN** an image is explicitly allowlisted as intentionally rolling
- **THEN** the gate passes and the allowlist documents why the mutable tag is accepted

### Requirement: CI workflow for SBOM and VEX
The system SHALL run an SBOM/VEX workflow on changes to Compose files, Dockerfiles, nix image definitions, SBOM/VEX scripts, and on a schedule. The workflow MUST generate inventory, SBOMs, scan results, and VEX artifacts, and MUST fail on any gate violation.

#### Scenario: Compose image change triggers workflow
- **WHEN** a Compose or Dockerfile image reference changes on the main branch
- **THEN** the workflow runs and evaluates the new image set

#### Scenario: Scheduled refresh
- **WHEN** the schedule fires without a code change
- **THEN** the workflow regenerates SBOM/VEX evidence and updates freshness metadata

### Requirement: Public artifact hygiene
Generated and committed SBOM/VEX artifacts SHALL NOT contain secret values, internal hostnames, RFC1918 addresses, or private registry credentials. Any such finding MUST fail the hygiene gate.

#### Scenario: SBOM contains an internal hostname
- **WHEN** an SBOM or VEX report contains an internal hostname, private IP, or credential-like string
- **THEN** the hygiene gate fails and identifies the artifact

#### Scenario: Clean public artifact
- **WHEN** SBOM/VEX artifacts are scanned for leaks
- **THEN** no secret or internal-reference pattern matches and the gate passes

### Requirement: Static contract coverage
Layer 0 static checks SHALL verify that the SBOM/VEX workflow contains the inventory, pinning, scan, VEX, freshness, and hygiene gates, and that generated artifacts are valid JSON. The checks MUST fail if a required gate is removed.

#### Scenario: Missing gate
- **WHEN** a required SBOM/VEX gate is deleted from the workflow or scripts
- **THEN** the static suite fails

#### Scenario: Invalid artifact
- **WHEN** an SBOM or VEX report is malformed
- **THEN** the static suite fails
