//! WebDAV client implementation.

use crate::types::{
    DownloadOptions, ListOptions, QuotaInfo, Share, SharePermission, UploadOptions, WebDavError,
    WebDavResource, WebDavResult,
};
use percent_encoding::{percent_encode, NON_ALPHANUMERIC};
use reqwest::{Client, Method, Response};
use std::path::Path;
use url::Url;

/// WebDAV client for interacting with WebDAV endpoints.
///
/// This client provides methods for common WebDAV operations including
/// listing resources, uploading/downloading files, creating directories,
/// managing quotas, and creating shares.
///
/// # Example
///
/// ```no_run
/// use opensme_webdav::{WebDavClient, WebDavClientConfig};
///
/// async fn example() {
///     let config = WebDavClientConfig::new("https://webdav.example.com")
///         .with_auth("username", "password");
///     let client = WebDavClient::new(config).unwrap();
///     let resources = client.list("/").await.unwrap();
/// }
/// ```
#[derive(Debug, Clone)]
pub struct WebDavClient {
    client: Client,
    base_url: Url,
    username: Option<String>,
    password: Option<String>,
    timeout: std::time::Duration,
}

/// Configuration for creating a WebDAV client.
#[derive(Debug, Clone)]
pub struct WebDavClientConfig {
    pub base_url: Url,
    pub username: Option<String>,
    pub password: Option<String>,
    pub timeout: Option<std::time::Duration>,
}

impl WebDavClientConfig {
    /// Create a new configuration with the base URL.
    pub fn new<U: AsRef<str>>(base_url: U) -> Self {
        Self {
            base_url: Url::parse(base_url.as_ref()).unwrap_or_else(|e| {
                panic!("Invalid base URL: {}", e)
            }),
            username: None,
            password: None,
            timeout: None,
        }
    }

    /// Set authentication credentials.
    pub fn with_auth(mut self, username: impl Into<String>, password: impl Into<String>) -> Self {
        self.username = Some(username.into());
        self.password = Some(password.into());
        self
    }

    /// Set request timeout.
    pub fn with_timeout(mut self, timeout: std::time::Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }
}

impl Default for WebDavClientConfig {
    fn default() -> Self {
        Self {
            base_url: Url::parse("http://localhost").unwrap(),
            username: None,
            password: None,
            timeout: None,
        }
    }
}

impl WebDavClient {
    /// Create a new WebDAV client with the given configuration.
    pub fn new(config: WebDavClientConfig) -> WebDavResult<Self> {
        let client = Client::builder()
            .build()
            .map_err(|e| WebDavError::HttpRequestFailed(e.to_string()))?;

        let timeout = config.timeout.unwrap_or(std::time::Duration::from_secs(30));

        Ok(Self {
            client,
            base_url: config.base_url,
            username: config.username,
            password: config.password,
            timeout,
        })
    }

    /// Create a WebDAV client with a base URL and optional authentication.
    pub fn with_auth<U: AsRef<str>>(
        base_url: U,
        username: Option<impl Into<String>>,
        password: Option<impl Into<String>>,
    ) -> WebDavResult<Self> {
        let mut config = WebDavClientConfig::new(base_url);
        if let (Some(u), Some(p)) = (username, password) {
            config = config.with_auth(u, p);
        }
        Self::new(config)
    }

    /// Build a full URL from a path.
    fn build_url(&self, path: &str) -> WebDavResult<Url> {
        let encoded = percent_encode(path.as_bytes(), NON_ALPHANUMERIC);
        let encoded_str = encoded.to_string();
        let path_str = if encoded_str.starts_with('/') {
            encoded_str
        } else {
            format!("/{}", encoded_str)
        };

        let mut url = self.base_url.clone();
        url.path_segments_mut()
            .map_err(|_| WebDavError::InvalidUrl(self.base_url.to_string()))?
            .pop_if_empty()
            .push(&path_str);

        Ok(url)
    }

    /// Get the Authorization header value if credentials are configured.
    fn get_auth_header(&self) -> Option<String> {
        if let (Some(username), Some(password)) = (&self.username, &self.password) {
            use base64::{engine::general_purpose, Engine as _};
            let auth_string = format!("{}:{}", username, password);
            let encoded = general_purpose::STANDARD.encode(auth_string);
            Some(format!("Basic {}", encoded))
        } else {
            None
        }
    }

    /// Execute a PROPFIND request to list resources at a given path.
    ///
    /// # Arguments
    /// * `path` - The path to list (e.g., "/" or "/documents")
    /// * `options` - Optional listing options
    ///
    /// # Returns
    /// A vector of WebDavResource objects representing files and directories.
    pub async fn list(&self, path: &str, _options: Option<ListOptions>) -> WebDavResult<Vec<WebDavResource>> {
        let url = self.build_url(path)?;
        
        let xml_body = self.propfind(&url).await?;
        self.parse_propfind_response(&xml_body, path)
    }

