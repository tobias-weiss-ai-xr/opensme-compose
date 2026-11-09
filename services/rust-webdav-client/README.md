# openSME WebDAV Client Library

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-only-blue.svg)](https://opensource.org/licenses/AGPL-3.0)

A shared WebDAV client library for openSME Rust services. This library provides a high-level, async interface for interacting with WebDAV endpoints.

## Features

- **Resource Listing** - List files and directories with metadata
- **File Upload/Download** - Upload and download files with various options
- **Directory Management** - Create directories on the server
- **Quota Management** - Check storage quotas
- **Share Management** - Create and manage shares
- **Error Handling** - Comprehensive error types for all operations
- **Async Support** - Built on async/await with tokio compatibility

## Quick Start

Add this to your `Cargo.toml`:

```toml
[dependencies]
opensme-webdav = { path = "../services/rust-webdav-client" }
tokio = { version = "1", features = ["full"] }
```

## Basic Usage

```rust
use opensme_webdav::{WebDavClient, WebDavClientConfig};
use opensme_webdav::{SharePermission, UploadOptions};

#[tokio::main]
async fn main() {
    // Create a client with authentication
    let config = WebDavClientConfig::new("https://webdav.example.com")
        .with_auth("username", "password")
        .with_timeout(std::time::Duration::from_secs(60));
    
    let client = WebDavClient::new(config).expect("Failed to create client");

    // List resources at the root
    let resources = client.list("/").await.expect("Failed to list resources");
    println!("Found {} resources", resources.len());

    // Upload a file
    let content = b"Hello, WebDAV!";
    let upload_options = UploadOptions {
        overwrite: true,
        content_type: Some("text/plain".to_string()),
        headers: None,
    };
    let response = client.upload("/test.txt", content, Some(upload_options))
        .await
        .expect("Failed to upload file");
    println!("Upload successful: {:?}", response.status());

    // Download a file
    let data = client.download("/test.txt", None)
        .await
        .expect("Failed to download file");
    println!("Downloaded {} bytes", data.len());

    // Create a directory
    let _ = client.create_dir("/new_folder")
        .await
        .expect("Failed to create directory");

    // Check if a resource exists
    let exists = client.exists("/test.txt").await.expect("Failed to check existence");
    println!("File exists: {}", exists);

    // Get metadata
    let metadata = client.get_metadata("/test.txt")
        .await
        .expect("Failed to get metadata");
    println!("File size: {} bytes", metadata.size);

    // Get quota information
    let quota = client.get_quota(None).await.expect("Failed to get quota");
    println!("Available: {} bytes, Used: {} bytes", quota.available, quota.used);

    // Create a share
    let share = client.create_share(
        "/test.txt",
        Some("My Shared File"),
        vec![SharePermission::Read, SharePermission::Write],
        Some("secret123"),
        None,
    ).await.expect("Failed to create share");
    println!("Share created: {} at {}", share.name, share.url);

    // Delete a resource
    let _ = client.delete("/test.txt").await.expect("Failed to delete file");
}
```

## Configuration

### Creating a Client

You can create a client in several ways:

**With authentication:**
```rust
let client = WebDavClient::with_auth(
    "https://webdav.example.com",
    Some("username"),
    Some("password"),
).expect("Failed to create client");
```

**Without authentication (for public endpoints):**
```rust
let config = WebDavClientConfig::new("https://webdav.example.com");
let client = WebDavClient::new(config).unwrap();
```

**With custom timeout:**
```rust
let config = WebDavClientConfig::new("https://webdav.example.com")
    .with_auth("username", "password")
    .with_timeout(std::time::Duration::from_secs(120));
let client = WebDavClient::new(config).unwrap();
```

## API Reference

### WebDavClient Methods

| Method | Description |
|--------|-------------|
| `list(path, options?)` | List resources at a path |
| `upload(path, content, options?)` | Upload a file |
| `upload_multipart(path, file_name, content, options?)` | Upload using multipart form data |
| `download(path, options?)` | Download a file |
| `delete(path)` | Delete a resource |
| `create_dir(path)` | Create a directory |
| `get_quota(path?)` | Get quota information |
| `create_share(path, name, permissions, password?, expires?)` | Create a share |
| `get_metadata(path)` | Get resource metadata |
| `exists(path)` | Check if a resource exists |

### Options

**UploadOptions:**
- `overwrite: bool` - Whether to overwrite existing files (default: false)
- `content_type: Option<String>` - Content-Type header
- `headers: Option<HashMap<String, String>>` - Custom headers

**DownloadOptions:**
- `range: Option<String>` - Range request (e.g., "bytes=0-999")
- `include_metadata: bool` - Include metadata in response (default: false)

**ListOptions:**
- `show_hidden: bool` - Show hidden files (default: false)
- `include_metadata: bool` - Include metadata (default: true)
- `limit: Option<usize>` - Maximum number of results
- `offset: Option<usize>` - Pagination offset

### Types

**WebDavResource:**
- `path: String` - Full path
- `name: String` - Resource name
- `is_dir: bool` - Whether it's a directory
- `size: u64` - Size in bytes
- `modified: Option<DateTime<Utc>>` - Last modified time
- `created: Option<DateTime<Utc>>` - Creation time
- `content_type: Option<String>` - MIME type
- `etag: Option<String>` - ETag for cache validation

**QuotaInfo:**
- `available: u64` - Available space in bytes
- `used: u64` - Used space in bytes
- `total: u64` - Total quota in bytes

**Share:**
- `id: String` - Share ID
- `name: String` - Share name
- `url: String` - Share URL
- `permissions: Vec<SharePermission>` - Permissions
- `expires: Option<DateTime<Utc>>` - Expiration date
- `password: Option<String>` - Password

**SharePermission:**
- `Read` - Read permission
- `Write` - Write permission
- `Delete` - Delete permission
- `Share` - Share permission
- `All` - All permissions

### Error Handling

All operations return a `WebDavResult<T>` which is an alias for `Result<T, WebDavError>`.

The `WebDavError` enum includes variants for:
- HTTP request failures
- HTTP errors (4xx/5xx)
- Invalid URLs
- File not found
- Permission denied
- Invalid response format
- Serialization errors
- Filesystem errors
- WebDAV-specific errors
- Authentication failures
- Timeout errors

Example error handling:
```rust
match client.download("/nonexistent.txt", None).await {
    Ok(data) => println!("Downloaded {} bytes", data.len()),
    Err(WebDavError::NotFound(path)) => eprintln!("File not found: {}", path),
    Err(WebDavError::PermissionDenied(path)) => eprintln!("Permission denied: {}", path),
    Err(e) => eprintln!("Error: {}", e),
}
```

## Testing

Run the tests with:

```bash
cd services/rust-webdav-client
cargo test
```

## Dependencies

- `reqwest` - HTTP client with JSON support
- `serde` / `serde_json` - Serialization
- `chrono` - Date/time handling
- `thiserror` - Error handling
- `url` - URL parsing
- `percent-encoding` - URL encoding
- `async-trait` - Async trait support
- `multipart` - Multipart form data support

## Compatibility

- Requires Rust 2021 edition or later
- Async runtime: tokio (via reqwest)
- WebDAV server: Compatible with most WebDAV implementations (Nextcloud, ownCloud, Generic WebDAV servers)

## License

This library is licensed under the AGPL-3.0-only license. See [LICENSE.md](../../LICENSE.md) for details.

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](../../CONTRIBUTING.md) for guidelines.
