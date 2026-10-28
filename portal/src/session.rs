//! Portal sign-in (OIDC authorization-code) + session guard for the
//! authenticated surfaces (intercom, polls).
//!
//! The portal is a public landing page, but its interactive surfaces must
//! only work for signed-in users. The IdP (Zitadel) authenticates the human
//! via a plain OIDC authorization-code flow; once the identity is confirmed
//! (token exchange + `/userinfo`), the portal mints its own STATELESS
//! HMAC-signed session cookie (`OPENSMESESSION`). Checking the cookie later
//! needs no IdP round trip and no JWT/keying material — just one HMAC.
//! The cookie holds no secrets; it is (subject, name, email, expiry) +
//! a keyed tag. Without a rotated `LOGIN_SESSION_COOKIE_SECRET` the surface
//! is FAIL-CLOSED (every protected call answers 401), never open.

use axum::{
    extract::{Query, Request, State},
    http::{
        header::{COOKIE, LOCATION, SET_COOKIE},
        HeaderMap, StatusCode,
    },
    middleware::Next,
    response::{IntoResponse, Json, Response},
};
use base64::Engine;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::AppConfig;

type HmacSha256 = Hmac<Sha256>;

/// A session secret shorter than this never enables the guard (fail-closed).
pub const SESSION_SECRET_MIN: usize = 32;
/// Sessions live 12h; the browser is redirected to re-login afterwards.
const SESSION_TTL: u64 = 12 * 3600;
/// HttpOnly portal-session cookie — the HMAC-signed portal session. Lives
/// SEPARATELY from `OPENSMESESSION` (the IdP id_token container used by the
/// logout end-session hint): portal sessions and IdP identities are different
/// trust domains and must not overwrite each other.
pub const PORTAL_SESSION_COOKIE: &str = "OPENSMEPORTALSESSION";
/// OAuth `state` (CSRF): bound in an HttpOnly cookie for 5 minutes.
const STATE_TTL_SECS: i64 = 300;
const STATE_COOKIE: &str = "OPENSMESTATE";

/// IdP wiring for the portal's own OIDC client + local session HMAC key.
#[derive(Clone)]
pub struct SessionConfig {
    pub idp_url: String,
    /// Separate base for the portal's SERVER-SIDE OIDC calls (token +
    /// userinfo). Demo/CI serve the IdP behind Traefik's self-signed cert,
    /// which reqwest's TLS verification rejects; those stacks map this to
    /// the IdP's plain-HTTP listener on the compose network
    /// (e.g. http://zitadel:8080). Empty = fall back to `idp_url` (prod
    /// keeps verifying the public endpoint).
    pub idp_internal_url: String,
    pub portal_domain: String,
    pub client_id: String,
    pub client_secret: String,
    /// HMAC key for session cookies; must be ≥ 32 bytes and not a CHANGEME
    /// placeholder, otherwise `enabled()` is false and the guard is closed.
    pub session_secret: Vec<u8>,
    pub http: reqwest::Client,
}

