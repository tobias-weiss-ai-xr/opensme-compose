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

use serde::{Deserialize, Serialize};
use std::fmt;

// ── env + OCS helpers (mirroring the original polls.rs seed) ─────────

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
    /// (+ optional CLOUD_SHARE_URL override); None unless all three are
    /// set and non-blank (same trim semantics as the original polls
    /// `CloudUploadConfig::from_env`).
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

    /// Explicit constructor (share endpoint derived from `dav_base`
    /// unless the CLOUD_SHARE_URL env override is set).
    pub fn from_parts(dav_base: String, user: String, pass: String, http: reqwest::Client) -> Self {
        let dav_base = dav_base.trim_end_matches('/').to_string();
        let share_url = std::env::var("CLOUD_SHARE_URL")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(|| {
                format!(
                    "{}/ocs/v2.php/apps/files_sharing/api/v1/shares",
                    dav_base_host_base(&dav_base)
                )
            });
        Self {
            dav_base,
            share_url,
            user,
            pass,
            http,
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
                 <d:displayname/><d:getcontentlength/><d:getlastmodified/>\
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

    /// GET `<dav_base>/<remote_path>` bytes.
    #[allow(dead_code)] // generalized client — consumers adopt incrementally
    pub async fn get(&self, remote_path: &str) -> Result<Vec<u8>, String> {
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
        let body = res
            .bytes()
            .await
            .map_err(|e| format!("GET {remote_path} response unreadable: {e}"))?;
        Ok(body.to_vec())
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
        let cfg = CloudFiles::from_parts(
            format!("{BASE}/"),
            "bot".into(),
            "hunter2".into(),
            reqwest::Client::new(),
        );
        assert_eq!(cfg.dav_base, BASE);
        assert_eq!(cfg.user, "bot");
        // share endpoint only overridden when CLOUD_SHARE_URL is set
        if std::env::var("CLOUD_SHARE_URL")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .is_none()
        {
            assert_eq!(
                cfg.share_url,
                "http://opensme-opencloud:9200/ocs/v2.php/apps/files_sharing/api/v1/shares"
            );
        }
        let dbg = format!("{cfg:?}");
        assert!(dbg.contains("***"), "debug must redact: {dbg}");
        assert!(!dbg.contains("hunter2"), "password must never leak: {dbg}");
    }
}