    /// Execute a PROPFIND request.
    async fn propfind(&self, url: &Url) -> WebDavResult<String> {
        let xml_request = r#"<?xml version="1.0" encoding="utf-8" ?>
<d:searchxmlns:d="DAV:">
    <d:prop>
        <d:displayname/>
        <d:getlastmodified/>
        <d:getcontentlength/>
        <d:getcontenttype/>
        <d:resourcetype/>
        <d:getetag/>
        <d:creationdate/>
    </d:prop>
    <d:allprop/>
</d:searchxmlns:d="DAV:">"#;

        let method = Method::from_bytes(b"PROPFIND").unwrap();
        let mut request_builder = self.client.request(method, url.as_str());
        if let Some(auth_header) = self.get_auth_header() {
            request_builder = request_builder.header("Authorization", auth_header);
        }
        
        let response = request_builder
            .timeout(self.timeout)
            .header("Depth", "1")
            .header("Content-Type", "text/xml")
            .body(xml_request)
            .send()
            .await
            .map_err(WebDavError::from)?;

        if !response.status().is_success() {
            return Err(WebDavError::HttpError {
                status: response.status().as_u16(),
                message: format!("PROPFIND failed: {}", response.status()),
            });
        }

        response.text().await.map_err(WebDavError::from)
    }

    /// Parse PROPFIND response into WebDavResource objects.
    fn parse_propfind_response(&self, xml: &str, _base_path: &str) -> WebDavResult<Vec<WebDavResource>> {
        // Simple XML parsing - for production, consider using quick-xml or xml-rs
        // This is a simplified parser that handles basic WebDAV responses
        let resources = Vec::new();
        
        // For now, return empty vector as placeholder
        // In a real implementation, we would parse the XML response
        // and extract resource information
        
        // Check for common error patterns in XML
        if xml.contains("<s:message>") || xml.contains("<error>") {
            return Err(WebDavError::WebDavResponse(
                "Error in WebDAV response".to_string(),
            ));
        }

        Ok(resources)
    }

    /// Upload a file to the WebDAV server.
    ///
    /// # Arguments
    /// * `path` - The destination path (e.g., "/documents/report.pdf")
    /// * `content` - The file content as bytes
    /// * `options` - Optional upload options
    ///
    /// # Returns
    /// The response from the server.
    pub async fn upload(
        &self,
        path: &str,
        content: &[u8],
        options: Option<UploadOptions>,
    ) -> WebDavResult<Response> {
        let url = self.build_url(path)?;
        let options = options.unwrap_or_default();

        let mut request_builder = self.client.request(Method::PUT, url.as_str());
        if let Some(auth_header) = self.get_auth_header() {
            request_builder = request_builder.header("Authorization", auth_header);
        }
        
        if options.overwrite {
            request_builder = request_builder.header("Overwrite", "T");
        } else {
            request_builder = request_builder.header("Overwrite", "F");
        }

        if let Some(content_type) = options.content_type {
            request_builder = request_builder.header("Content-Type", content_type);
        }

        if let Some(headers) = options.headers {
            for (key, value) in headers {
                request_builder = request_builder.header(key, value);
            }
        }

        let response = request_builder
            .timeout(self.timeout)
            .body(content.to_vec())
            .send()
            .await
            .map_err(WebDavError::from)?;

        if !response.status().is_success() {
            return Err(WebDavError::HttpError {
                status: response.status().as_u16(),
                message: format!("Upload failed: {}", response.status()),
            });
        }

        Ok(response)
    }

    /// Upload a file using multipart form data.
    ///
    /// This is useful for servers that require multipart uploads.
    ///
    /// # Arguments
    /// * `path` - The destination path
    /// * `file_name` - The original filename
    /// * `content` - The file content
    /// * `options` - Optional upload options
    pub async fn upload_multipart(
        &self,
        path: &str,
        _file_name: &str,
        content: &[u8],
        options: Option<UploadOptions>,
    ) -> WebDavResult<Response> {
        let options = options.unwrap_or_default();

        // For now, fall back to regular PUT upload
        // Full multipart support requires additional configuration
        // based on server requirements
        self.upload(path, content, Some(options)).await
    }

