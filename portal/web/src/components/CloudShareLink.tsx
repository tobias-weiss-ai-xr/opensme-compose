"use client";

import { useState, useCallback } from "react";

/**
 * Props for CloudShareLink component
 */
export interface CloudShareLinkProps {
  /** The share URL to display (required) */
  shareUrl: string;
  /** Title for the share link section (optional) */
  title?: string;
  /** Callback when revoke button is clicked (optional) */
  onRevoke?: () => void | Promise<void>;
  /** Display mode: 'compact' or 'expanded' (optional) */
  mode?: 'compact' | 'expanded';
}

/**
 * Extract expiration date from WebDAV share URL if present.
 * looks for expiry parameter or expiration timestamp in the URL.
 */
function extractExpirationDate(url: string): Date | null {
  try {
    const urlObj = new URL(url);
    
    // Check for expiry parameter (common in share links)
    const expiryParam = urlObj.searchParams.get('expiry') || 
                       urlObj.searchParams.get('expiration') ||
                       urlObj.searchParams.get('expires');
    
    if (expiryParam) {
      const timestamp = Date.parse(expiryParam);
      if (!isNaN(timestamp)) {
        return new Date(timestamp);
      }
    }
    
    // Check path segments for date patterns
    const pathSegments = urlObj.pathname.split('/');
    for (const segment of pathSegments) {
      // Try to parse as ISO date or timestamp
      const timestamp = Date.parse(segment);
      if (!isNaN(timestamp)) {
        return new Date(timestamp);
      }
    }
    
    return null;
  } catch {
    return null;
  }
}

/**
 * React component for displaying OpenCloud share links (shareLink).
 * Features:
 * - Display clickable share link
 * - Copy-to-clipboard button
 * - Show expiration date if present in URL
 * - Optional revoke button
 * - Compact and expanded display modes
 * - Proper styling with Tailwind CSS matching portal theme
 */
export default function CloudShareLink({
  shareUrl,
  title = 'Share Link',
  onRevoke,
  mode = 'expanded',
}: CloudShareLinkProps) {
  const [copied, setCopied] = useState<boolean>(false);
  const [isRevoking, setIsRevoking] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  
  const expirationDate = extractExpirationDate(shareUrl);
  
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError('Failed to copy link to clipboard');
      setTimeout(() => setError(null), 3000);
    }
  }, [shareUrl]);
  
  const handleRevoke = useCallback(async () => {
    if (!onRevoke) return;
    
    setIsRevoking(true);
    setError(null);
    
    try {
      await onRevoke();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke share link');
      setTimeout(() => setError(null), 5000);
    } finally {
      setIsRevoking(false);
    }
  }, [onRevoke]);
  
  // Format expiration date for display
  const formatExpiration = useCallback(() => {
    if (!expirationDate) return null;
    
    return expirationDate.toLocaleString();
  }, [expirationDate]);
  
  // Check if link is expired
  const isExpired = !!(expirationDate && expirationDate <= new Date());
  
  // Compact mode renders a simple inline link with copy button
  if (mode === 'compact') {
    return (
      <div className="share-link share-link-compact flex items-center gap-2">
        <a
          href={shareUrl}
          className="share-link-url text-color-accent hover:text-color-accent-hover text-sm truncate underline"
          target="_blank"
          rel="noopener noreferrer"
          title={shareUrl}
        >
          {title}
        </a>
        <button
          className="share-link-copy btn btn-secondary text-xs px-2 py-1"
          onClick={handleCopy}
          title="Copy to clipboard"
          disabled={isExpired}
        >
          {copied ? '✓' : 'Copy'}
        </button>
        {isExpired && (
          <span className="share-link-expired text-color-danger text-xs">
            (Expired)
          </span>
        )}
      </div>
    );
  }
  
  // Expanded mode renders a full card with all features
  return (
    <div className="share-link share-link-expanded bg-color-card border border-color-border rounded-lg p-4">
      {/* Title */}
      {title && (
        <h4 className="share-link-title text-color-foreground font-semibold mb-3">
          {title}
        </h4>
      )}
      
      {/* Error display */}
      {error && (
        <div className="share-link-error mb-3 p-2 bg-color-danger bg-opacity-10 border border-color-danger rounded text-color-danger text-sm">
          {error}
        </div>
      )}
      
      {/* Link display */}
      <div className="share-link-container flex flex-col sm:flex-row gap-3 mb-3">
        <a
          href={shareUrl}
          className="share-link-url flex-1 text-color-accent hover:text-color-accent-hover break-all text-sm sm:text-base underline"
          target="_blank"
          rel="noopener noreferrer"
        >
          {shareUrl}
        </a>
        <button
          className="share-link-copy btn btn-secondary px-4 py-2 text-sm whitespace-nowrap"
          onClick={handleCopy}
          disabled={isExpired}
        >
          {copied ? '✓ Copied!' : 'Copy Link'}
        </button>
      </div>
      
      {/* Expiration info */}
      {expirationDate && (
        <div className="share-link-expiry mb-3 text-sm">
          <span className="text-color-dim">Expires: </span>
          <span className={isExpired ? 'text-color-danger line-through' : 'text-color-foreground'}>
            {formatExpiration()}
          </span>
          {isExpired && (
            <span className="ml-2 text-color-danger font-medium">
              (Expired)
            </span>
          )}
        </div>
      )}
      
      {/* Revoke button */}
      {onRevoke && !isExpired && (
        <button
          className="share-link-revoke btn btn-danger px-4 py-2 text-sm"
          onClick={handleRevoke}
          disabled={isRevoking}
        >
          {isRevoking ? 'Revoking...' : 'Revoke Share Link'}
        </button>
      )}
      
      {/* Expired state message */}
      {isExpired && !onRevoke && (
        <div className="share-link-expired-message text-color-danger text-sm mt-2">
          This share link has expired and is no longer accessible.
        </div>
      )}
    </div>
  );
}
