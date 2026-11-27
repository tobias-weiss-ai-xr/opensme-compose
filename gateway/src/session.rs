//! Minimal verifier for the portal's stateless session cookie.
//!
//! The portal signs `Session { sub, name, email, exp }` as URL-safe no-pad
//! base64 JSON, appends a dot and the base64 HMAC-SHA256 over the payload.
//! This module deliberately implements only verification: the gateway never
//! mints cookie and has no OIDC client or database access.

use axum::http::{header::COOKIE, HeaderMap};
use base64::Engine;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

/// A session secret shorter than this never enables verification (fail-closed).
pub const SESSION_SECRET_MIN: usize = 32;
/// HttpOnly portal-session cookie; identical to the portal's constant.
pub const PORTAL_SESSION_COOKIE: &str = "OPENSMEPORTALSESSION";

/// A verified portal session (decoded from the signed cookie).
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Session {
    pub sub: String,
    pub name: String,
    pub email: String,
    /// Unix seconds at which the session expires.
    pub exp: i64,
}

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn b64(data: &[u8]) -> String {
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(data)
}

fn unb64(s: &str) -> Option<Vec<u8>> {
    base64::engine::general_purpose::URL_SAFE_NO_PAD
        .decode(s)
        .ok()
}

pub fn secret_from_env() -> Vec<u8> {
    std::env::var("LOGIN_SESSION_COOKIE_SECRET")
        .unwrap_or_default()
        .into_bytes()
}

/// Verify a session cookie value. `None` for a weak secret, malformed value,
/// bad signature, empty subject or expired session — never logs the token.
pub fn verify_session(secret: &[u8], token: &str) -> Option<Session> {
    if secret.len() < SESSION_SECRET_MIN || secret.starts_with(b"CHANGEME_") {
        return None;
    }
    let (payload, sig) = token.split_once('.')?;
    let mut mac = HmacSha256::new_from_slice(secret).ok()?;
    mac.update(payload.as_bytes());
    mac.verify_slice(&unb64(sig)?).ok()?;
    let session: Session = serde_json::from_slice(&unb64(payload)?).ok()?;
    if session.exp < now() || session.sub.is_empty() {
        return None;
    }
    Some(session)
}

/// Extract one cookie value from a Cookie header without third-party parsing.
pub fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let raw = headers.get(COOKIE)?.to_str().ok()?;
    raw.split(';').find_map(|part| {
        let (key, value) = part.trim().split_once('=')?;
        (key == name).then(|| value.trim().trim_matches('"').to_string())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &[u8] = b"0123456789abcdef0123456789abcdef";

    fn sign(payload: &str, secret: &[u8]) -> String {
        let mut mac = HmacSha256::new_from_slice(secret).expect("valid key");
        mac.update(payload.as_bytes());
        format!("{payload}.{}", b64(&mac.finalize().into_bytes()))
    }

    fn token(exp: i64, sub: &str, secret: &[u8]) -> String {
        let session = Session {
            sub: sub.into(),
            name: "Ada".into(),
            email: "ada@example.test".into(),
            exp,
        };
        sign(
            &b64(&serde_json::to_vec(&session).expect("serializes")),
            secret,
        )
    }

    #[test]
    fn accepts_a_valid_cookie() {
        let value = token(now() + 3600, "user-1", SECRET);
        let session = verify_session(SECRET, &value).expect("valid session");
        assert_eq!(session.sub, "user-1");
        assert_eq!(session.email, "ada@example.test");
    }

    #[test]
    fn rejects_tampered_payload() {
        let value = token(now() + 3600, "user-1", SECRET);
        let tampered = format!(
            "{}x.{}",
            value.split_once('.').unwrap().0,
            value.split_once('.').unwrap().1
        );
        assert!(verify_session(SECRET, &tampered).is_none());
    }

    #[test]
    fn rejects_expired_and_empty_subject() {
        assert!(verify_session(SECRET, &token(now() - 1, "user-1", SECRET)).is_none());
        assert!(verify_session(SECRET, &token(now() + 3600, "", SECRET)).is_none());
    }

    #[test]
    fn rejects_weak_or_placeholder_secret() {
        let value = token(now() + 3600, "user-1", SECRET);
        assert!(verify_session(b"short", &value).is_none());
        assert!(verify_session(b"CHANGEME_0123456789abcdef01234567", &value).is_none());
    }

    #[test]
    fn extracts_the_portal_cookie() {
        let mut headers = HeaderMap::new();
        headers.insert(
            COOKIE,
            "other=x; OPENSMEPORTALSESSION=abc.def; third=1"
                .parse()
                .unwrap(),
        );
        assert_eq!(
            cookie_value(&headers, PORTAL_SESSION_COOKIE).as_deref(),
            Some("abc.def")
        );
    }
}
