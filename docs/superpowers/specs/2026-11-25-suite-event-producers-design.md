# Design: suite event producers (realtime-events follow-up slice 1)

## Context

The portal event backbone (`portal/src/events.rs`) already publishes
`task.*` and `intercom.*` events to a Redis Stream, and the browser consumes
them through the protected `/api/events` SSE proxy. The original design
(`docs/superpowers/specs/2026-11-21-portal-realtime-events-design.md`)
lists "OpenCloud/SOGo producers publish file/share/SSO events to the stream"
as follow-up slice 1. Today the cloud bridge and OIDC session handlers are
the only in-repo integration points for those services: OpenCloud is reached
through the portal's WebDAV bridge, and the IdP session is established at the
portal callback. SOGo and OpenCloud have no event-emitting code in this
repository.

## Goals / Non-Goals

**Goals:**
- Publish file/share events after successful portal-mediated cloud mutations.
- Publish SSO session lifecycle events (`session.login`, `session.logout`).
- Pin the producer contract with a Layer-0 guard so a handler cannot stop
  publishing, switch to team-wide visibility, or leak file content silently.

**Non-Goals:**
- No changes in the SOGo, OpenCloud, Zitadel or rclone repositories — their
  own producers remain the external half of the follow-up and are documented
  as such.
- No new UI panels, no change to the SSE transport or frontend dispatcher.
- No new env vars or deployment topology; the existing Redis stream is used.

## Architecture

Four Rust handlers become producers. Each publishes exactly one event after
the underlying operation succeeded, using the existing
`config.event_bus.publish(&Event::new(...))` pattern:

| Handler | Event kind | Payload |
|---|---|---|
| PUT `/api/cloud/files` (`handle_cloud_upload`) | `cloud.uploaded` | `{"path": <relative path>, "name": <name>}` |
| POST `/api/cloud/files/mkdir` (`handle_cloud_mkdir_post`) | `cloud.mkdir` | `{"path": <relative path>}` |
| DELETE `/api/cloud/files/*path` (`handle_cloud_delete_file`) | `cloud.deleted` | `{"path": <relative path>}` |
| POST `/api/cloud/share` (`handle_cloud_share`) | `cloud.shared` | `{"path": <relative path>, "url": <share url>}` |
| `session::handle_callback` | `session.login` | `{}` (identity stays in `actor_sub`) |
| `logout::handle_logout` | `session.logout` | `{}` |

All six use actor `session.sub` (OIDC subject) and tenant
`config.opensme_domain`. Visibility is always
`Visibility::Recipients(vec![actor_sub.clone()])`: file activity and session
lifecycle are private to the actor, never `Visibility::Team`. No file bytes,
credentials, id token or signed cookie value enter an event payload.

## Data flow and error handling

- Cloud handlers: WebDAV operation → success → publish → existing HTTP
  response. A publish failure is ignored with `let _ = … .await;` (identical
  to the task/intercom sites): a Redis outage must not turn a successful file
  operation into an error.
- Login: OIDC callback verifies tokens, mints the signed portal session
  cookie, publishes `session.login`, then redirects.
- Logout: read `OPENSMEPORTALSESSION`, verify it with
  `session::verify_session(&config.session, token)`, publish `session.logout`
  when valid, then build the existing 303 + cookie clears. A missing,
  tampered or expired cookie yields no event; the logout response is
  unchanged. The RP-initiated IdP logout flow itself is untouched.
- Tenant/visibility filtering in `events::filter_visible` is reused without
  change; the browser dispatcher needs no new handler.

## Testing

- New `tests/00-static/check_event_producers.py`, registered in `tests/run.py`:
  each of the six handlers contains a publish call with the correct kind,
  explicit recipient visibility and no team visibility; cloud payloads carry
  no `bytes`/`body` fields; logout verifies the session before publishing;
  the existing SSE guard (`check_events.py`) still pins the transport.
- Mutation entry in `check_mutations.py` for a removed cloud publish (with
  teeth — the guard must fail), inventory line in `docs/VALIDATION.md`.
- Rust unit tests in `main.rs` for an event's visibility/payload shape and in
  `logout.rs` that an invalid session cookie yields no logout event. Remote
  `cargo test` runs on `vhrz2392` (AGENTS.md forbids local portal builds;
  the host was unreachable earlier, so CI remains the compile gate).
- Acceptance: `python3 tests/run.py --static` green (including mutation
  coverage) and `cargo fmt --check` clean.

## Risks / Rollback

- Extra Redis writes on every cloud mutation — bounded by the existing
  stream `maxlen` and fail-soft; no retry loop.
- Logout with a stale cookie publishes no event — acceptable by design, as
  the actor is unknowable without a verified session.
- Rollback is a revert of the producer commit; the stream simply stops
  receiving the new kinds, existing consumers ignore them.