impl SessionConfig {
    /// Read the OIDC client + session secret from the environment. A weak or
    /// missing secret keeps `enabled()` false (authenticated surfaces stay
    /// closed) while still allowing the public landing page to serve.
    pub fn from_env() -> Self {
        let idp_url = crate::load_env("IDP_URL", "");
        let idp_internal_url = crate::load_env("IDP_INTERNAL_URL", "");
        let portal_domain = crate::load_env("PORTAL_DOMAIN", "portal.opensme.graphwiz.ai");
        let client_id = crate::load_env("PORTAL_OIDC_CLIENT_ID", "");
        let client_secret = crate::load_env("PORTAL_OIDC_CLIENT_SECRET", "");
        let session_secret = std::env::var("LOGIN_SESSION_COOKIE_SECRET")
            .unwrap_or_default()
            .into_bytes();
        SessionConfig {
            idp_url,
            idp_internal_url,
            portal_domain,
            client_id,
            client_secret,
            session_secret,
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(15))
                .build()
                .unwrap_or_default(),
        }
    }

    pub fn redirect_uri(&self) -> String {
        format!("https://{}/api/auth/callback", self.portal_domain)
    }

    /// The portal's authenticated surfaces only open when an IdP AND a
    /// real (rotated) session secret are configured. Everything else is
    /// fail-closed.
    /// The base the portal itself uses for server-side OIDC calls.
    fn idp_internal_base(&self) -> String {
        let v = self.idp_internal_url.trim();
        if v.is_empty() {
            self.idp_url.trim_end_matches('/').to_string()
        } else {
            v.trim_end_matches('/').to_string()
        }
    }

    /// Public `Host` header to present when the server-side base differs
    /// from the browser-facing `idp_url` (demo/CI talk to the IdP's
    /// plain-HTTP listener, which only serves the OIDC surface for its
    /// external domain). None when no internal base is configured — the
    /// request then naturally carries the public Host.
    fn idp_public_host(&self) -> Option<String> {
        if self.idp_internal_url.trim().is_empty() {
            return None;
        }
        let rest = self.idp_url.split("://").nth(1)?;
        let host = rest.split('/').next().unwrap_or(rest);
        (!host.is_empty()).then(|| host.to_string())
    }

    pub fn enabled(&self) -> bool {
        !self.idp_url.trim().is_empty()
            && !self.client_id.trim().is_empty()
            && self.secret_is_strong()
    }

    fn secret_is_strong(&self) -> bool {
        self.session_secret.len() >= SESSION_SECRET_MIN
            && !String::from_utf8_lossy(&self.session_secret)
                .trim_start()
                .starts_with("CHANGEME_")
    }

    /// Startup guard: warn loudly when the interactive surfaces are closed
    /// because the session secret is missing or still a placeholder.
    pub fn warn_if_closed(&self) {
        if self.session_secret.is_empty() {
            tracing::warn!(
                "LOGIN_SESSION_COOKIE_SECRET unset — intercom/polls are FAIL-CLOSED \
                 (401 for everyone) until you set one via init-env.sh/demo scripts"
            );
        } else if !self.secret_is_strong() {
            tracing::warn!(
                "LOGIN_SESSION_COOKIE_SECRET is a CHANGEME_ placeholder or <32 bytes — \
                 intercom/polls are FAIL-CLOSED (401 for everyone). Rotate it."
            );
        }
    }
}

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

fn sign(cfg: &SessionConfig, payload: &str) -> Vec<u8> {
    let mut mac = HmacSha256::new_from_slice(&cfg.session_secret).expect("valid key length");
    mac.update(payload.as_bytes());
    mac.finalize().into_bytes().to_vec()
}

/// Mint a signed session cookie value. The IdP already vouched for the
/// identity (userinfo); the cookie only needs to be unforgeable + bounded.
pub fn mint_session(cfg: &SessionConfig, sub: &str, name: &str, email: &str) -> String {
    let session = Session {
        sub: sub.into(),
        name: name.into(),
        email: email.into(),
        exp: now() + SESSION_TTL as i64,
    };
    let payload = b64(&serde_json::to_vec(&session).expect("session serializes"));
    let sig = sign(cfg, &payload);
    format!("{payload}.{}", b64(&sig))
}

/// Verify a session cookie value. `None` for any tampering, wrong secret,
/// malformed data or an expired session.
pub fn verify_session(cfg: &SessionConfig, token: &str) -> Option<Session> {
    let (payload, sig) = token.split_once('.')?;
    let mut mac = HmacSha256::new_from_slice(&cfg.session_secret).ok()?;
    mac.update(payload.as_bytes());
    mac.verify_slice(&unb64(sig)?).ok()?;
    let session: Session = serde_json::from_slice(&unb64(payload)?).ok()?;
    if session.exp < now() {
        return None;
    }
    if session.sub.is_empty() {
        return None;
    }
    Some(session)
}

/// Constant-time string equality via HMAC tags (never leak how much matched).
fn ct_eq(a: &str, b: &str) -> bool {
    let tag = |s: &str| {
        let mut mac = HmacSha256::new_from_slice(b"opensme-state-compare").expect("key ok");
        mac.update(s.as_bytes());
        mac.finalize().into_bytes().to_vec()
    };
    let ta = tag(a);
    let tb = tag(b);
    ta.len() == tb.len()
        && ta
            .into_iter()
            .zip(tb)
            .fold(0u8, |acc, (x, y)| acc | (x ^ y))
            == 0
}

/// Random OAuth state for the authorize round trip.
pub fn mint_state() -> String {
    let mut buf = [0u8; 32];
    let _ = getrandom::getrandom(&mut buf);
    b64(&buf)
}

fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let header = headers.get(COOKIE)?.to_str().ok()?;
    for part in header.split(';') {
        let (key, value) = part.trim().split_once('=')?;
        if key == name {
            return Some(value.to_string());
        }
    }
    None
}