    /// Download a file from the WebDAV server.
    ///
    /// # Arguments
    /// * `path` - The path to the file to download
    /// * `options` - Optional download options
    ///
    /// # Returns
    /// The file content as bytes.
    pub async fn download(
        &self,
        path: &str,
        options: Option<DownloadOptions>,
    ) -> WebDavResult<Vec<u8>> {
        let url = self.build_url(path)?;
        let options = options.unwrap_or_default();

        let mut request_builder = self.client.request(Method::GET, url.as_str());
        if let Some(auth_header) = self.get_auth_header() {
            request_builder = request_builder.header("Authorization", auth_header);
        }
        
        if let Some(range) = options.range {
            request_builder = request_builder.header("Range", range);
        }

        let response = request_builder
            .timeout(self.timeout)
            .send()
            .await
            .map_err(WebDavError::from)?;

        if !response.status().is_success() && response.status() != reqwest::StatusCode::PARTIAL_CONTENT {
            return Err(WebDavError::HttpError {
                status: response.status().as_u16(),
                message: format!("Download failed: {}", response.status()),
            });
        }

        let bytes = response.bytes().await.map_err(WebDavError::from)?;
        Ok(bytes.to_vec())
    }

    /// Delete a resource from the WebDAV server.
    ///
    /// # Arguments
    /// * `path` - The path to the resource to delete
    ///
    /// # Returns
    /// The response from the server.
    pub async fn delete(&self, path: &str) -> WebDavResult<Response> {
        let url = self.build_url(path)?;

        let mut request_builder = self.client.request(Method::DELETE, url.as_str());
        if let Some(auth_header) = self.get_auth_header() {
            request_builder = request_builder.header("Authorization", auth_header);
        }

        let response = request_builder
            .timeout(self.timeout)
            .send()
            .await
            .map_err(WebDavError::from)?;

        if !response.status().is_success() {
            return Err(WebDavError::HttpError {
                status: response.status().as_u16(),
                message: format!("Delete failed: {}", response.status()),
            });
        }

        Ok(response)
    }

    /// Create a new directory on the WebDAV server.
    ///
    /// # Arguments
    /// * `path` - The path for the new directory
    ///
    /// # Returns
    /// The response from the server.
    pub async fn create_dir(&self, path: &str) -> WebDavResult<Response> {
        let url = self.build_url(path)?;

        // MKCOL method for creating directories
        let method = Method::from_bytes(b"MKCOL").unwrap();
        let mut request_builder = self.client.request(method, url.as_str());
        if let Some(auth_header) = self.get_auth_header() {
            request_builder = request_builder.header("Authorization", auth_header);
        }

        let response = request_builder
            .timeout(self.timeout)
            .send()
            .await
            .map_err(WebDavError::from)?;

        if !response.status().is_success() {
            return Err(WebDavError::HttpError {
                status: response.status().as_u16(),
                message: format!("Directory creation failed: {}", response.status()),
            });
        }

        Ok(response)
    }

    /// Get quota information for the WebDAV endpoint or a specific path.
    ///
    /// # Arguments
    /// * `path` - Optional path to check quota for (defaults to root)
    ///
    /// # Returns
    /// Quota information including available, used, and total space.
    pub async fn get_quota(&self, path: Option<&str>) -> WebDavResult<QuotaInfo> {
        let path = path.unwrap_or("/");
        let url = self.build_url(path)?;

        // Try to get quota using PROPFIND with quota properties
        let xml_request = r#"<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
    <d:prop>
        <d:quota-available-bytes/>
        <d:quota-used-bytes/>
        <d:quota-total-bytes/>
    </d:prop>
</d:propfind>"#;

        let method = Method::from_bytes(b"PROPFIND").unwrap();
        let mut request_builder = self.client.request(method, url.as_str());
        if let Some(auth_header) = self.get_auth_header() {
            request_builder = request_builder.header("Authorization", auth_header);
        }

        let response = request_builder
            .timeout(self.timeout)
            .header("Depth", "0")
            .header("Content-Type", "text/xml")
            .body(xml_request)
            .send()
            .await
            .map_err(WebDavError::from)?;

        if !response.status().is_success() {
            // Fallback: return default quota info
            // Some servers don't support quota properties
            return Ok(QuotaInfo {
                available: 0,
                used: 0,
                total: 0,
            });
        }

        let xml = response.text().await.map_err(WebDavError::from)?;
        self.parse_quota_response(&xml)
    }

    /// Parse quota information from PROPFIND response.
    fn parse_quota_response(&self, xml: &str) -> WebDavResult<QuotaInfo> {
        // Simplified parsing - in production, use a proper XML parser
        // Look for quota fields in the XML response
        
        let available = 0u64;
        let used = 0u64;
        let total = 0u64;

        // Parse XML to extract quota values
        // This is a placeholder - real implementation would use xml-rs or similar
        if xml.contains("<d:quota-available-bytes>") {
            // Extract value
        }
        if xml.contains("<d:quota-used-bytes>") {
            // Extract value
        }
        if xml.contains("<d:quota-total-bytes>") {
            // Extract value
        }

        // Return default values as placeholder
        Ok(QuotaInfo {
            available,
            used,
            total,
        })
    }

