//! Portal cloud — generalized OpenCloud (WebDAV + OCS) client.
//!
//! Extracted from `polls.rs` so every portal feature can store files in
//! OpenCloud and mint public share links — not just polls. OpenCloud is
//! Nextcloud-compatible: files live under a WebDAV base
//! (`<host>/remote.php/dav/files/<user>`, internal compose-net host, no
//! egress proxy needed), and public links are minted through the OCS
//! shares API on the same host.
//!
//! Credentials and URLs come ONLY from env — nothing here hard-codes a
//! host or credential:
//! - `CLOUD_WEBDAV_URL` / `CLOUD_WEBDAV_USER` / `CLOUD_WEBDAV_PASS`
//! - optional `CLOUD_SHARE_URL` override, else the OCS shares endpoint
//!   is derived from the WebDAV base's scheme://authority +
//!   `/ocs/v2.php/apps/files_sharing/api/v1/shares`.
//!
//! All remote paths are RELATIVE to the WebDAV base (`"dir/name"`).
//! Every segment is percent-encoded and unsafe components (`..`, `.`,
//! empty segments from leading/trailing/double slashes) are rejected
//! BEFORE any request URL is built, so a relative path can never escape
//! the configured base.
//!
//! The network operations mirror the established polls upload pattern
//! exactly: reqwest with Basic auth, non-2xx → `Err` mentioning the
//! operation and the HTTP status, connection failures → `Err` with
//! `unreachable`.

use axum::{http::StatusCode, Json};
use serde::{Deserialize, Serialize};
use std::fmt;
use std::time::Duration;

// ── env + OCS helpers (mirroring the original polls.rs seed) ─────────

/// OCS (OpenCloud/Nextcloud "Open Collaboration Services") API v2 base
/// path, mounted under the cloud host's root. Exposed as a public
/// constant so callers building bespoke OCS endpoints share one source
/// of truth for the version path.
#[allow(dead_code)] // public OCS API surface — consumers adopt incrementally
pub const OCS_API_V2: &str = "/ocs/v2.php";

/// OCS files_sharing API base path. The concrete shares endpoint is
/// `<host>{OCS_SHARE_API}/shares` (POST to create, GET to list).
pub const OCS_SHARE_API: &str = "/ocs/v2.php/apps/files_sharing/api/v1";

/// Read an env var, trimmed; None when unset or blank.
pub fn non_empty(key: &str) -> Option<String> {
    std::env::var(key)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

/// WebDAV base host part: scheme://authority (strip everything after the
/// authority, so the OCS endpoint derives from the same host).
pub fn dav_base_host_base(dav_base: &str) -> String {
    let trimmed = dav_base.trim_end_matches('/');
    if let Some(rest) = trimmed.strip_prefix("https://") {
        let authority = rest.split('/').next().unwrap_or(rest);
        format!("https://{authority}")
    } else if let Some(rest) = trimmed.strip_prefix("http://") {
        let authority = rest.split('/').next().unwrap_or(rest);
        format!("http://{authority}")
    } else {
        trimmed.to_string()
    }
}

/// Resolve the OCS shares endpoint URL from explicit overrides or the
/// WebDAV base host. Precedence (mirrors [`CloudFiles::from_parts`]):
///
/// 1. `share_url_override` — full shares endpoint (legacy `CLOUD_SHARE_URL`),
/// 2. `ocs_url_override`   — OCS base (`CLOUD_OCS_URL`); endpoint =
///    `<ocs>{OCS_SHARE_API}/shares`,
/// 3. derived from `dav_base`'s scheme://authority + `{OCS_SHARE_API}/shares`.
///
/// Pure (no env, no I/O) so the precedence is unit-testable without
/// process-global env races.
pub fn resolve_share_url(
    dav_base: &str,
    share_url_override: Option<&str>,
    ocs_url_override: Option<&str>,
) -> String {
    if let Some(s) = share_url_override.map(str::trim).filter(|s| !s.is_empty()) {
        return s.to_string();
    }
    if let Some(o) = ocs_url_override.map(str::trim).filter(|s| !s.is_empty()) {
        return format!("{}{}/shares", o.trim_end_matches('/'), OCS_SHARE_API);
    }
    format!(
        "{}{}/shares",
        dav_base_host_base(dav_base),
        OCS_SHARE_API
    )
}

/// Extract the public share URL from an OCS share-create response
/// (format=json → {"ocs":{"meta":{...},"data":{"url":...}}}.
pub fn parse_share_url(body: &[u8]) -> Result<String, String> {
    let v: serde_json::Value =
        serde_json::from_slice(body).map_err(|e| format!("share response not json: {e}"))?;
    let url = v["ocs"]["data"]["url"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "share response carries no url".to_string())?;
    Ok(url.to_string())
}

/// Extract the upstream HTTP status an operation error carries
/// (`"MKCOL x failed: HTTP 405"` → 405). None for transport-level
/// failures (`unreachable`) and parse errors — callers map those to a
/// gateway status. Offline-testable core of the bridge error passthrough.
pub fn http_status(err: &str) -> Option<u16> {
    let at = err.find("HTTP ")?;
    let rest = &err[at + "HTTP ".len()..];
    let digits = &rest[..rest
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(rest.len())];
    digits.parse().ok()
}

// ── OCS response types (typed mirrors of the OCS v2 JSON envelope) ───

/// OCS error type for share management operations.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum OcsError {
    /// HTTP error with status code and message
    HttpError(u16, String),
    /// JSON parsing error
    ParseError(String),
    /// Share response carries no URL
    NoShareUrl,
    /// Authentication or permission error
    AuthError(String),
}

impl fmt::Display for OcsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            OcsError::HttpError(code, msg) => write!(f, "OCS HTTP error {}: {}", code, msg),
            OcsError::ParseError(msg) => write!(f, "OCS parse error: {}", msg),
            OcsError::NoShareUrl => write!(f, "OCS share response carries no URL"),
            OcsError::AuthError(msg) => write!(f, "OCS auth error: {}", msg),
        }
    }
}

impl std::error::Error for OcsError {}

/// OCS API meta block: `{"status":...,"statuscode":...,"message":...}`.
/// Every field defaults so a sparse meta block still parses.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct OcsMeta {
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub statuscode: i64,
    #[serde(default)]
    pub message: String,
}

/// Inner `{"meta":...,"data":...}` body of an OCS v2 response.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OcsBody<T> {
    #[serde(default)]
    pub meta: OcsMeta,
    pub data: T,
}

/// Typed OCS v2 response envelope: `{"ocs":{"meta":{...},"data":...}}`.
/// Generic over `data` so the same envelope parses a single share
/// (`OcsResponse<ShareInfo>`) or a share list (`OcsResponse<ShareList>`).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OcsResponse<T> {
    pub ocs: OcsBody<T>,
}

/// One OCS share record — the subset of fields Nextcloud/OpenCloud
/// returns for a share-create / share-list response. Unknown fields are
/// ignored; every known field defaults so partial payloads still parse.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ShareInfo {
    #[serde(default)]
    pub id: i64,
    #[serde(default, rename = "share_type")]
    pub share_type: i64,
    #[serde(default, rename = "uid_owner")]
    pub uid_owner: String,
    #[serde(default, rename = "displayname_owner")]
    pub displayname_owner: String,
    #[serde(default, rename = "path")]
    pub path: String,
    #[serde(default, rename = "item_type")]
    pub item_type: String,
    #[serde(default, rename = "mimetype")]
    pub mimetype: String,
    #[serde(default, rename = "file_target")]
    pub file_target: String,
    #[serde(default, rename = "url")]
    pub url: String,
    #[serde(default, rename = "token")]
    pub token: String,
    #[serde(default, rename = "expiration")]
    pub expiration: Option<String>,
    #[serde(default, rename = "permissions")]
    pub permissions: i64,
    #[serde(default, rename = "share_with")]
    pub share_with: String,
    #[serde(default, rename = "share_with_displayname")]
    pub share_with_displayname: String,
    #[serde(default, rename = "name")]
    pub name: String,
}