fn unauthorized(body: &str) -> Response {
    (
        StatusCode::UNAUTHORIZED,
        Json(serde_json::json!({ "error": body })),
    )
        .into_response()
}

/// Auth firewall for the interactive surfaces (intercom, polls, their
/// upload/file endpoints). Every request must carry a valid signed session
/// cookie; with no/weak session config the surface is closed, not open.
pub async fn require_session(
    State(config): State<Arc<AppConfig>>,
    headers: HeaderMap,
    request: Request,
    next: Next,
) -> Response {
    if !config.session.enabled() {
        return unauthorized("auth is not configured (session secret / IdP missing)");
    }
    let Some(cookie) = cookie_value(&headers, PORTAL_SESSION_COOKIE) else {
        return unauthorized("session required");
    };
    match verify_session(&config.session, &cookie) {
        Some(session) => {
            let mut request = request;
            request.extensions_mut().insert(session);
            next.run(request).await
        }
        None => unauthorized("session invalid or expired"),
    }
}

/// IdP authorize path for the portal's OIDC client.
///
/// Zitadel v2 (the shipped IdP, idm/zitadel.yml, pinned v4.19) serves the
/// OIDC authorize endpoint at `/oauth/v2/authorize` — the same path the
/// OpenCloud/Notes/webmail integrations and every e2e journey use via
/// discovery (`authorization_endpoint`). The legacy `/oidc/v1/authorize`
/// returns Zitadel's `404 page not found`, which silently broke real
/// portal login on compose-deployed stacks.
pub const AUTHORIZE_PATH: &str = "/oauth/v2/authorize";

/// IdP authorize URL for the portal's OIDC client.
pub fn authorize_url(cfg: &SessionConfig, state: &str) -> String {
    let base = cfg.idp_url.trim_end_matches('/');
    format!(
        "{base}{AUTHORIZE_PATH}?response_type=code&client_id={}&redirect_uri={}\
         &scope={}&state={}",
        urlencode(&cfg.client_id),
        urlencode(&cfg.redirect_uri()),
        urlencode("openid profile email"),
        urlencode(state),
    )
}

fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// GET /api/session — public session probe for the UI chrome.
///
/// Always answers 200 so the header can tell "anonymous" from "signed in"
/// without an error path: `{"authenticated":false}` when no valid portal
/// session cookie is present, or `{"authenticated":true,"user":{...}}`
/// otherwise. Deliberately leaks only: authenticated boolean + the identity
/// the cookie itself vouches for (sub/name/email — nothing secret).
pub async fn handle_session(State(config): State<Arc<AppConfig>>, headers: HeaderMap) -> Response {
    let session = if config.session.enabled() {
        cookie_value(&headers, PORTAL_SESSION_COOKIE)
            .and_then(|v| verify_session(&config.session, &v))
    } else {
        None
    };
    let body = match session {
        Some(s) => serde_json::json!({
            "authenticated": true,
            "user": { "sub": s.sub, "name": s.name, "email": s.email },
        }),
        None => serde_json::json!({ "authenticated": false }),
    };
    (StatusCode::OK, Json(body)).into_response()
}

/// GET /api/auth/login — start the OIDC flow. 303 to the IdP with a fresh
/// `state` bound in an HttpOnly short-lived cookie; the callback verifies it.
pub async fn handle_login(State(config): State<Arc<AppConfig>>) -> Response {
    if !config.session.enabled() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({
                "error": "login is not configured (IdP + client + session secret required)"
            })),
        )
            .into_response();
    }
    let state = mint_state();
    let url = authorize_url(&config.session, &state);
    Response::builder()
        .status(StatusCode::SEE_OTHER)
        .header(LOCATION, url)
        .header(
            SET_COOKIE,
            format!(
                "{STATE_COOKIE}={state}; Path=/api/auth; HttpOnly; SameSite=Lax; Secure; \
                 Max-Age={STATE_TTL_SECS}"
            ),
        )
        .body(axum::body::Body::empty())
        .expect("static redirect always valid")
}

#[derive(Deserialize)]
pub struct CallbackParams {
    code: String,
    /// OIDC `state` echoed by the IdP. A real browser round trip always
    /// carries one, but the v2 Session-API machine flow (api_login drives
    /// the IdP directly) may return a callback without it — tolerate that
    /// while still enforcing the CSRF equality whenever a state is present.
    state: Option<String>,
}