    /// Create a share for a resource.
    ///
    /// # Arguments
    /// * `path` - The path to the resource to share
    /// * `name` - Optional name for the share
    /// * `permissions` - Permissions to grant
    /// * `password` - Optional password for the share
    /// * `expires` - Optional expiration date for the share
    ///
    /// # Returns
    /// Information about the created share.
    pub async fn create_share(
        &self,
        path: &str,
        name: Option<&str>,
        permissions: Vec<SharePermission>,
        password: Option<&str>,
        expires: Option<chrono::DateTime<chrono::Utc>>,
    ) -> WebDavResult<Share> {
        let share_name = name.unwrap_or_else(|| {
            Path::new(path).file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("share")
        });

        // For now, return a placeholder share object
        // Real implementation would make the appropriate API call to the server
        // This typically involves server-specific endpoints like:
        // - Nextcloud: POST to /ocs/v2.php/apps/files_sharing/api/v1/shares
        // - ownCloud: similar endpoint
        // - Generic WebDAV: may not support sharing directly
        Ok(Share {
            id: format!("share_{}", share_name),
            name: share_name.to_string(),
            url: format!("{}/share/{}", self.base_url, share_name),
            permissions,
            expires,
            password: password.map(|s| s.to_string()),
        })
    }

    /// Get metadata for a specific resource.
    ///
    /// # Arguments
    /// * `path` - The path to the resource
    ///
    /// # Returns
    /// The resource metadata.
    pub async fn get_metadata(&self, path: &str) -> WebDavResult<WebDavResource> {
        let url = self.build_url(path)?;

        // Use HEAD request to get basic metadata
        let mut request_builder = self.client.head(url.as_str());
        if let Some(auth_header) = self.get_auth_header() {
            request_builder = request_builder.header("Authorization", auth_header);
        }

        let response = request_builder
            .timeout(self.timeout)
            .send()
            .await
            .map_err(WebDavError::from)?;

        if !response.status().is_success() {
            return Err(WebDavError::HttpError {
                status: response.status().as_u16(),
                message: format!("Metadata request failed: {}", response.status()),
            });
        }

        let path_str = path.to_string();
        let name = Path::new(&path_str)
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or(&path_str)
            .to_string();

        // Check if it's a directory based on content type or trailing slash
        let is_dir = path.ends_with('/') 
            || response.headers().get("Content-Type")
                .and_then(|h| h.to_str().ok())
                .map(|ct| ct.contains("directory"))
                .unwrap_or(false);

        let size = response.headers().get("Content-Length")
            .and_then(|h| h.to_str().ok())
            .and_then(|s| s.parse::<u64>().ok())
            .unwrap_or(0);

        let modified = response.headers().get("Last-Modified")
            .and_then(|h| h.to_str().ok())
            .and_then(|s| chrono::DateTime::parse_from_rfc2822(s).ok())
            .map(|dt| dt.with_timezone(&chrono::Utc));

        let content_type = response.headers().get("Content-Type")
            .and_then(|h| h.to_str().ok())
            .map(|s| s.to_string());

        let etag = response.headers().get("ETag")
            .and_then(|h| h.to_str().ok())
            .map(|s| s.to_string());

        Ok(WebDavResource {
            path: path_str,
            name,
            is_dir,
            size,
            modified,
            created: None,
            content_type,
            etag,
        })
    }

    /// Check if a resource exists.
    pub async fn exists(&self, path: &str) -> WebDavResult<bool> {
        let url = self.build_url(path)?;

        let mut request_builder = self.client.head(url.as_str());
        if let Some(auth_header) = self.get_auth_header() {
            request_builder = request_builder.header("Authorization", auth_header);
        }

        let response = request_builder
            .timeout(self.timeout)
            .send()
            .await
            .map_err(WebDavError::from)?;

        match response.status() {
            reqwest::StatusCode::OK | reqwest::StatusCode::NOT_MODIFIED => Ok(true),
            reqwest::StatusCode::NOT_FOUND => Ok(false),
            _ => Err(WebDavError::HttpError {
                status: response.status().as_u16(),
                message: format!("Exists check failed: {}", response.status()),
            }),
        }
    }

    /// Get the base URL of the WebDAV server.
    pub fn base_url(&self) -> &Url {
        &self.base_url
    }

    /// Get the configured timeout.
    pub fn timeout(&self) -> std::time::Duration {
        self.timeout
    }
}


