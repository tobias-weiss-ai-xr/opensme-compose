//! Types and error definitions for the WebDAV client library.

use serde::{Deserialize, Serialize};
use std::fmt::Display;
use thiserror::Error;

/// Error types for WebDAV operations
#[derive(Debug, Error)]
pub enum WebDavError {
    /// HTTP request failed
    #[error("HTTP request failed: {0}")]
    HttpRequestFailed(String),

    /// HTTP response error (4xx/5xx)
    #[error("HTTP error ({status}): {message}")]
    HttpError {
        status: u16,
        message: String,
    },

    /// Invalid URL
    #[error("Invalid URL: {0}")]
    InvalidUrl(String),

    /// File not found
    #[error("File not found: {0}")]
    NotFound(String),

    /// Permission denied
    #[error("Permission denied: {0}")]
    PermissionDenied(String),

    /// Invalid response format
    #[error("Invalid response format: {0}")]
    InvalidResponse(String),

    /// Serialization/deserialization error
    #[error("Serialization error: {0}")]
    SerializationError(String),

    /// File system error
    #[error("Filesystem error: {0}")]
    IoError(String),

    /// WebDAV-specific error (from XML response)
    #[error("WebDAV error: {0}")]
    WebDavResponse(String),

    /// Authentication failed
    #[error("Authentication failed")]
    AuthenticationFailed,

    /// Connection timeout
    #[error("Connection timeout")]
    Timeout,
}

impl From<std::io::Error> for WebDavError {
    fn from(err: std::io::Error) -> Self {
        WebDavError::IoError(err.to_string())
    }
}

impl From<reqwest::Error> for WebDavError {
    fn from(err: reqwest::Error) -> Self {
        if err.is_timeout() {
            WebDavError::Timeout
        } else if err.is_status() {
            let status = err.status().map(|s| s.as_u16()).unwrap_or(0);
            WebDavError::HttpError {
                status,
                message: err.to_string(),
            }
        } else {
            WebDavError::HttpRequestFailed(err.to_string())
        }
    }
}

impl From<url::ParseError> for WebDavError {
    fn from(err: url::ParseError) -> Self {
        WebDavError::InvalidUrl(err.to_string())
    }
}

impl From<serde_json::Error> for WebDavError {
    fn from(err: serde_json::Error) -> Self {
        WebDavError::SerializationError(err.to_string())
    }
}

/// Represents a WebDAV resource (file or directory)
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WebDavResource {
    /// Full path/URL of the resource
    pub path: String,
    /// Resource name (filename or directory name)
    pub name: String,
    /// Whether this is a directory
    pub is_dir: bool,
    /// Size in bytes (0 for directories)
    pub size: u64,
    /// Last modification time
    pub modified: Option<chrono::DateTime<chrono::Utc>>,
    /// Creation time
    pub created: Option<chrono::DateTime<chrono::Utc>>,
    /// MIME type
    pub content_type: Option<String>,
    /// ETag for cache validation
    pub etag: Option<String>,
}

/// Quota information for a WebDAV endpoint
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuotaInfo {
    /// Total available space in bytes
    pub available: u64,
    /// Used space in bytes
    pub used: u64,
    /// Total quota in bytes
    pub total: u64,
}

/// Share information for a shared WebDAV resource
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Share {
    /// Share ID
    pub id: String,
    /// Share name
    pub name: String,
    /// Share URL
    pub url: String,
    /// Permissions (read, write, etc.)
    pub permissions: Vec<SharePermission>,
    /// Expiration date
    pub expires: Option<chrono::DateTime<chrono::Utc>>,
    /// Share password (if applicable)
    pub password: Option<String>,
}

/// Share permissions
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub enum SharePermission {
    Read,
    Write,
    Delete,
    Share,
    All,
}

impl Display for SharePermission {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SharePermission::Read => write!(f, "read"),
            SharePermission::Write => write!(f, "write"),
            SharePermission::Delete => write!(f, "delete"),
            SharePermission::Share => write!(f, "share"),
            SharePermission::All => write!(f, "all"),
        }
    }
}

/// Upload options for file uploads
#[derive(Debug, Clone)]
pub struct UploadOptions {
    /// Whether to overwrite existing files
    pub overwrite: bool,
    /// Content type to set
    pub content_type: Option<String>,
    /// Custom headers to include
    pub headers: Option<std::collections::HashMap<String, String>>,
}

impl Default for UploadOptions {
    fn default() -> Self {
        Self {
            overwrite: false,
            content_type: None,
            headers: None,
        }
    }
}

/// Download options for file downloads
#[derive(Debug, Clone)]
pub struct DownloadOptions {
    /// Range request (bytes= start-end)
    pub range: Option<String>,
    /// Whether to include metadata
    pub include_metadata: bool,
}

impl Default for DownloadOptions {
    fn default() -> Self {
        Self {
            range: None,
            include_metadata: false,
        }
    }
}

/// List options for directory listings
#[derive(Debug, Clone)]
pub struct ListOptions {
    /// Show hidden files
    pub show_hidden: bool,
    /// Include metadata in response
    pub include_metadata: bool,
    /// Maximum number of results
    pub limit: Option<usize>,
    /// Offset for pagination
    pub offset: Option<usize>,
}

impl Default for ListOptions {
    fn default() -> Self {
        Self {
            show_hidden: false,
            include_metadata: true,
            limit: None,
            offset: None,
        }
    }
}

/// Result type for WebDAV operations
pub type WebDavResult<T> = Result<T, WebDavError>;