/// GET /api/auth/callback — IdP sent the human back with `code`+`state`.
/// Verifies state (CSRF), exchanges the code, confirms identity via
/// `/userinfo`, mints the signed session cookie and lands on the portal.
pub async fn handle_callback(
    State(config): State<Arc<AppConfig>>,
    Query(params): Query<CallbackParams>,
    headers: HeaderMap,
) -> Response {
    let Some(state_cookie) = cookie_value(&headers, STATE_COOKIE) else {
        return unauthorized("missing state cookie");
    };
    let clear_state =
        || format!("{STATE_COOKIE}=; Path=/api/auth; HttpOnly; SameSite=Lax; Secure; Max-Age=0");
    // state may legitimately be absent in the v2 Session-API machine flow
    // (no browser round trip); a browser flow always echoes state — enforce
    // the equality check whenever it is present.
    if let Some(echoed) = &params.state {
        if !ct_eq(&state_cookie, echoed) {
            tracing::warn!("OIDC callback state mismatch (CSRF suspected)");
            return Response::builder()
                .status(StatusCode::BAD_REQUEST)
                .header(SET_COOKIE, clear_state())
                .body(axum::body::Body::empty())
                .expect("static");
        }
    } else {
        tracing::warn!("OIDC callback without state (v2 Session-API machine flow?)");
    }
    match exchange_and_userinfo(&config.session, &params.code).await {
        Ok(user) => {
            let cookie = mint_session(&config.session, &user.sub, &user.name, &user.email);
            Response::builder()
                .status(StatusCode::FOUND)
                .header(LOCATION, "/")
                .header(SET_COOKIE, clear_state())
                .header(
                    SET_COOKIE,
                    format!(
                        "{PORTAL_SESSION_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Lax; Secure; \
                         Max-Age={SESSION_TTL}"
                    ),
                )
                .body(axum::body::Body::empty())
                .expect("static")
        }
        Err(e) => {
            tracing::warn!(error = %e, "OIDC callback token/userinfo exchange failed");
            Response::builder()
                .status(StatusCode::FOUND)
                .header(LOCATION, "/")
                .header(SET_COOKIE, clear_state())
                .body(axum::body::Body::empty())
                .expect("static")
        }
    }
}

/// Identity the IdP vouched for via /userinfo.
pub struct UserInfo {
    pub sub: String,
    pub name: String,
    pub email: String,
}

#[derive(Debug)]
pub enum ExchangeError {
    Http(String),
    Status(u16),
    NoAccessToken,
    Userinfo(String),
    NoSubject,
}

impl std::fmt::Display for ExchangeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ExchangeError::Http(e) => write!(f, "token request failed: {e}"),
            ExchangeError::Status(s) => write!(f, "token endpoint returned {s}"),
            ExchangeError::NoAccessToken => write!(f, "token response without access_token"),
            ExchangeError::Userinfo(s) => write!(f, "userinfo failed: {s}"),
            ExchangeError::NoSubject => write!(f, "userinfo without sub"),
        }
    }
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
}

#[derive(Deserialize)]
struct UserinfoResponse {
    sub: String,
    #[serde(default)]
    preferred_username: Option<String>,
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    email: Option<String>,
}

