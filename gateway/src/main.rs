//! Opt-in read-only SSE gateway.
//!
//! It reuses the portal's event module verbatim so framing, cursor handling,
//! visibility filtering and per-viewer limits cannot drift. Publishing stays
//! in the portal; this process only reads the Redis stream for an
//! authenticated viewer.

#[path = "../../portal/src/events.rs"]
mod events;
mod session;

use axum::{
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Json, Response},
    routing::get,
    Router,
};
use serde::Deserialize;
use std::sync::Arc;

#[derive(Clone)]
struct AppState {
    secret: Vec<u8>,
    tenant: String,
    bus: events::EventBus,
}

#[derive(Debug, Deserialize)]
struct EventsQuery {
    since: Option<String>,
}

async fn handle_events(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Query(query): Query<EventsQuery>,
) -> Response {
    if state.bus.is_disabled() {
        return StatusCode::NO_CONTENT.into_response();
    }
    let Some(token) = session::cookie_value(&headers, session::PORTAL_SESSION_COOKIE) else {
        return unauthorized();
    };
    let Some(s) = session::verify_session(&state.secret, &token) else {
        return unauthorized();
    };
    let last = headers
        .get("last-event-id")
        .and_then(|v| v.to_str().ok())
        .map(str::to_string)
        .or(query.since);
    let viewer = events::Viewer {
        sub: s.sub,
        email: s.email,
        name: s.name,
        tenant: state.tenant.clone(),
    };
    state.bus.stream(viewer, last).await
}

fn unauthorized() -> Response {
    (
        StatusCode::UNAUTHORIZED,
        Json(serde_json::json!({"error": "session invalid or expired"})),
    )
        .into_response()
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let cfg = events::StreamConfig::from_env(|key| std::env::var(key).ok());
    let tenant = std::env::var("OPENSME_DOMAIN").unwrap_or_else(|_| "opensme.graphwiz.ai".into());
    let secret = session::secret_from_env();
    let bus = events::EventBus::from_config(cfg);
    let state = Arc::new(AppState {
        secret,
        tenant,
        bus,
    });

    let app = Router::new()
        .route("/api/events", get(handle_events))
        .with_state(state);
    let port = std::env::var("GATEWAY_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8081);
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port))
        .await
        .expect("bind gateway");
    tracing::info!("events gateway listening on :{port}");
    axum::serve(listener, app).await.expect("serve gateway");
}
