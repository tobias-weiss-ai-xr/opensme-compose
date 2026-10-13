# Proposal: SME Function Catalog

## Why

The portal landing page grew tile-by-tile and no longer reflects what an SME
actually needs: infrastructure surfaces (auth, Collabora-as-embedded-editor)
show up as tiles while real business functions (invoicing, document archive)
are missing entirely. There is no shared model of *which SME functions the
suite exposes*, so tiles, env wiring, contracts, and tests drift apart
(seen 2026-10-12: live portal showed 3 of 5+ deployed functions; Invoice
Ninja has no tile at all).

## What Changes

- Introduce an **SME function catalog**: a defined set of end-user business
  functions (files, mail, workflow, invoicing, archive/DMS, support,
  website, shop, notes, chat, AI) mapped to suite components, with explicit
  **non-catalog** classification for infrastructure (identity/auth, mail
  backend, embedded editors, databases, routing).
- **BREAKING** (portal UX): remove the *Identity* and *Collabora* tiles from
  the portal function grid. Auth is transparent SSO plumbing; Collabora is
  reached from within OpenCloud documents.
- Add missing function tiles: *Invoice* (`INVOICE_URL`, Invoice Ninja) and
  *Archive* (`PAPERLESS_URL`, Paperless-ngx), plus *Chat* (`CHAT_URL`,
  Element/Synapse) and *Notes* (`NOTES_URL`, Impress) so every catalog
  function has a tile path.
- Define the saga → epic → user-story hierarchy for the catalog (see
  design.md) and derive requirements, contracts, and tests from it.
- New Layer-2 **tiles contract** (`contracts/tiles.yml`): every wired tile
  URL must correspond to a live compose service with Traefik routing and a
  healthcheck — "no tile to nowhere".
- New Layer-0 static check (`tests/00-static/check_tiles.py`) enforcing the
  catalog: completeness (deployed user-facing services are tiled or
  explicitly excluded), consistency (env ↔ service ↔ router), and
  exclusion rules (identity/collabora never tiled).
- New **interoperability capability** `sme-interoperability`: the
  cross-service contracts behind the sagas — SSO (OIDC, swappable IdM),
  mail (internal SMTP submission only), file shares (link-shape only),
  news feeds (network-internal fetch, graceful degradation), processes
  (REST-only), document flow (quote-to-cash via public service APIs) —
  plus a machine-readable `contracts/interop.yml` and a Layer-2
  `check_interop.py` that catches wiring drift before deploy.
- Document the sagas as parametrizable E2E journey blueprints
  (`tests/05-e2e/` conventions), skippable per deployment.

## Capabilities

### New Capabilities
- `portal-catalog`: The portal as the SME function catalog — which functions
  exist, how they become visible (tiles/cards), the gating contract
  (env URL → service → router → healthcheck), and the exclusion list for
  infrastructure surfaces.
- `sme-interoperability`: Integration contracts between catalog services
  (SSO, mail, shares, feeds, processes, document flow) with a
  machine-readable interop contract and static drift validation.

### Modified Capabilities
<!-- none: existing specs (bpm-engine, dev-agent*, perf-*) are unaffected.
     Component behavior does not change — only the portal's function
     surface and its validation. -->

## Impact

- Portal code: `get_services()` tile set changes (2 removed, 4 added);
  `AppConfig` gains `invoice_url`, `paperless_url`, `chat_url`, `notes_url`.
- Compose: portal service env gains the four new `*_URL` pass-throughs.
- `.env.example` / `.env.demo`: new documented (empty-default) vars.
- `contracts/` + `tests/00-static/`: new contracts (tiles, interop) +
  checks (`check_tiles.py`, `check_interop.py`), wired into
  `tests/run.py --static` and CI.
- Docs: README function table + `docs/ARC42.md` portal section reference the
  catalog.
- Deployment note: tiles render only for deployed components — live
  (openedusuite.org) gains Invoice/Archive/Chat/Notes tiles only when those
  services are added to the host's compose chain (follow-up deployment
  decision, out of scope here).