/// Exchange the authorization code for an access token and confirm the
/// identity against the IdP's /userinfo — the one authoritative "this token
/// is real" check (the JWT-signature equivalent, without JWKS parsing).
///
/// Server-side legs run against `idp_internal_base()` (plain HTTP inside
/// the compose network in demo/CI). Those stacks' Zitadel only serves the
/// OIDC surface for its EXTERNAL domain, so the public `Host` is sent
/// explicitly; production (no internal base) keeps the default Host.
async fn exchange_and_userinfo(cfg: &SessionConfig, code: &str) -> Result<UserInfo, ExchangeError> {
    let base = cfg.idp_internal_base();
    // Zitadel v4.19 serves the token endpoint at /oauth/v2/token (the
    // discovery document, what every other integration uses) — /oidc/v1/
    // is only userinfo + end_session. See AUTHORIZE_PATH for the same
    // class of v1->v2 drift.
    let mut token_req = cfg.http.post(format!("{base}/oauth/v2/token")).form(&[
        ("grant_type", "authorization_code"),
        ("code", code),
        ("redirect_uri", cfg.redirect_uri().as_str()),
        ("client_id", cfg.client_id.as_str()),
        ("client_secret", cfg.client_secret.as_str()),
    ]);
    if let Some(host) = cfg.idp_public_host() {
        token_req = token_req.header(reqwest::header::HOST, host);
    }
    let token_resp = token_req
        .send()
        .await
        .map_err(|e| ExchangeError::Http(e.to_string()))?;
    if !token_resp.status().is_success() {
        return Err(ExchangeError::Status(token_resp.status().as_u16()));
    }
    let token: TokenResponse = token_resp
        .json()
        .await
        .map_err(|e| ExchangeError::Http(e.to_string()))?;
    if token.access_token.is_empty() {
        return Err(ExchangeError::NoAccessToken);
    }
    let mut user_req = cfg
        .http
        .get(format!("{base}/oidc/v1/userinfo"))
        .bearer_auth(&token.access_token);
    if let Some(host) = cfg.idp_public_host() {
        user_req = user_req.header(reqwest::header::HOST, host);
    }
    let user_resp = user_req
        .send()
        .await
        .map_err(|e| ExchangeError::Userinfo(e.to_string()))?;
    if !user_resp.status().is_success() {
        return Err(ExchangeError::Userinfo(format!(
            "status {}",
            user_resp.status().as_u16()
        )));
    }
    let info: UserinfoResponse = user_resp
        .json()
        .await
        .map_err(|e| ExchangeError::Userinfo(e.to_string()))?;
    if info.sub.is_empty() {
        return Err(ExchangeError::NoSubject);
    }
    let sub = info.sub;
    Ok(UserInfo {
        sub: sub.clone(),
        name: info.name.or(info.preferred_username).unwrap_or(sub),
        email: info.email.unwrap_or_default(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request as HttpRequest;
    use axum::middleware;
    use axum::routing::get;
    use axum::Router;
    use tower::ServiceExt;

    const SECRET: &str = "0123456789abcdef0123456789abcdef"; // 32 bytes, not CHANGEME

    fn cfg() -> SessionConfig {
        SessionConfig {
            idp_url: "https://auth.example".into(),
            idp_internal_url: String::new(),
            portal_domain: "portal.example".into(),
            client_id: "opensme-portal".into(),
            client_secret: "client-secret".into(),
            session_secret: SECRET.as_bytes().to_vec(),
            http: reqwest::Client::new(),
        }
    }

    #[test]
    fn enabled_requires_idp_client_and_rotated_secret() {
        let mut c = cfg();
        assert!(c.enabled());
        c.idp_url = String::new();
        assert!(!c.enabled());
        c.idp_url = "https://auth.example".into();
        c.client_id = String::new();
        assert!(!c.enabled());
        c.client_id = "opensme-portal".into();
        c.session_secret = b"tooshort".to_vec();
        assert!(!c.enabled());
        c.session_secret = b"CHANGEME_session_cookie_secret_at_least_32_chars".to_vec();
        assert!(!c.enabled()); // fail-closed while a placeholder
        c.session_secret = SECRET.as_bytes().to_vec();
        assert!(c.enabled());
    }

    #[test]
    fn session_probe_anonymous_is_200_false() {
        let sc = cfg();
        let mut app = crate::tests::cfg_with("https://auth.example", None, "");
        app.session = sc;
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/api/session")
            .body(Body::from(""))
            .unwrap();
        let res = one(
            req,
            Router::new()
                .route("/api/session", get(handle_session))
                .with_state(std::sync::Arc::new(app)),
        );
        assert_eq!(res.status(), StatusCode::OK);
        let body: serde_json::Value = serde_json::from_slice(
            &tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap()
                .block_on(axum::body::to_bytes(res.into_body(), usize::MAX))
                .unwrap(),
        )
        .unwrap();
        assert_eq!(body["authenticated"], false);
    }

    #[test]
    fn session_probe_signed_in_returns_user() {
        let sc = cfg();
        let cookie_v = mint_session(&sc, "sub-1", "Ada Demo", "ada@example");
        let mut app = crate::tests::cfg_with("https://auth.example", None, "");
        app.session = sc;
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/api/session")
            .header("cookie", format!("{PORTAL_SESSION_COOKIE}={cookie_v}"))
            .body(Body::from(""))
            .unwrap();
        let res = one(
            req,
            Router::new()
                .route("/api/session", get(handle_session))
                .with_state(std::sync::Arc::new(app)),
        );
        assert_eq!(res.status(), StatusCode::OK);
        let body: serde_json::Value = serde_json::from_slice(
            &tokio::runtime::Builder::new_current_thread()
                .build()
                .unwrap()
                .block_on(axum::body::to_bytes(res.into_body(), usize::MAX))
                .unwrap(),
        )
        .unwrap();
        assert_eq!(body["authenticated"], true);
        assert_eq!(body["user"]["name"], "Ada Demo");
        assert_eq!(body["user"]["email"], "ada@example");
    }

    #[test]
    fn mint_verify_roundtrip() {
        let c = cfg();
        let token = mint_session(&c, "user-1", "Ada", "ada@example");
        let s = verify_session(&c, &token).expect("valid");
        assert_eq!(s.sub, "user-1");
        assert_eq!(s.name, "Ada");
        assert_eq!(s.email, "ada@example");
        assert!(s.exp > now());
    }

    #[test]
    fn verify_rejects_tampered_payload() {
        let c = cfg();
        let token = mint_session(&c, "user-1", "Ada", "ada@example");
        let (p, _sig) = token.split_once('.').unwrap();
        let forged = format!("{p}.{}", b64(&[0u8; 32]));
        assert!(verify_session(&c, &forged).is_none());
        let mut bytes = unb64(p).unwrap();
        bytes[1] ^= 0x01;
        let p2 = b64(&bytes);
        assert!(verify_session(&c, &format!("{p2}.{_sig}")).is_none());
    }

    #[test]
    fn verify_rejects_wrong_secret() {
        let c = cfg();
        let token = mint_session(&c, "u", "N", "e");
        let other = SessionConfig {
            session_secret: b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_vec(),
            ..c.clone()
        };
        assert!(verify_session(&other, &token).is_none());
    }

    #[test]
    fn verify_rejects_garbage() {
        let c = cfg();
        assert!(verify_session(&c, "not-a-cookie").is_none());
        assert!(verify_session(&c, "").is_none());
        assert!(verify_session(&c, "aaaa.").is_none());
    }

    #[test]
    fn verify_rejects_expired() {
        let c = cfg();
        let s = Session {
            sub: "u".into(),
            name: "N".into(),
            email: "e".into(),
            exp: now() - 10,
        };
        let payload = b64(&serde_json::to_vec(&s).unwrap());
        let sig = sign(&c, &payload);
        let token = format!("{payload}.{}", b64(&sig));
        assert!(verify_session(&c, &token).is_none());
    }

    #[test]
    fn authorize_url_shape() {
        let c = cfg();
        let url = authorize_url(&c, "state-xyz");
        assert!(url.starts_with("https://auth.example/oauth/v2/authorize?"));
        assert!(url.contains("response_type=code"));
        assert!(url.contains("client_id=opensme-portal"));
        assert!(url.contains("redirect_uri=https%3A%2F%2Fportal.example%2Fapi%2Fauth%2Fcallback"));
        assert!(url.contains("scope=openid%20profile%20email"));
        assert!(url.contains("state=state-xyz"));
    }

    #[test]
    fn redirect_uri_built_from_portal_domain() {
        let c = cfg();
        assert_eq!(c.redirect_uri(), "https://portal.example/api/auth/callback");
    }

    #[test]
    fn state_mint_is_random_and_roundtrips_ct_eq() {
        let a = mint_state();
        let b = mint_state();
        assert_ne!(a, b);
        assert!(ct_eq(&a, &a));
        assert!(!ct_eq(&a, &b));
    }

    /// A tiny protected route that echoes the session sub from extensions,
    /// so middleware behaviour is observable through the real router.
    async fn echo_session(
        axum::extract::Extension(session): axum::extract::Extension<Session>,
    ) -> impl IntoResponse {
        (StatusCode::OK, session.sub)
    }

    fn protected_router(idp_url: &str, secret: &[u8]) -> Router {
        let mut app = crate::tests::cfg_with("", None, "");
        let sc = SessionConfig {
            idp_url: idp_url.into(),
            idp_internal_url: String::new(),
            portal_domain: app.session.portal_domain.clone(),
            client_id: "opensme-portal".into(),
            client_secret: "client-secret".into(),
            session_secret: secret.to_vec(),
            http: reqwest::Client::new(),
        };
        app.session = sc;
        Router::new()
            .route("/protected", get(echo_session))
            .layer(middleware::from_fn_with_state(
                std::sync::Arc::new(app.clone()),
                require_session,
            ))
            .with_state(std::sync::Arc::new(app))
    }

    fn one(req: HttpRequest<Body>, router: Router) -> Response {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(router.oneshot(req))
            .unwrap()
    }

    #[test]
    fn protected_rejects_without_cookie() {
        let router = protected_router("https://auth.example", SECRET.as_bytes());
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/protected")
            .body(Body::empty())
            .unwrap();
        assert_eq!(one(req, router).status(), StatusCode::UNAUTHORIZED);
    }

    #[test]
    fn protected_rejects_garbage_cookie() {
        let router = protected_router("https://auth.example", SECRET.as_bytes());
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/protected")
            .header("Cookie", format!("{PORTAL_SESSION_COOKIE}=garbage"))
            .body(Body::empty())
            .unwrap();
        assert_eq!(one(req, router).status(), StatusCode::UNAUTHORIZED);
    }

    #[test]
    fn protected_denies_when_secret_is_weak() {
        // Fail-closed: an operator who forgot to rotate the cookie secret
        // gets 401 for everyone, not accidentally-open intercom/polls.
        let router = protected_router("https://auth.example", b"CHANGEME_short");
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/protected")
            .body(Body::empty())
            .unwrap();
        assert_eq!(one(req, router).status(), StatusCode::UNAUTHORIZED);
        // ...even with what would otherwise be a valid token:
        let router = protected_router("https://auth.example", b"CHANGEME_short");
        let token = mint_session(
            &SessionConfig {
                session_secret: b"CHANGEME_short".to_vec(),
                ..cfg()
            },
            "u",
            "N",
            "e",
        );
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/protected")
            .header("Cookie", format!("{PORTAL_SESSION_COOKIE}={token}"))
            .body(Body::empty())
            .unwrap();
        assert_eq!(one(req, router).status(), StatusCode::UNAUTHORIZED);
    }

    #[test]
    fn protected_accepts_valid_session() {
        let router = protected_router("https://auth.example", SECRET.as_bytes());
        let token = mint_session(&cfg(), "user-42", "Berta", "berta@example");
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/protected")
            .header("Cookie", format!("{PORTAL_SESSION_COOKIE}={token}"))
            .body(Body::empty())
            .unwrap();
        let res = one(req, router);
        assert_eq!(res.status(), StatusCode::OK);
        let body = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
            .block_on(axum::body::to_bytes(res.into_body(), 4096))
            .unwrap();
        assert_eq!(&body[..], b"user-42");
    }

    #[test]
    fn login_disabled_when_not_configured() {
        let mut app = crate::tests::cfg_with("", None, "");
        app.session = SessionConfig {
            session_secret: Vec::new(),
            ..cfg()
        };
        let router = crate::build_router(std::sync::Arc::new(app));
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/api/auth/login")
            .body(Body::empty())
            .unwrap();
        let res = one(req, router);
        assert_eq!(res.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[test]
    fn login_redirects_with_state_cookie() {
        let mut app = crate::tests::cfg_with("", None, "");
        app.session = cfg();
        let router = crate::build_router(std::sync::Arc::new(app));
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/api/auth/login")
            .body(Body::empty())
            .unwrap();
        let res = one(req, router);
        assert_eq!(res.status(), StatusCode::SEE_OTHER);
        let loc = res.headers().get(LOCATION).unwrap().to_str().unwrap();
        assert!(loc.starts_with("https://auth.example/oauth/v2/authorize?"));
        assert!(loc.contains("client_id=opensme-portal"));
        let sc = res.headers().get(SET_COOKIE).unwrap().to_str().unwrap();
        assert!(sc.starts_with("OPENSMESTATE="));
        assert!(sc.contains("Path=/api/auth"));
        assert!(sc.contains("HttpOnly"));
        assert!(sc.contains("Secure"));
    }

    /// end-to-end callback against a mocked IdP (token + userinfo):
    /// valid code → session cookie minted; bad userinfo → no session.
    #[tokio::test]
    async fn callback_exchange_against_mock_idp() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let app = axum::Router::new()
                .route(
                    "/oauth/v2/token",
                    axum::routing::post(
                        |axum::extract::Form(form): axum::extract::Form<
                            std::collections::HashMap<String, String>,
                        >| async move {
                            let code = form.get("code").cloned().unwrap_or_default();
                            if code == "good-code" {
                                (
                                    StatusCode::OK,
                                    axum::Json(serde_json::json!({ "access_token": "at-123" })),
                                )
                            } else {
                                (StatusCode::BAD_REQUEST, axum::Json(serde_json::json!({})))
                            }
                        },
                    ),
                )
                .route(
                    "/oidc/v1/userinfo",
                    axum::routing::get(|headers: axum::http::HeaderMap| async move {
                        let bearer = headers
                            .get("authorization")
                            .map(|v| v.to_str().unwrap_or_default().to_string())
                            .unwrap_or_default();
                        if bearer == "Bearer at-123" {
                            (
                                StatusCode::OK,
                                axum::Json(serde_json::json!({
                                    "sub": "user-mock",
                                    "preferred_username": "mockuser",
                                    "email": "m@example",
                                })),
                            )
                        } else {
                            (StatusCode::UNAUTHORIZED, axum::Json(serde_json::json!({})))
                        }
                    }),
                );
            axum::serve(listener, app).await.unwrap();
        });

        let mut sc = cfg();
        sc.idp_url = format!("http://{addr}");
        let user = exchange_and_userinfo(&sc, "good-code").await.unwrap();
        assert_eq!(user.sub, "user-mock");
        assert_eq!(user.name, "mockuser");
        assert_eq!(user.email, "m@example");

        assert!(matches!(
            exchange_and_userinfo(&sc, "bad-code").await,
            Err(ExchangeError::Status(400))
        ));
    }

    #[tokio::test]
    async fn callback_mints_session_and_redirects_home() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let app = axum::Router::new()
                .route(
                    "/oauth/v2/token",
                    axum::routing::post(
                        |axum::extract::Form(form): axum::extract::Form<
                            std::collections::HashMap<String, String>,
                        >| async move {
                            let code = form.get("code").cloned().unwrap_or_default();
                            if code == "good-code" {
                                (
                                    StatusCode::OK,
                                    axum::Json(serde_json::json!({ "access_token": "at-123" })),
                                )
                            } else {
                                (StatusCode::BAD_REQUEST, axum::Json(serde_json::json!({})))
                            }
                        },
                    ),
                )
                .route(
                    "/oidc/v1/userinfo",
                    axum::routing::get(|headers: axum::http::HeaderMap| async move {
                        let bearer = headers
                            .get("authorization")
                            .map(|v| v.to_str().unwrap_or_default().to_string())
                            .unwrap_or_default();
                        if bearer == "Bearer at-123" {
                            (
                                StatusCode::OK,
                                axum::Json(serde_json::json!({
                                    "sub": "user-mock",
                                    "name": "Mock User",
                                })),
                            )
                        } else {
                            (StatusCode::UNAUTHORIZED, axum::Json(serde_json::json!({})))
                        }
                    }),
                );
            axum::serve(listener, app).await.unwrap();
        });

        let mut app = crate::tests::cfg_with("", None, "");
        let mut sc = cfg();
        sc.idp_url = format!("http://{addr}");
        sc.session_secret = SECRET.as_bytes().to_vec();
        app.session = sc.clone();

        let _state = mint_state();
        let router = crate::build_router(std::sync::Arc::new(app));
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/api/auth/callback?code=good-code&state=abc123")
            .header("Cookie", "OPENSMESTATE=abc123")
            .body(Body::empty())
            .unwrap();
        let res = router.oneshot(req).await.unwrap();
        assert_eq!(res.status(), StatusCode::FOUND);
        assert_eq!(res.headers().get(LOCATION).unwrap(), "/");
        let sc_header = res.headers().get_all(SET_COOKIE);
        let cookies: Vec<String> = sc_header
            .iter()
            .filter_map(|v| v.to_str().ok())
            .map(|s| s.to_string())
            .collect();
        let session_cookie = cookies
            .iter()
            .find(|c| c.starts_with(&format!("{PORTAL_SESSION_COOKIE}=")))
            .expect("session cookie set");
        let value = session_cookie
            .split_once('=')
            .unwrap()
            .1
            .split(';')
            .next()
            .unwrap();
        let s = verify_session(&sc, value).expect("minted session verifies");
        assert_eq!(s.sub, "user-mock");
        assert_eq!(s.name, "Mock User");

        // wrong state → 400, no session cookie
        let mut app = crate::tests::cfg_with("", None, "");
        app.session = sc.clone();
        let router = crate::build_router(std::sync::Arc::new(app));
        let req = HttpRequest::builder()
            .method("GET")
            .uri("/api/auth/callback?code=good-code&state=wrong")
            .header("Cookie", "OPENSMESTATE=abc123")
            .body(Body::empty())
            .unwrap();
        let res = router.oneshot(req).await.unwrap();
        assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    }
}
