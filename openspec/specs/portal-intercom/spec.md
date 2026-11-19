# portal-intercom Specification

## Purpose

Defines how portal-web components interoperate inside the portal without
importing each other: a typed provider registry (the "bridge"), registered
cross-component actions (share-to-team), and a single shared cloud-attachment
slot — every integration degrades to the unmodified host when no provider is
present.

## Requirements

### Requirement: Typed provider registry (bridge)

All cross-component integration in the portal frontend SHALL pass through one
typed registry (`lib/bridge.ts`). Providers SHALL register named capabilities
(e.g. `share.provider`, `cloud.pick`); host components SHALL declare and
render the corresponding slots. No host component SHALL import a peer
component module directly. The registry SHALL be safe for server-side
rendering (no client-only import effect at module load) and SHALL be fail-
soft: a host with zero registered providers MUST behave exactly as it does
without the registry.

#### Scenario: Registry works without any provider

- **WHEN** a host component is rendered with an empty registry
- **THEN** the component behaves unchanged and renders no provider-driven
  affordances

#### Scenario: A registered provider lights up its slot

- **WHEN** a provider registers a capability that a host consumes
- **THEN** the host renders the provider-driven affordance

#### Scenario: Static guard pins the contract

- **WHEN** an integration's registration is removed from the bridge wiring
- **THEN** the static test layer fails, signalling the detached integration

### Requirement: Cross-component share-to-team action

The portal SHALL provide a registered "share to team notes" action slot that
any data source (news-feed article, cloud file/share, calendar booking once
available) can offer. Activating the action SHALL open the team-note composer
prefilled with a human-readable reference (source title/name) and the link,
without a separate navigation; posting SHALL create a note through the portal
note API. When no share provider is registered the host SHALL render no
share affordance and stay fully functional.

#### Scenario: Share a feed article into team notes

- **WHEN** a feed card offers "ins Team teilen" and the user activates it
- **THEN** the team-note composer opens with the article title and link
  prefilled, and the post creates a note containing both

#### Scenario: Share a cloud file into team notes

- **WHEN** a cloud file/share offers "ins Team teilen" and the user
  activates it
- **THEN** the composer opens prefilled and the created note carries the
  cloud share link as its attachment

#### Scenario: No share source available

- **WHEN** no component registers a share provider (e.g. feeds unconfigured)
- **THEN** hosts render no share affordance at all and remain usable

#### Scenario: Share post failure

- **WHEN** the note API rejects the share post (e.g. session expired)
- **THEN** an inline error is shown and the prefilled composer content is
  preserved for retry

### Requirement: Shared cloud-attachment slot

The portal SHALL expose exactly one cloud-file selection slot
(`cloud.pick`) registered through the bridge, usable by every component that
accepts attachments — the team-note composer now AND any further consumer
(including poll composers with attachments) — so attachment behavior is
uniform. Local-file uploads into team notes SHALL remain intentionally
ephemeral (the note store documents them as conversational, not business
data); a note with such an uploaded file SHALL offer an explicit "save to
cloud" action that turns the ephemeral file into a durable cloud share link
and updates the note's attachment to that link. Without the cloud provider
registered, components SHALL omit the picker but keep their existing URL
input fallback.

#### Scenario: One picker in notes and polls

- **WHEN** a user attaches a cloud file to a note and to a poll
- **THEN** both use the same picker slot with identical behavior

#### Scenario: Make an uploaded file durable on demand

- **WHEN** a user activates "save to cloud" on a note's uploaded file
- **THEN** the file is stored as a cloud share and the note's attachment
  becomes that share link, opening in the file service

#### Scenario: Cloud provider absent

- **WHEN** no cloud provider is registered
- **THEN** components render no cloud-pick affordance and the URL-input
  fallback still works
