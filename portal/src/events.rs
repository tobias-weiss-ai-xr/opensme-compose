//! Real-time event backbone for the portal (slice 1 of suite-wide intercom).
//! Events are appended to one Redis Stream; the browser reads them through an
//! SSE endpoint that filters by visibility AFTER authenticating the session.

use serde::Serialize;
use std::time::{SystemTime, UNIX_EPOCH};

pub const DEFAULT_STREAM: &str = "opensme:events";
pub const DEFAULT_GROUP: &str = "portal-sse";
pub const DEFAULT_MAXLEN: usize = 10_000;

#[derive(Clone, Debug, PartialEq)]
pub enum Visibility {
    /// Every authenticated member may see the event (team notes).
    Team,
    /// Only these OIDC `sub` values may see it (task author + assignee).
    Recipients(Vec<String>),
}

impl Visibility {
    pub fn visible_to(&self, sub: &str) -> bool {
        match self {
            Visibility::Team => true,
            Visibility::Recipients(ids) => ids.iter().any(|id| id == sub),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Event {
    /// Redis stream ID (`<ms>-<seq>`) — becomes the SSE `id:`.
    pub id: String,
    pub kind: String,
    pub actor_sub: String,
    pub tenant: String,
    #[serde(skip)]
    pub visibility: Visibility,
    pub payload: serde_json::Value,
    pub created_at: i64,
}

impl Event {
    pub fn new(
        kind: &str,
        actor_sub: &str,
        tenant: &str,
        visibility: Visibility,
        payload: serde_json::Value,
    ) -> Self {
        Self {
            id: String::new(),
            kind: kind.into(),
            actor_sub: actor_sub.into(),
            tenant: tenant.into(),
            visibility,
            payload,
            created_at: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0),
        }
    }
}

/// Serialize an event into an SSE frame. `visibility` is `#[serde(skip)]`, so
/// no recipient metadata reaches the browser.
pub fn sse_frame(event: &Event) -> String {
    format!(
        "event: {}\nid: {}\ndata: {}\n\n",
        event.kind,
        event.id,
        serde_json::to_string(event).unwrap_or_else(|_| "{}".into())
    )
}

/// Server-side authorization filter. Must run BEFORE `sse_frame` for every event.
pub fn filter_visible(events: Vec<Event>, viewer_sub: &str) -> Vec<Event> {
    events
        .into_iter()
        .filter(|e| e.visibility.visible_to(viewer_sub))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn visibility_team_is_visible_to_everyone_and_recipients_only_to_members() {
        assert!(Visibility::Team.visible_to("alice"));
        assert!(Visibility::Team.visible_to("bob"));
        let v = Visibility::Recipients(vec!["alice".into(), "carol".into()]);
        assert!(v.visible_to("alice"));
        assert!(v.visible_to("carol"));
        assert!(!v.visible_to("bob"));
    }

    #[test]
    fn event_serializes_with_redis_id_as_sse_id_field() {
        let e = Event {
            id: "123-0".into(),
            kind: "intercom.created".into(),
            actor_sub: "alice".into(),
            tenant: "opensme.local".into(),
            visibility: Visibility::Team,
            payload: serde_json::json!({ "id": 7, "text": "hello" }),
            created_at: 1_700_000_000,
        };
        let json = serde_json::to_value(&e).unwrap();
        assert_eq!(json["id"], "123-0");
        assert_eq!(json["kind"], "intercom.created");
        assert_eq!(json["payload"]["id"], 7);
        assert!(
            json.get("visibility").is_none(),
            "visibility must not leak to the browser"
        );
    }

    #[test]
    fn sse_frame_contains_event_id_data_and_terminator() {
        let e = Event {
            id: "9-0".into(),
            kind: "task.created".into(),
            actor_sub: "a".into(),
            tenant: "t".into(),
            visibility: Visibility::Team,
            payload: serde_json::json!({ "id": 1 }),
            created_at: 0,
        };
        let frame = sse_frame(&e);
        assert!(frame.starts_with("event: task.created\nid: 9-0\ndata: "));
        assert!(frame.ends_with("\n\n"));
    }

    #[test]
    fn filter_drops_events_not_visible_to_viewer() {
        let team = Event {
            id: "1-0".into(),
            kind: "intercom.created".into(),
            actor_sub: "a".into(),
            tenant: "t".into(),
            visibility: Visibility::Team,
            payload: serde_json::json!({}),
            created_at: 0,
        };
        let private = Event {
            id: "2-0".into(),
            kind: "task.created".into(),
            actor_sub: "a".into(),
            tenant: "t".into(),
            visibility: Visibility::Recipients(vec!["a".into()]),
            payload: serde_json::json!({}),
            created_at: 0,
        };
        let seen = filter_visible(vec![team, private], "bob");
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].id, "1-0");
    }
}