/// Convenience getter for the share link (same as `url`).
impl ShareInfo {
    pub fn share_link(&self) -> &str {
        &self.url
    }
}

/// A list of OCS shares — the `data` array of a share-list response.
/// (De)serializes as a bare JSON array so `OcsResponse<ShareList>`
/// matches `{"ocs":{"data":[{...},{...}]}}` directly.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct ShareList {
    pub shares: Vec<ShareInfo>,
}

impl<'de> Deserialize<'de> for ShareList {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let shares = Vec::<ShareInfo>::deserialize(deserializer)?;
        Ok(ShareList { shares })
    }
}

/// Parse the `data.url` of an OCS share-create response typed as
/// `OcsResponse<ShareInfo>`. Mirrors [`parse_share_url`] but returns the
/// validated typed envelope, leaving the URL extraction to the caller.
pub fn parse_ocs_share_create(body: &[u8]) -> Result<OcsResponse<ShareInfo>, String> {
    serde_json::from_slice::<OcsResponse<ShareInfo>>(body)
        .map_err(|e| format!("share response not OCS json: {e}"))
        .and_then(|r| {
            if r.ocs.data.url.is_empty() {
                Err("share response carries no url".to_string())
            } else {
                Ok(r)
            }
        })
}

/// Parse an OCS share-list response body into a `Vec<ShareInfo>`.
pub fn parse_ocs_share_list(body: &[u8]) -> Result<Vec<ShareInfo>, String> {
    serde_json::from_slice::<OcsResponse<ShareList>>(body)
        .map(|r| r.ocs.data.shares)
        .map_err(|e| format!("share list response not OCS json: {e}"))
}

// ── path building (the safety-critical part) ─────────────────────────

