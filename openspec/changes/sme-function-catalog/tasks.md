## 1. Portal code (RED → GREEN)

- [x] 1.1 Remove Identity and Collabora tiles from `get_services()` in `portal/src/main.rs`; drop now-unused icon entries and the `collabora_url` config field plus its env parsing
- [x] 1.2 Add Invoice, Archive (Paperless), Chat, Notes tiles: `invoice_url`, `paperless_url`, `chat_url`, `notes_url` config fields read from `INVOICE_URL`, `PAPERLESS_URL`, `CHAT_URL`, `NOTES_URL` (empty default, tile only when non-empty)
- [x] 1.3 Update portal unit tests: assertions for identity/collabora tiles removed, new-tile rendering tests added; `cargo fmt`, `cargo clippy --all-targets -- -D warnings`, `cargo test` green

## 2. Compose & env plumbing

- [x] 2.1 `docker-compose.yml`: portal env gains `INVOICE_URL`, `PAPERLESS_URL`, `CHAT_URL`, `NOTES_URL` pass-throughs; remove portal's `COLLABORA_URL` pass-through (keep `COLLABORA_DOMAIN` — still used by routing)
- [x] 2.2 `.env.example` + `.env.demo`: document the four new vars (empty default), drop portal-only `COLLABORA_URL` if present; init-env.sh untouched
- [x] 2.3 `scripts/demo-live.sh`: keep `MAIL_URL`/`BPM_URL` wiring, remove `COLLABORA_URL` portal wiring, add commented examples for the new vars

## 3. Contract & static check

- [x] 3.1 Create `contracts/tiles.yml`: catalog entries (files/opencloud, mail/sogo, workflow/camunda, invoice/invoice-ninja, archive/paperless, support/ticketing, website/cms, shop/store, notes/notes, chat/synapse-element) each with `url_env`, `service`, `domain_env`, `healthcheck`; `excluded:` list (identity, stalwart, collabora, infra)
- [x] 3.2 Create `tests/00-static/check_tiles.py` (Layer 2): env↔compose pass-through, set-URL⇒service-with-router-and-healthcheck, chain-completeness (every user-facing service cataloged or excluded), exclusion enforcement; register in `tests/run.py` static layer
- [x] 3.3 `python3 tests/run.py --static` fully green with the new check in place

## 4. Saga E2E blueprints

- [x] 4.1 Add `tests/05-e2e/saga_smoke.py`: parametrizable saga blueprints (quote-to-cash, leave-request, support-ticket) probing public endpoints of participating services, skipping not-deployed ones per `run.py` conventions
- [x] 4.2 Document the six sagas in `docs/E2E-JOURNEYS.md` (persona, steps, participating services, test mapping)

## 5. Interoperability contract & check

- [x] 5.1 Create `contracts/interop.yml`: declared integration surfaces — `oidc` (issuer/client env namespace, swappable IdM list), `smtp_submission` (host/port/sender envs per mail-sending service), `feed_columns` (PORTAL_FEEDS contract, http(s)-only), `share_links` (cloud share URL shape), `processes` (REST-only + bootstrap-deployed BPMN keys), `document_flow` (invoice exports → DMS ingest API paths)
- [x] 5.2 Create `tests/00-static/check_interop.py` (Layer 2): for each consumer in the active chain, validate provider wiring per contract; absent provider ⇒ reported skip; drift (consumer without wiring) ⇒ violation; register in `tests/run.py` static layer
- [x] 5.3 Extend `saga_smoke.py` steps for the document-flow surface: invoice exports endpoint + DMS ingest endpoint availability (skipped when either is undeployed)
- [x] 5.4 Document the interop matrix in `docs/ARC42.md` (integration section) and link from README

## 6. Docs & spec hygiene

- [x] 6.1 README function table + `docs/ARC42.md` portal section reference the catalog and exclusion rule
- [x] 6.2 `openspec validate --all` passes; CI green (Code quality + Static validation)
