//! Shared WebDAV client library for openSME Rust services.
//!
//! This library provides a high-level, async WebDAV client for interacting
//! with WebDAV endpoints. It supports listing resources, uploading/downloading
//! files, creating directories, managing quotas, and creating shares.
//!
//! # Quick Start
//!
//! ```no_run
//! use opensme_webdav::{WebDavClient, WebDavClientConfig};
//!
//! async fn example() {
//!     // Create a client with authentication
//!     let config = WebDavClientConfig::new("https://webdav.example.com")
//!         .with_auth("username", "password");
//!     let client = WebDavClient::new(config).unwrap();
//!
//!     // List resources
//!     let resources = client.list("/").await.unwrap();
//!
//!     // Upload a file
//!     let content = b"Hello, WebDAV!";
//!     let response = client.upload("/test.txt", content, None).await.unwrap();
//!
//!     // Download a file
//!     let data = client.download("/test.txt", None).await.unwrap();
//!
//!     // Create a directory
//!     let _ = client.create_dir("/new_folder").await.unwrap();
//!
//!     // Get quota
//!     let quota = client.get_quota(None).await.unwrap();
//!
//!     // Create a share
//!     use opensme_webdav::SharePermission;
//!     let share = client.create_share(
//!         "/test.txt",
//!         Some("My Share"),
//!         vec![SharePermission::Read, SharePermission::Write],
//!         None,
//!         None,
//!     ).await.unwrap();
//! }
//! ```

pub mod client;
pub mod types;

// Re-export main types for convenience
pub use client::{WebDavClient, WebDavClientConfig};
pub use types::{
    DownloadOptions, ListOptions, QuotaInfo, Share, SharePermission, UploadOptions,
    WebDavError, WebDavResource, WebDavResult,
};

/// Version of the library
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_version() {
        assert!(!VERSION.is_empty());
    }
}