/// Percent-encode ONE path segment per RFC 3986 `pchar`: unreserved +
/// sub-delims + `:@` pass through, everything else (spaces, umlauts,
/// `#`, `%`, …) becomes `%XX`. `/` is deliberately NOT in the safe set —
/// segments never swallow path structure.
pub fn encode_segment(segment: &str) -> String {
    let mut out = String::with_capacity(segment.len());
    for &b in segment.as_bytes() {
        if b.is_ascii_alphanumeric()
            || matches!(
                b,
                b'-' | b'_'
                    | b'.'
                    | b'~'
                    | b'!'
                    | b'$'
                    | b'&'
                    | b'\''
                    | b'('
                    | b')'
                    | b'*'
                    | b'+'
                    | b','
                    | b';'
                    | b'='
                    | b':'
                    | b'@'
            )
        {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Build the absolute request URL for a relative `dir/name` under the
/// WebDAV base. Rejects any component equal to `..` or `.`, and empty
/// segments (the residue of leading/trailing/double slashes) so a
/// relative path can never escape the base. Segments are
/// percent-encoded; the base is kept as configured (trailing slash
/// trimmed).
pub fn dav_url(dav_base: &str, rel_path: &str) -> Result<String, String> {
    let base = dav_base.trim_end_matches('/');
    if base.is_empty() {
        return Err("cloud dav base is empty".into());
    }
    let rel_path = rel_path.trim();
    if rel_path.is_empty() {
        return Err(format!("invalid cloud path {rel_path:?}: empty path"));
    }
    let mut url = base.to_string();
    for seg in rel_path.split('/') {
        if seg.is_empty() {
            return Err(format!(
                "invalid cloud path {rel_path:?}: empty path segment"
            ));
        }
        if seg == ".." || seg == "." {
            return Err(format!(
                "invalid cloud path {rel_path:?}: refusing {:?} segment",
                seg
            ));
        }
        url.push('/');
        url.push_str(&encode_segment(seg));
    }
    Ok(url)
}

/// Build a user-specific WebDAV base path from a username.
/// OpenCloud serves user files at /remote.php/dav/files/<username>/.
/// This function constructs the full path segment for a given user.
pub fn get_user_dav_path(username: &str) -> String {
    if username.is_empty() {
        String::new()
    } else {
        format!("/remote.php/dav/files/{}", encode_segment(username))
    }
}

/// Validate a RELATIVE directory/file path for the portal bridge:
/// non-empty (after trim), no leading `/` (absolute request URLs under
/// `remote.php/...` are built by [`dav_url`] — callers never supply
/// absolute text), no empty segments (leading/trailing/double slashes)
/// and no `..`/`.` segments. Returns the trimmed path. This is the gate
/// the `/api/cloud` handlers run BEFORE any network call; `dav_url`
/// re-checks when the request URL is built (defense in depth).
pub fn validate_path(raw: &str) -> Result<String, String> {
    let path = raw.trim();
    if path.is_empty() {
        return Err(format!("invalid cloud path {raw:?}: empty path"));
    }
    if path.starts_with('/') {
        return Err(format!(
            "invalid cloud path {raw:?}: path must be relative (no leading '/')"
        ));
    }
    for seg in path.split('/') {
        if seg.is_empty() {
            return Err(format!("invalid cloud path {raw:?}: empty path segment"));
        }
        if seg == ".." || seg == "." {
            return Err(format!(
                "invalid cloud path {raw:?}: refusing {seg:?} segment"
            ));
        }
    }
    Ok(path.to_string())
}

/// Validate a single entry NAME (mkdir/upload): one non-empty segment
/// after trim, no `/` (path structure), not `..` or `.` (traversal).
/// Percent-encoding of everything else happens in the encoder.
pub fn validate_name(raw: &str) -> Result<String, String> {
    let name = raw.trim();
    if name.is_empty() {
        return Err(format!("invalid cloud name {raw:?}: empty name"));
    }
    if name.contains('/') {
        return Err(format!(
            "invalid cloud name {raw:?}: name must be a single path segment"
        ));
    }
    if name == ".." || name == "." {
        return Err(format!("invalid cloud name {raw:?}: refusing {name:?}"));
    }
    Ok(name.to_string())
}

/// Join a validated directory path and a validated entry name into the
/// relative remote path (`dir/name`).
pub fn join(dir: &str, name: &str) -> String {
    format!("{dir}/{name}")
}

/// Percent-decode `%XX` sequences; malformed ones pass through verbatim.
fn percent_decode(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && bytes[i + 1].is_ascii_hexdigit()
            && bytes[i + 2].is_ascii_hexdigit()
        {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
            if let Ok(v) = u8::from_str_radix(hex, 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Path portion of an absolute URL (scheme://authority stripped),
/// percent-decoded — used to match the PROPFIND request URI against the
/// server's multistatus hrefs (which may be absolute paths or full URLs).
fn url_path(url: &str) -> String {
    let path = match url.split_once("://") {
        Some((_, rest)) => match rest.find('/') {
            Some(at) => &rest[at..],
            None => "",
        },
        None => url,
    };
    percent_decode(path)
}

/// Decoded path of a WebDAV `<href>` (full-URL hrefs are reduced to
/// their path part).
fn href_path(raw: &str) -> String {
    let decoded = percent_decode(raw.trim());
    match decoded.split_once("://") {
        Some((_, rest)) => rest
            .split_once('/')
            .map(|(_, p)| format!("/{p}"))
            .unwrap_or_else(|| "/".to_string()),
        None => decoded,
    }
}

// ── minimal XML support (no XML crate in the tree) ───────────────────

/// Inner content of every `<prefix:local>…</prefix:local>` block whose
/// local tag name matches (any namespace prefix or none, attributes
/// tolerated). Same-name elements never nest in PROPFIND responses.
fn xml_blocks<'a>(haystack: &'a str, local: &str) -> Vec<&'a str> {
    let mut out = Vec::new();
    let mut i = 0;
    while let Some(rel) = haystack[i..].find('<') {
        let start = i + rel;
        let Some(gt_rel) = haystack[start..].find('>') else {
            break;
        };
        let gt = start + gt_rel;
        let tag = haystack[start + 1..gt].trim_end_matches('/');
        let name = tag.split_whitespace().next().unwrap_or("");
        if name.rsplit(':').next() != Some(local) || name.is_empty() {
            i = gt + 1;
            continue;
        }
        let close = format!("</{name}>");
        let inner_start = gt + 1;
        let Some(end_rel) = haystack[inner_start..].find(&close) else {
            break;
        };
        out.push(&haystack[inner_start..inner_start + end_rel]);
        i = inner_start + end_rel + close.len();
    }
    out
}

/// Text of the FIRST `<prefix:local>…</prefix:local>` occurrence with
/// non-empty content (self-closing/empty stubs — typical of a 404
/// propstat — are skipped). None when the element is absent or empty.
fn xml_first_text(haystack: &str, local: &str) -> Option<String> {
    let mut i = 0;
    while let Some(rel) = haystack[i..].find('<') {
        let start = i + rel;
        let Some(gt_rel) = haystack[start..].find('>') else {
            return None;
        };
        let gt = start + gt_rel;
        let raw_tag = &haystack[start + 1..gt];
        let self_closing = raw_tag.trim_end().ends_with('/');
        let tag = raw_tag.trim_end_matches('/');
        let name = tag.split_whitespace().next().unwrap_or("");
        let matches = name.rsplit(':').next() == Some(local) && !name.is_empty();
        if !matches || self_closing {
            i = gt + 1;
            continue;
        }
        let close = format!("</{name}>");
        let rest = &haystack[gt + 1..];
        let Some(end_rel) = rest.find(&close) else {
            return None;
        };
        let text = rest[..end_rel].trim();
        if !text.is_empty() {
            return Some(text.to_string());
        }
        i = gt + 1;
    }
    None
}

/// Decode the entity one `<prefix:local>` text may carry — no XML crate
/// in the tree, and servers only ever emit the basic five + numeric refs.
fn xml_decode_entities(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(at) = rest.find('&') {
        out.push_str(&rest[..at]);
        let tail = &rest[at..];
        match tail.find(';') {
            Some(end) if end <= 10 && end >= 2 => match decode_entity(&tail[1..end]) {
                Some(c) => {
                    out.push(c);
                    rest = &tail[end + 1..];
                }
                None => {
                    out.push('&');
                    rest = &tail[1..];
                }
            },
            _ => {
                out.push('&');
                rest = &tail[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

fn decode_entity(ent: &str) -> Option<char> {
    match ent {
        "amp" => Some('&'),
        "lt" => Some('<'),
        "gt" => Some('>'),
        "quot" => Some('"'),
        "apos" => Some('\''),
        _ => {
            let n = ent.strip_prefix('#')?;
            let (radix, digits) = match n.strip_prefix(['x', 'X']) {
                Some(h) => (16, h),
                None => (10, n),
            };
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_hexdigit()) {
                return None;
            }
            u32::from_str_radix(digits, radix)
                .ok()
                .and_then(char::from_u32)
        }
    }
}

/// True when the block carries a `<prefix:collection` child (empty
/// `resourcetype` = plain file).
fn block_has_collection(block: &str) -> bool {
    let mut from = 0;
    while let Some(rel) = block[from..].find("collection") {
        let at = from + rel;
        let before = &block[..at];
        if let Some(lt) = before.rfind('<') {
            let tag = &before[lt + 1..];
            let looks_like_tag_name = !tag.is_empty()
                && !tag.contains('>')
                && !tag.contains(char::is_whitespace)
                && tag
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, ':' | '_' | '-' | '.'));
            if looks_like_tag_name {
                return true;
            }
        }
        from = at + "collection".len();
    }
    false
}

// ── entries + client ─────────────────────────────────────────────────

/// One child entry of a WebDAV directory listing (Depth:1 PROPFIND).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CloudEntry {
    /// Display name, or the href-decoded file name when the server omits
    /// `displayname`.
    pub name: String,
    /// Decoded absolute DAV path as returned by the server
    /// (e.g. `/remote.php/dav/files/user/Umfragen/x.pdf`).
    pub path: String,
    pub is_dir: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    /// HTTP-date Last-Modified as reported by the server.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mtime: Option<String>,
}

/// Rich file metadata extracted from WebDAV PROPFIND responses.
/// Mirrors the properties returned for each file/collection entry.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct CloudFileInfo {
    /// Display name, or the href-decoded file name when the server omits
    /// `displayname`.
    pub name: String,
    /// Decoded absolute DAV path as returned by the server
    /// (e.g. `/remote.php/dav/files/user/Umfragen/x.pdf`).
    pub path: String,
    /// Size in bytes for files; None for collections.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    /// HTTP-date Last-Modified as reported by the server.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_modified: Option<String>,
    /// True for collections (directories), false for files.
    pub is_directory: bool,
    /// MIME type from the `getcontenttype` property (e.g. `text/plain`).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mime_type: Option<String>,
}

/// Parse a Depth:1 PROPFIND multistatus body into a list of `CloudFileInfo`
/// entries with rich metadata. The request URI's own entry (the listed
/// collection itself) is skipped. Entries are sorted: collections first,
/// then by name (case-insensitive, then byte-wise).
pub fn parse_propfind(body: &[u8], request_path: &str) -> Result<Vec<CloudFileInfo>, String> {
    let xml = String::from_utf8_lossy(body).into_owned();
    if !xml.to_ascii_lowercase().contains("multistatus") {
        return Err(format!(
            "propfind response is not a WebDAV multistatus ({} bytes)",
            body.len()
        ));
    }
    let want = request_path.trim_end_matches('/');
    let mut entries = Vec::new();
    for block in xml_blocks(&xml, "response") {
        let Some(href) = xml_first_text(block, "href").map(|h| href_path(&h)) else {
            continue;
        };
        let path = href.trim_end_matches('/').to_string();
        if path == want {
            continue; // the collection itself (Depth:1 always echoes it)
        }
        let name = xml_first_text(block, "displayname")
            .map(|n| xml_decode_entities(&n))
            .filter(|n| !n.is_empty())
            .or_else(|| path.rsplit('/').next().map(str::to_string))
            .filter(|n| !n.is_empty());
        let Some(name) = name else { continue };
        let size =
            xml_first_text(block, "getcontentlength").and_then(|s| s.trim().parse::<u64>().ok());
        let last_modified = xml_first_text(block, "getlastmodified")
            .map(|s| xml_decode_entities(&s));
        let mime_type = xml_first_text(block, "getcontenttype");
        entries.push(CloudFileInfo {
            name,
            path,
            size,
            last_modified,
            is_directory: block_has_collection(block),
            mime_type,
        });
    }
    entries.sort_by(|a, b| {
        b.is_directory
            .cmp(&a.is_directory)
            .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
            .then_with(|| a.name.cmp(&b.name))
    });
    Ok(entries)
}

/// Parse a Depth:1 PROPFIND multistatus body into sorted `CloudEntry`s
/// (collections first, then by name). The request URI's own entry — the
/// listed collection itself — is skipped. This is the offline-testable
/// core of [`CloudFiles::list`].
pub fn list(body: &[u8], request_path: &str) -> Result<Vec<CloudEntry>, String> {
    let xml = String::from_utf8_lossy(body).into_owned();
    if !xml.to_ascii_lowercase().contains("multistatus") {
        return Err(format!(
            "propfind response is not a WebDAV multistatus ({} bytes)",
            body.len()
        ));
    }
    let want = request_path.trim_end_matches('/');
    let mut entries = Vec::new();
    for block in xml_blocks(&xml, "response") {
        let Some(href) = xml_first_text(block, "href").map(|h| href_path(&h)) else {
            continue;
        };
        let path = href.trim_end_matches('/').to_string();
        if path == want {
            continue; // the collection itself (Depth:1 always echoes it)
        }
        let name = xml_first_text(block, "displayname")
            .map(|n| xml_decode_entities(&n))
            .filter(|n| !n.is_empty())
            .or_else(|| path.rsplit('/').next().map(str::to_string))
            .filter(|n| !n.is_empty());
        let Some(name) = name else { continue };
        let size =
            xml_first_text(block, "getcontentlength").and_then(|s| s.trim().parse::<u64>().ok());
        let mtime = xml_first_text(block, "getlastmodified").map(|s| xml_decode_entities(&s));
        entries.push(CloudEntry {
            name,
            path,
            is_dir: block_has_collection(block),
            size,
            mtime,
        });
    }
    entries.sort_by(|a, b| {
        b.is_dir
            .cmp(&a.is_dir)
            .then_with(|| a.name.to_lowercase().cmp(&b.name.to_lowercase()))
            .then_with(|| a.name.cmp(&b.name))
    });
    Ok(entries)
}

/// A downloaded cloud file: raw bytes plus the server's Content-Type.
/// The download bridge falls back to `application/octet-stream` when
/// the cloud omits the header.
#[derive(Clone, Debug, PartialEq)]
pub struct CloudDownload {
    pub bytes: Vec<u8>,
    pub content_type: Option<String>,
}

/// OpenCloud WebDAV + OCS client. All remote paths are relative to
/// `dav_base`; the `http` client is injectable so callers can share the
/// portal's wired reqwest client (timeouts, egress proxy, no redirects).
#[derive(Clone)]
pub struct CloudFiles {
    /// WebDAV base, e.g. http://opensme-opencloud:9200/remote.php/dav/files/uploader
    pub dav_base: String,
    /// OCS shares endpoint; derived from dav_base when CLOUD_SHARE_URL unset.
    pub share_url: String,
    pub user: String,
    pub pass: String,
    pub http: reqwest::Client,
}

// `pass` must never leak through logs / panic messages.
impl fmt::Debug for CloudFiles {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CloudFiles")
            .field("dav_base", &self.dav_base)
            .field("share_url", &self.share_url)
            .field("user", &self.user)
            .field("pass", &"***")
            .field("http", &"reqwest::Client")
            .finish()
    }
}

impl CloudFiles {
    /// From CLOUD_WEBDAV_URL / CLOUD_WEBDAV_USER / CLOUD_WEBDAV_PASS
    /// (+ optional CLOUD_SHARE_URL or CLOUD_OCS_URL override for the OCS
    /// shares endpoint); None unless all three WebDAV vars are set and
    /// non-blank (same trim semantics as the original polls
    /// `CloudUploadConfig::from_env`). The OCS sharing API (public link
    /// creation + share listing) is reached through the derived/overridden
    /// OCS endpoint — see [`Self::ocs_share_create_public_link`] and
    /// [`Self::ocs_share_list`].
    pub fn from_env() -> Option<Self> {
        let dav_base = non_empty("CLOUD_WEBDAV_URL")?;
        let user = non_empty("CLOUD_WEBDAV_USER")?;
        let pass = non_empty("CLOUD_WEBDAV_PASS")?;
        Some(Self::from_parts(
            dav_base,
            user,
            pass,
            reqwest::Client::new(),
        ))
    }

    /// Explicit constructor (share endpoint resolved in this order:
    /// 1. `CLOUD_SHARE_URL` — full shares endpoint override (legacy),
    /// 2. `CLOUD_OCS_URL`   — OCS base; endpoint = `<ocs>{OCS_SHARE_API}/shares`,
    /// 3. derived from `dav_base`'s scheme://authority + `{OCS_SHARE_API}/shares`).
    pub fn from_parts(dav_base: String, user: String, pass: String, http: reqwest::Client) -> Self {
        let dav_base = dav_base.trim_end_matches('/').to_string();
        let share_url = resolve_share_url(
            &dav_base,
            non_empty("CLOUD_SHARE_URL").as_deref(),
            non_empty("CLOUD_OCS_URL").as_deref(),
        );
        Self {
            dav_base,
            share_url,
            user,
            pass,
            http,
        }
    }

    /// Get the user-specific DAV path for this CloudFiles instance.
    /// Returns the path component (without the base URL) for the configured user.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub fn user_dav_path(&self) -> String {
        get_user_dav_path(&self.user)
    }

    /// Build a user-specific CloudFiles instance from a username.
    /// Uses the same base URL and credentials, but constructs the DAV path
    /// for the specified user.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub fn for_user(&self, username: String) -> Self {
        // Extract base URL without the user path
        // CLOUD_WEBDAV_URL might be: http://opensme-opencloud:9200/remote.php/dav/files/uploader
        // We want: http://opensme-opencloud:9200 + /remote.php/dav/files/<new-user>
        let base_url = self.dav_base.trim_end_matches('/');
        let new_path = get_user_dav_path(&username);
        let new_dav_base = format!("{}{}", base_url, new_path);
        Self {
            dav_base: new_dav_base,
            share_url: self.share_url.clone(),
            user: username,
            pass: self.pass.clone(),
            http: self.http.clone(),
        }
    }

    /// Adopt the app's shared HTTP client so cloud traffic follows the
    /// same timeout / no-redirect / egress-proxy wiring as the rest of
    /// the portal.
    pub fn with_client(mut self, http: reqwest::Client) -> Self {
        self.http = http;
        self
    }

    /// PROPFIND (Depth:1) listing of `<dav_base>/<dir>`, sorted
    /// dirs-first then by name. An empty `dir` lists the base collection.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn list(&self, dir: &str) -> Result<Vec<CloudEntry>, String> {
        let dir = dir.trim();
        let url = if dir.is_empty() {
            Ok(self.dav_base.clone())
        } else {
            dav_url(&self.dav_base, dir)
        }?;
        let propfind = reqwest::Method::from_bytes(b"PROPFIND").expect("valid method");
        let res = self
            .http
            .request(propfind, &url)
            .header("Depth", "1")
            .header(reqwest::header::CONTENT_TYPE, "application/xml")
            .basic_auth(&self.user, Some(&self.pass))
            .body(
                "<?xml version=\"1.0\"?>\n\
                 <d:propfind xmlns:d=\"DAV:\"><d:prop>\
                 <d:displayname/><d:getcontentlength/><d:getlastmodified/><d:getcontenttype />\
                 <d:resourcetype/>\
                 </d:prop></d:propfind>",
            )
            .send()
            .await
            .map_err(|e| format!("PROPFIND {dir} unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "PROPFIND {dir} failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        let body = res
            .bytes()
            .await
            .map_err(|e| format!("PROPFIND {dir} response unreadable: {e}"))?;
        list(&body, &url_path(&url))
    }

    /// List files with rich metadata via PROPFIND. Returns CloudFileInfo
    /// entries with name, path, size, last_modified, is_directory, mime_type.
    /// Unlike list() which returns CloudEntry, this returns richer metadata.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn list_files(&self, dir: &str) -> Result<Vec<CloudFileInfo>, String> {
        let dir = dir.trim();
        let url = if dir.is_empty() {
            Ok(self.dav_base.clone())
        } else {
            dav_url(&self.dav_base, dir)
        }?;
        let propfind = reqwest::Method::from_bytes(b"PROPFIND").expect("valid method");
        let res = self
            .http
            .request(propfind, &url)
            .header("Depth", "1")
            .header(reqwest::header::CONTENT_TYPE, "application/xml")
            .basic_auth(&self.user, Some(&self.pass))
            .body(
                "<?xml version=\"1.0\"?>\n\
                 <d:propfind xmlns:d=\"DAV:\"><d:prop>\
                 <d:displayname/><d:getcontentlength/><d:getlastmodified/><d:getcontenttype />\
                 <d:resourcetype/>\
                 </d:prop></d:propfind>",
            )
            .send()
            .await
            .map_err(|e| format!("PROPFIND {dir} unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "PROPFIND {dir} failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        let body = res
            .bytes()
            .await
            .map_err(|e| format!("PROPFIND {dir} response unreadable: {e}"))?;
        parse_propfind(&body, &url_path(&url))
    }

    /// MKCOL `<dav_base>/<dir>`. RFC 4918: 405 means it already exists —
    /// callers commonly ignore the error for that case.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn mkdir(&self, dir: &str) -> Result<(), String> {
        let url = dav_url(&self.dav_base, dir)?;
        let mkcol = reqwest::Method::from_bytes(b"MKCOL").expect("valid method");
        let res = self
            .http
            .request(mkcol, &url)
            .basic_auth(&self.user, Some(&self.pass))
            .send()
            .await
            .map_err(|e| format!("MKCOL {dir} unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "MKCOL {dir} failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        Ok(())
    }

    /// Create a directory at `<dav_base>/<path>` using MKCOL.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn create_directory(&self, path: &str) -> Result<(), String> {
        let url = dav_url(&self.dav_base, path)?;
        let mkcol = reqwest::Method::from_bytes(b"MKCOL").expect("valid method");
        let res = self
            .http
            .request(mkcol, &url)
            .basic_auth(&self.user, Some(&self.pass))
            .send()
            .await
            .map_err(|e| format!("MKCOL {path} unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "MKCOL {path} failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        Ok(())
    }

    /// PUT bytes to `<dav_base>/<remote_path>`; non-2xx maps to `Err`
    /// with the operation and HTTP status.
    pub async fn put(
        &self,
        remote_path: &str,
        bytes: &[u8],
        content_type: &str,
    ) -> Result<(), String> {
        let url = dav_url(&self.dav_base, remote_path)?;
        let res = self
            .http
            .put(&url)
            .header(reqwest::header::CONTENT_TYPE, content_type)
            .basic_auth(&self.user, Some(&self.pass))
            .body(bytes.to_vec())
            .send()
            .await
            .map_err(|e| format!("PUT {remote_path} unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "PUT {remote_path} failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        Ok(())
    }

    /// Upload file with raw bytes. Simpler interface than put() which
    /// exposes content_type parameter. Uses application/octet-stream as default.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn upload_file(&self, remote_path: &str, bytes: &[u8]) -> Result<(), String> {
        self.put(remote_path, bytes, "application/octet-stream").await
    }

    /// GET `<dav_base>/<remote_path>` bytes.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn get(&self, remote_path: &str) -> Result<Vec<u8>, String> {
        Ok(self.download(remote_path).await?.bytes)
    }

    /// GET `<dav_base>/<remote_path>` — raw bytes plus the server's
    /// Content-Type (None when omitted; callers fall back to
    /// `application/octet-stream`). This is what the portal's
    /// `/api/cloud/file` download bridge serves.
    pub async fn download(&self, remote_path: &str) -> Result<CloudDownload, String> {
        let url = dav_url(&self.dav_base, remote_path)?;
        let res = self
            .http
            .get(&url)
            .basic_auth(&self.user, Some(&self.pass))
            .send()
            .await
            .map_err(|e| format!("GET {remote_path} unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "GET {remote_path} failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        let content_type = res
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty());
        let bytes = res
            .bytes()
            .await
            .map_err(|e| format!("GET {remote_path} response unreadable: {e}"))?;
        Ok(CloudDownload {
            bytes: bytes.to_vec(),
            content_type,
        })
    }

    /// Download file and return raw bytes. Simpler interface than download()
    /// which returns CloudDownload with content_type.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn download_file(&self, remote_path: &str) -> Result<Vec<u8>, String> {
        let file = self.download(remote_path).await?;
        Ok(file.bytes)
    }

    /// DELETE `<dav_base>/<remote_path>`.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn delete(&self, remote_path: &str) -> Result<(), String> {
        let url = dav_url(&self.dav_base, remote_path)?;
        let res = self
            .http
            .delete(&url)
            .basic_auth(&self.user, Some(&self.pass))
            .send()
            .await
            .map_err(|e| format!("DELETE {remote_path} unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "DELETE {remote_path} failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        Ok(())
    }

    /// Delete a file at `<dav_base>/<path>` using DELETE.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn delete_file(&self, path: &str) -> Result<(), String> {
        let url = dav_url(&self.dav_base, path)?;
        let res = self
            .http
            .delete(&url)
            .basic_auth(&self.user, Some(&self.pass))
            .send()
            .await
            .map_err(|e| format!("DELETE {path} unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "DELETE {path} failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        Ok(())
    }

    /// Mint a public read-only share link for a remote path (OCS v2
    /// share-create, public link, read-only — mirrors the original polls
    /// upload flow) and parse the share URL from the response.
    pub async fn create_share(&self, path: &str) -> Result<String, String> {
        // Reuse the URL builder purely as the traversal guard: a share
        // path must be a sane relative path under the base, too.
        dav_url(&self.dav_base, path)?;
        let form = [
            ("path", format!("/{path}")),
            ("shareType", "3".to_string()),   // public link
            ("permissions", "1".to_string()), // read only
        ];
        let res = self
            .http
            .post(&self.share_url)
            .header("OCS-APIRequest", "true")
            .basic_auth(&self.user, Some(&self.pass))
            .form(&form)
            .send()
            .await
            .map_err(|e| format!("share creation unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "share creation failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        let body = res
            .bytes()
            .await
            .map_err(|e| format!("share response unreadable: {e}"))?;
        parse_share_url(&body)
    }

    /// Mint a public read-only share link for a remote path through the
    /// OCS v2 shares API and return the parsed share URL. This is the
    /// typed counterpart to [`Self::create_share`]: same network call,
    /// but the response is decoded into an [`OcsResponse<ShareInfo>`]
    /// and the `data.url` is returned. The `path` is validated with the
    /// same traversal guard as WebDAV operations.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn ocs_share_create_public_link(&self, path: &str) -> Result<String, String> {
        // Reuse the WebDAV URL builder purely as the traversal guard: a
        // share path must be a sane relative path under the base, too.
        dav_url(&self.dav_base, path)?;
        let form = [
            ("path", format!("/{path}")),
            ("shareType", "3".to_string()),   // public link
            ("permissions", "1".to_string()), // read only
        ];
        let res = self
            .http
            .post(&self.share_url)
            .header("OCS-APIRequest", "true")
            .header(reqwest::header::ACCEPT, "application/json")
            .basic_auth(&self.user, Some(&self.pass))
            .form(&form)
            .send()
            .await
            .map_err(|e| format!("share creation unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "share creation failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        let body = res
            .bytes()
            .await
            .map_err(|e| format!("share response unreadable: {e}"))?;
        let parsed = parse_ocs_share_create(&body)?;
        Ok(parsed.ocs.data.url)
    }

    /// List the existing shares for a remote path through the OCS v2
    /// shares API (`GET <share_url>?path=/<path>`). Returns the typed
    /// [`ShareInfo`] records the server reports. The `path` is validated
    /// with the same traversal guard as WebDAV operations.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn ocs_share_list(&self, path: &str) -> Result<Vec<ShareInfo>, String> {
        dav_url(&self.dav_base, path)?;
        let res = self
            .http
            .get(&self.share_url)
            .header("OCS-APIRequest", "true")
            .header(reqwest::header::ACCEPT, "application/json")
            .basic_auth(&self.user, Some(&self.pass))
            .query(&[("path", format!("/{path}")), ("reshares", "true".to_string())])
            .send()
            .await
            .map_err(|e| format!("share list unreachable: {e}"))?;
        if !res.status().is_success() {
            return Err(format!(
                "share list failed: HTTP {}",
                res.status().as_u16()
            ));
        }
        let body = res
            .bytes()
            .await
            .map_err(|e| format!("share list response unreadable: {e}"))?;
        parse_ocs_share_list(&body)
    }

    /// Check if opencloud is reachable by GET /health/ready at the
    /// WebDAV base URL. Uses a dedicated client with a 5s timeout.
    /// If dav_base is empty, returns true trivially.
    pub async fn check_health(&self) -> bool {
        let dav_base = self.dav_base.trim_end_matches('/');
        if dav_base.is_empty() {
            return true;
        }
        // Build the health check URL: <dav_base>/health/ready
        let health_url = format!("{}/health/ready", dav_base);
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .redirect(reqwest::redirect::Policy::none())
            .build()
        {
            Ok(c) => c,
            Err(_) => return false,
        };
        let res = client
            .get(&health_url)
            .basic_auth(&self.user, Some(&self.pass))
            .send()
            .await;
        match res {
            Ok(res) => res.status().is_success(),
            Err(_) => false,
        }
    }

    /// Check CalDAV endpoint health using an OPTIONS request.
    /// The CalDAV endpoint is derived from the dav_base host as
    /// `<host>/remote.php/dav/calendars/<user>/`.
    /// Uses a dedicated client with a 5s timeout.
    /// If dav_base is empty, returns true trivially.
    pub async fn check_caldav_health(&self) -> bool {
        let dav_base = self.dav_base.trim_end_matches('/');
        if dav_base.is_empty() {
            return true;
        }
        // Extract the host part from dav_base
        let host = dav_base_host_base(dav_base);
        // Build the CalDAV principal URL: <host>/remote.php/dav/calendars/<user>/
        let caldav_url = format!("{}/remote.php/dav/calendars/{}/", host, self.user);
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .redirect(reqwest::redirect::Policy::none())
            .build()
        {
            Ok(c) => c,
            Err(_) => return false,
        };
        let res = client
            .request(reqwest::Method::OPTIONS, &caldav_url)
            .basic_auth(&self.user, Some(&self.pass))
            .send()
            .await;
        match res {
            Ok(res) => res.status().is_success(),
            Err(_) => false,
        }
    }

    /// Check CardDAV endpoint health using an OPTIONS request.
    /// The CardDAV endpoint is derived from the dav_base host as
    /// `<host>/remote.php/dav/addressbooks/<user>/`.
    /// Uses a dedicated client with a 5s timeout.
    /// If dav_base is empty, returns true trivially.
    pub async fn check_carddav_health(&self) -> bool {
        let dav_base = self.dav_base.trim_end_matches('/');
        if dav_base.is_empty() {
            return true;
        }
        // Extract the host part from dav_base
        let host = dav_base_host_base(dav_base);
        // Build the CardDAV principal URL: <host>/remote.php/dav/addressbooks/<user>/
        let carddav_url = format!("{}/remote.php/dav/addressbooks/{}/", host, self.user);
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .redirect(reqwest::redirect::Policy::none())
            .build()
        {
            Ok(c) => c,
            Err(_) => return false,
        };
        let res = client
            .request(reqwest::Method::OPTIONS, &carddav_url)
            .basic_auth(&self.user, Some(&self.pass))
            .send()
            .await;
        match res {
            Ok(res) => res.status().is_success(),
            Err(_) => false,
        }
    }
}

/// Standalone health check function that checks if opencloud is reachable.
/// GET /health/ready at CLOUD_WEBDAV_URL with a 5s timeout.
/// If CLOUD_WEBDAV_URL is empty, succeeds trivially.
pub async fn cloud_health() -> bool {
    let Some(cloud) = CloudFiles::from_env() else {
        // No config means no cloud service to check
        return true;
    };
    cloud.check_health().await
}

/// Handler for GET /api/cloud/health - returns JSON with healthy bool field.
pub async fn get_health() -> impl axum::response::IntoResponse {
    let healthy = cloud_health().await;
    (StatusCode::OK, Json(serde_json::json!({ "healthy": healthy })))
}

// ── offline tests (no network) ───────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    const BASE: &str = "http://opensme-opencloud:9200/remote.php/dav/files/umfragen-bot";

    // ── segment encoding ─────────────────────────────────────────────

    #[test]
    fn encode_segment_keeps_rfc3986_pchars() {
        assert_eq!(encode_segment("Antrag_2026.pdf"), "Antrag_2026.pdf");
        assert_eq!(encode_segment("a1-B_C.d~"), "a1-B_C.d~");
        // sub-delims + ':@' are legal inside a path segment
        assert_eq!(encode_segment("!(1)*+';,=&$:"), "!(1)*+';,=&$:");
        assert_eq!(encode_segment(""), "");
    }

    #[test]
    fn encode_segment_escapes_space_umlauts_hash_percent() {
        assert_eq!(encode_segment("Antrag 2026.pdf"), "Antrag%202026.pdf");
        // ä = U+00E4 → UTF-8 C3 A4
        assert_eq!(encode_segment("beschluss-ä"), "beschluss-%C3%A4");
        assert_eq!(encode_segment("f#1"), "f%231");
        assert_eq!(encode_segment("50%"), "50%25");
        assert_eq!(encode_segment("a/b"), "a%2Fb"); // never a separator
        assert_eq!(encode_segment("?q"), "%3Fq");
    }

    // ── URL building + traversal guards ──────────────────────────────

    #[test]
    fn dav_url_joins_and_encodes() {
        assert_eq!(
            dav_url(BASE, "Umfragen/abc123/tok_antrag.pdf").unwrap(),
            format!("{BASE}/Umfragen/abc123/tok_antrag.pdf")
        );
        assert_eq!(
            dav_url(BASE, "Umfragen/Antrag 1.pdf").unwrap(),
            format!("{BASE}/Umfragen/Antrag%201.pdf")
        );
        assert_eq!(
            dav_url(
                "https://cloud.example/remote.php/dav/files/uploader/",
                "x.pdf"
            )
            .unwrap(),
            "https://cloud.example/remote.php/dav/files/uploader/x.pdf"
        );
    }

    #[test]
    fn dav_url_rejects_traversal_slash_nonsense_and_empty() {
        for bad in [
            "..",
            "../..",
            "Umfragen/../etc/passwd",
            "./x",
            "a/./b",
            "/abs",
            "Umfragen/",
            "a//b",
            "",
            "   ",
        ] {
            let err = dav_url(BASE, bad).unwrap_err();
            assert!(
                err.contains("invalid cloud path"),
                "path {bad:?} must be rejected, got: {err}"
            );
        }
        assert!(dav_url(BASE, "..").unwrap_err().contains(".."));
        assert!(dav_url("", "x").is_err());
        // benign single/multi-segment paths stay fine
        assert!(dav_url(BASE, "Umfragen").is_ok());
        assert!(dav_url(BASE, "Umfragen/key").is_ok());
    }

    // ── bridge validation (the /api/cloud gate — pure, no network) ───

    #[test]
    fn validate_path_accepts_relative_multi_segment_paths() {
        assert_eq!(validate_path("Umfragen").unwrap(), "Umfragen");
        assert_eq!(validate_path(" Umfragen/2026 ").unwrap(), "Umfragen/2026");
        assert_eq!(
            validate_path("Anträge/Antrag 1.pdf").unwrap(),
            "Anträge/Antrag 1.pdf"
        );
    }

    #[test]
    fn validate_path_rejects_empty_absolute_traversal_and_slash_nonsense() {
        for bad in [
            "",
            "   ",
            "/abs",
            "/",
            "..",
            "../x",
            "a/../b",
            "./x",
            "a/./b",
            "Umfragen/",
            "a//b",
            ".",
        ] {
            let err = validate_path(bad).unwrap_err();
            assert!(
                err.starts_with("invalid cloud path"),
                "path {bad:?} must be rejected, got: {err}"
            );
        }
        // Callers must never supply absolute remote.php text.
        assert!(validate_path("/remote.php/dav/files/u/x.pdf").is_err());
        // absolute text is the ONLY reject with its own message
        assert!(validate_path("/x")
            .unwrap_err()
            .contains("must be relative"));
    }

    #[test]
    fn validate_name_is_exactly_one_sane_segment() {
        assert_eq!(validate_name("Antrag 2026.pdf").unwrap(), "Antrag 2026.pdf");
        assert_eq!(validate_name(" neu ").unwrap(), "neu");
        for bad in ["", "  ", "..", ".", "a/b", "a/b/c", " / "] {
            let err = validate_name(bad).unwrap_err();
            assert!(
                err.starts_with("invalid cloud name"),
                "name {bad:?} must be rejected, got: {err}"
            );
        }
        // a joined path is rejected as a NAME and caught as a PATH instead
        assert!(validate_name("Docs/neu")
            .unwrap_err()
            .contains("single path segment"));
    }

    #[test]
    fn join_prepends_the_directory_segment() {
        assert_eq!(join("Umfragen", "x.pdf"), "Umfragen/x.pdf");
        // joined results must survive both validators (round-trip)
        let full = join("Umfragen/2026", "Antrag 1.pdf");
        assert_eq!(validate_path(&full).unwrap(), full);
    }

    #[test]
    fn http_status_extracts_upstream_codes_only() {
        assert_eq!(http_status("MKCOL Docs/neu failed: HTTP 405"), Some(405));
        assert_eq!(http_status("GET x.pdf failed: HTTP 404"), Some(404));
        assert_eq!(http_status("PROPFIND Docs failed: HTTP 207"), Some(207));
        assert_eq!(
            http_status("PROPFIND Docs unreachable: connection refused"),
            None
        );
        assert_eq!(http_status("share response carries no url"), None);
        assert_eq!(http_status("HTTP "), None);
    }

    #[test]
    fn percent_decode_and_url_path_helpers() {
        assert_eq!(percent_decode("Antrag%201.pdf"), "Antrag 1.pdf");
        assert_eq!(percent_decode("%C3%A4"), "ä");
        assert_eq!(percent_decode("100%"), "100%"); // malformed → verbatim
        assert_eq!(
            url_path("http://opensme-opencloud:9200/remote.php/dav/files/u/Umfragen"),
            "/remote.php/dav/files/u/Umfragen"
        );
        assert_eq!(url_path("http://host"), "");
        assert_eq!(
            href_path("http://cloud.example/remote.php/dav/files/u/a%20b.pdf"),
            "/remote.php/dav/files/u/a b.pdf"
        );
        assert_eq!(href_path("/dav/files/u/x.pdf"), "/dav/files/u/x.pdf");
    }

    // ── PROPFIND parsing ─────────────────────────────────────────────

    fn nextcloud_propfind_body() -> Vec<u8> {
        br#"<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
 <d:response>
  <d:href>/remote.php/dav/files/umfragen-bot/Umfragen/</d:href>
  <d:propstat>
   <d:prop>
    <d:getlastmodified>Thu, 06 Nov 2025 09:30:22 GMT</d:getlastmodified>
    <d:resourcetype><d:collection/></d:resourcetype>
    <d:quota-used-bytes>17</d:quota-used-bytes>
   </d:prop>
   <d:status>HTTP/1.1 200 OK</d:status>
  </d:propstat>
 </d:response>
 <d:response>
  <d:href>/remote.php/dav/files/umfragen-bot/Umfragen/Beschluss%202026.pdf</d:href>
  <d:propstat>
   <d:prop>
    <d:getlastmodified>Thu, 06 Nov 2025 09:30:24 GMT</d:getlastmodified>
    <d:getcontentlength>4711</d:getcontentlength>
    <d:resourcetype/>
    <d:displayname>Beschluss &amp; Anlagen 2026.pdf</d:displayname>
   </d:prop>
   <d:status>HTTP/1.1 200 OK</d:status>
  </d:propstat>
 </d:response>
 <d:response>
  <d:href>/remote.php/dav/files/umfragen-bot/Umfragen/archiv/</d:href>
  <d:propstat>
   <d:prop>
    <d:getlastmodified>Thu, 06 Nov 2025 09:31:00 GMT</d:getlastmodified>
    <d:resourcetype><d:collection/></d:resourcetype>
   </d:prop>
   <d:status>HTTP/1.1 200 OK</d:status>
  </d:propstat>
 </d:response>
 <d:response>
  <d:href>/remote.php/dav/files/umfragen-bot/Umfragen/antrag.txt</d:href>
  <d:propstat>
   <d:prop>
    <d:getlastmodified>Thu, 06 Nov 2025 09:29:00 GMT</d:getlastmodified>
    <d:getcontentlength>12</d:getcontentlength>
   </d:prop>
   <d:status>HTTP/1.1 200 OK</d:status>
  </d:propstat>
 </d:response>
</d:multistatus>"#
            .to_vec()
    }

    #[test]
    fn propfind_parses_nextcloud_sample_dirs_first_and_skips_root() {
        let entries = list(
            &nextcloud_propfind_body(),
            "/remote.php/dav/files/umfragen-bot/Umfragen",
        )
        .unwrap();
        // root collection echoed by the server is skipped
        assert_eq!(entries.len(), 3, "root entry must be skipped: {entries:?}");
        // dirs first, then name (case-insensitive)
        assert!(entries[0].is_dir && entries[0].name == "archiv");
        assert_eq!(
            entries[0].path,
            "/remote.php/dav/files/umfragen-bot/Umfragen/archiv"
        );
        assert!(entries[0].size.is_none());
        assert_eq!(
            entries[0].mtime.as_deref(),
            Some("Thu, 06 Nov 2025 09:31:00 GMT")
        );
        assert_eq!(entries[1].name, "antrag.txt");
        assert_eq!(entries[1].size, Some(12));
        assert!(!entries[1].is_dir);
        // name prefers displayname (decoded entities) over the href
        assert_eq!(entries[2].name, "Beschluss & Anlagen 2026.pdf");
        assert_eq!(entries[2].size, Some(4711));
    }

    #[test]
    fn propfind_handles_unprefixed_tags_and_encoded_href_names() {
        let body = br#"<multistatus xmlns="DAV:">
<response><href>/dav/files/u/</href><propstat><prop><resourcetype><collection/></resourcetype></prop><status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/dav/files/u/Antrag%20zwei.pdf</href><propstat><prop><getcontentlength>3</getcontentlength></prop><status>HTTP/1.1 200 OK</status></propstat></response>
</multistatus>"#;
        let entries = list(body, "/dav/files/u").unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "Antrag zwei.pdf");
        assert_eq!(entries[0].size, Some(3));
        assert!(!entries[0].is_dir);
    }

    #[test]
    fn propfind_tolerates_full_url_hrefs_and_404_propstat_stubs() {
        let body = br#"<d:multistatus xmlns:d="DAV:">
<d:response><d:href>http://cloud.example/dav/files/u/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/dav/files/u/x.pdf</d:href>
 <d:propstat><d:prop><d:getcontentlength>9</d:getcontentlength></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 <d:propstat><d:prop><d:displayname/><d:getlastmodified/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
</d:response>
</d:multistatus>"#;
        let entries = list(body, "/dav/files/u").unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].name, "x.pdf");
        assert_eq!(entries[0].size, Some(9));
        assert!(
            entries[0].mtime.is_none(),
            "404-propstat stub must not count"
        );
    }

    #[test]
    fn propfind_rejects_non_multistatus_bodies() {
        let err = list(b"<html><body>403 Forbidden</body></html>", "/x").unwrap_err();
        assert!(err.contains("multistatus"), "got: {err}");
        assert!(list(b"", "/x").is_err());
    }

    // ── helpers ported from polls.rs ─────────────────────────────────

    #[test]
    fn dav_base_host_base_strips_paths() {
        assert_eq!(
            dav_base_host_base("https://cloud.opensme.graphwiz.ai/remote.php/dav/files/uploader/"),
            "https://cloud.opensme.graphwiz.ai"
        );
        assert_eq!(
            dav_base_host_base("http://cloud.local:8099/remote.php/dav/files/u"),
            "http://cloud.local:8099"
        );
        assert_eq!(dav_base_host_base("http://only-host"), "http://only-host");
    }

    #[test]
    fn parse_share_url_extracts_ocs_data_url() {
        let body = br#"{"ocs":{"meta":{"status":"ok"},"data":{"url":"https://cloud.opensme.graphwiz.ai/s/kJeYFw7BdAoLnHd"}}}"#;
        assert_eq!(
            parse_share_url(body).unwrap(),
            "https://cloud.opensme.graphwiz.ai/s/kJeYFw7BdAoLnHd"
        );
        assert!(parse_share_url(br#"{"ocs":{"data":{}}}"#).is_err());
        assert!(parse_share_url(b"<xml/>").is_err());
    }

    #[test]
    fn cloud_client_requires_all_three_vars() {
        // env vars leak between tests (process-global); only assert the
        // genuinely-safe direction: with vars unset-and-empty it is None.
        std::env::remove_var("CLOUD_WEBDAV_URL");
        std::env::remove_var("CLOUD_WEBDAV_USER");
        std::env::remove_var("CLOUD_WEBDAV_PASS");
        assert!(CloudFiles::from_env().is_none());
    }

    #[test]
    fn from_parts_trims_base_and_debug_redacts_password() {
        // These tests must not be polluted by host env overrides.
        std::env::remove_var("CLOUD_SHARE_URL");
        std::env::remove_var("CLOUD_OCS_URL");
        let cfg = CloudFiles::from_parts(
            format!("{BASE}/"),
            "bot".into(),
            "hunter2".into(),
            reqwest::Client::new(),
        );
        assert_eq!(cfg.dav_base, BASE);
        assert_eq!(cfg.user, "bot");
        // share endpoint derived from the WebDAV base host when neither
        // CLOUD_SHARE_URL nor CLOUD_OCS_URL is set.
        assert_eq!(
            cfg.share_url,
            "http://opensme-opencloud:9200/ocs/v2.php/apps/files_sharing/api/v1/shares"
        );
        let dbg = format!("{cfg:?}");
        assert!(dbg.contains("***"), "debug must redact: {dbg}");
        assert!(!dbg.contains("hunter2"), "password must never leak: {dbg}");
    }

    #[test]
    fn from_parts_honors_cloud_ocs_url_override() {
        // Pure precedence check — no env mutation, so no parallel-test races.
        assert_eq!(
            resolve_share_url(BASE, None, Some("http://cloud.example/ocs")),
            "http://cloud.example/ocs/ocs/v2.php/apps/files_sharing/api/v1/shares"
        );
        // trailing slash on the OCS base is trimmed
        assert_eq!(
            resolve_share_url(BASE, None, Some("http://cloud.example/ocs/")),
            "http://cloud.example/ocs/ocs/v2.php/apps/files_sharing/api/v1/shares"
        );
        // blank OCS override falls through to the dav-host derivation
        assert_eq!(
            resolve_share_url(BASE, None, Some("   ")),
            "http://opensme-opencloud:9200/ocs/v2.php/apps/files_sharing/api/v1/shares"
        );
    }

    #[test]
    fn from_parts_cloud_share_url_wins_over_ocs_url() {
        assert_eq!(
            resolve_share_url(
                BASE,
                Some("https://share.example/api/shares"),
                Some("http://ignored.example/ocs")
            ),
            "https://share.example/api/shares"
        );
        // neither override → derived from dav base host
        assert_eq!(
            resolve_share_url(BASE, None, None),
            "http://opensme-opencloud:9200/ocs/v2.php/apps/files_sharing/api/v1/shares"
        );
    }

    #[test]
    fn parse_ocs_share_create_decodes_typed_envelope() {
        let body = br#"{"ocs":{"meta":{"status":"ok","statuscode":200,"message":""},"data":{"id":42,"share_type":3,"path":"/Umfragen/x.pdf","url":"https://cloud.example/s/abc","token":"tok","permissions":1}}}"#;
        let parsed = parse_ocs_share_create(body).unwrap();
        assert_eq!(parsed.ocs.meta.status, "ok");
        assert_eq!(parsed.ocs.meta.statuscode, 200);
        assert_eq!(parsed.ocs.data.id, 42);
        assert_eq!(parsed.ocs.data.share_type, 3);
        assert_eq!(parsed.ocs.data.url, "https://cloud.example/s/abc");
        assert_eq!(parsed.ocs.data.token, "tok");
        assert_eq!(parsed.ocs.data.permissions, 1);
        // missing url → Err (parity with parse_share_url)
        assert!(parse_ocs_share_create(br#"{"ocs":{"data":{}}}"#).is_err());
        // non-json → Err
        assert!(parse_ocs_share_create(b"<xml/>").is_err());
    }

    #[test]
    fn parse_ocs_share_list_decodes_array_data() {
        let body = br#"{"ocs":{"meta":{"status":"ok","statuscode":200},"data":[{"id":1,"share_type":3,"path":"/Umfragen/a.pdf","url":"https://cloud.example/s/1","permissions":1},{"id":2,"share_type":0,"path":"/Umfragen/a.pdf","share_with":"alice","permissions":31}]}}"#;
        let shares = parse_ocs_share_list(body).unwrap();
        assert_eq!(shares.len(), 2);
        assert_eq!(shares[0].id, 1);
        assert_eq!(shares[0].share_type, 3);
        assert_eq!(shares[0].url, "https://cloud.example/s/1");
        assert_eq!(shares[1].id, 2);
        assert_eq!(shares[1].share_with, "alice");
        assert_eq!(shares[1].permissions, 31);
        // empty list
        assert_eq!(
            parse_ocs_share_list(br#"{"ocs":{"meta":{"status":"ok"},"data":[]}}"#).unwrap(),
            Vec::<ShareInfo>::new()
        );
        // non-json → Err
        assert!(parse_ocs_share_list(b"nope").is_err());
    }

    #[test]
    fn ocs_constants_are_the_documented_paths() {
        assert_eq!(OCS_API_V2, "/ocs/v2.php");
        assert_eq!(
            OCS_SHARE_API,
            "/ocs/v2.php/apps/files_sharing/api/v1"
        );
    }
}
