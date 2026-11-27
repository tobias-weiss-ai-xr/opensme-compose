# Engine-REST public-block guard — design

**Date:** 2026-11-27

## Context

Operaton protects only its webapps via OAuth2; `/engine-rest` remains
unauthenticated. The public path is already blocked twice: by a fleet-edge
file-provider router (`Host(bpm.*) && PathPrefix(/engine-rest)` →
`noop@internal`) and by the compose-internal Traefik label router
`camunda-rest-block` in `services/camunda.yml`. First-party bootstrap and
healthchecks use the compose network / `docker exec` and are unaffected.

The guard makes this security boundary explicit in Layer 0 so a future edit
cannot silently restore public REST access.

## Contract pinned

- `services/camunda.yml` keeps a security note stating `/engine-rest` is
  unauthenticated and must stay blocked.
- Router `traefik.http.routers.camunda-rest-block.rule` contains
  `PathPrefix(\`/engine-rest\`)`, is bound to `websecure`, has
  `priority=10000` and `service=noop@internal`.
- The ordinary `camunda` router stays host-only (no `PathPrefix(\`/engine-rest\`)`).
- `bootstrap/README.md` documents the public block and internal-only access.

## Testing

- New `tests/00-static/check_engine_rest_guard.py`, registered in
  `tests/run.py` and inventoried in `docs/VALIDATION.md`.
- Mutation `camunda-rest-block-service-removed` changes the block router's
  service from `noop@internal` to `camunda`; the guard must fail.
- No runtime or compose change: this is a contract guard only.
