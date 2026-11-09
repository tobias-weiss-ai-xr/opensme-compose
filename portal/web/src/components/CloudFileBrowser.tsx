"use client";

import { useState, useEffect, useCallback } from "react";

/**
 * File entry from the OpenCloud API
 */
export interface CloudFile {
  name: string;
  path: string;
  size: number | null;
  is_dir: boolean;
  modified_at: string | null;
  type: "file" | "dir" | "symlink";
}

/**
 * Response from /api/cloud/files
 */
export interface CloudFilesResponse {
  files: CloudFile[];
  path: string;
}

/**
 * API response wrapper for directory listing
 */
export interface ListFilesResponse {
  files: CloudFile[];
  path: string;
}

/**
 * Props for CloudFileBrowser component
 */
export interface CloudFileBrowserProps {
  /** Current directory path, defaults to '/' */
  path?: string;
  /** Callback when a file/directory is selected */
  onSelect?: (item: CloudFile) => void;
  /** Callback when upload is triggered */
  onUpload?: (files: FileList) => void | Promise<void>;
  /** Callback when a new directory is created */
  onCreateDir?: (name: string) => void | Promise<void>;
  /** Callback when delete is triggered for selected items */
  onDelete?: (items: CloudFile[]) => void | Promise<void>;
}

/**
 * Helper to list files from the cloud API
 */
async function list_files(path: string): Promise<ListFilesResponse> {
  const query = new URLSearchParams({ path });
  const res = await fetch(`/api/cloud/files?${query.toString()}`, {
    cache: "no-store",
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} ${path}`);
  }
  return res.json() as Promise<ListFilesResponse>;
}

/**
 * React component for browsing OpenCloud files via portal API.
 * Features:
 * - List files from /api/cloud/files?path={path}
 * - Display as table with columns: Name, Size, Last Modified, Type
 * - Multi-select with checkboxes
 * - Breadcrumb navigation for path
 * - Upload button that triggers onUpload
 * - New folder button that shows dialog
 * - Delete button for selected items
 * - Uses arrow-up icon for parent directory
 */
export default function CloudFileBrowser({
  path: initialPath = "/",
  onSelect,
  onUpload,
  onCreateDir,
  onDelete,
}: CloudFileBrowserProps) {
  const [files, setFiles] = useState<CloudFile[]>([]);
  const [currentPath, setCurrentPath] = useState<string>(initialPath);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [showNewFolder, setShowNewFolder] = useState<boolean>(false);
  const [newFolderName, setNewFolderName] = useState<string>("");
  const [fileInputKey, setFileInputKey] = useState<number>(0);

  // Derive parent path for breadcrumb navigation
  const getParentPath = useCallback((p: string): string | null => {
    if (p === "/") return null;
    const parts = p.split("/").filter(Boolean);
    if (parts.length === 1) return "/";
    return "/" + parts.slice(0, -1).join("/") + "/";
  }, []);

  // Derive breadcrumbs from path
  const getBreadcrumbs = useCallback((p: string): { name: string; path: string }[] => {
    const parts = p.split("/").filter(Boolean);
    const crumbs: { name: string; path: string }[] = [
      { name: "root", path: "/" },
    ];
    let built = "";
    for (const part of parts) {
      built += "/" + part;
      crumbs.push({ name: part, path: built });
    }
    return crumbs;
  }, []);

  // Parse path to get directory name
  const getDirName = useCallback((p: string): string => {
    if (p === "/") return "root";
    const parts = p.split("/").filter(Boolean);
    return parts[parts.length - 1] || "root";
  }, []);

  // Load files for current path
  const loadFiles = useCallback(async (p: string) => {
    setLoading(true);
    setError(null);
    try {
      const response = await list_files(p);
      setFiles(response.files);
      setCurrentPath(response.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setFiles([]);
    } finally {
      setLoading(false);
    }
  }, []);

  // Navigate to a different path
  const navigateTo = useCallback((p: string) => {
    setSelected(new Set());
    void loadFiles(p);
  }, [loadFiles]);

  // Navigate to parent directory
  const navigateUp = useCallback(() => {
    const parent = getParentPath(currentPath);
    if (parent !== null) {
      navigateTo(parent);
    }
  }, [currentPath, getParentPath, navigateTo]);

  // Handle file selection
  const handleSelectItem = useCallback((item: CloudFile) => {
    if (onSelect) {
      onSelect(item);
    }
    // If it's a directory, navigate into it
    if (item.is_dir) {
      const newPath = item.path.endsWith("/") ? item.path : item.path + "/";
      navigateTo(newPath);
    }
  }, [navigateTo, onSelect]);

  // Toggle selection for multi-select
  const toggleSelect = useCallback((path: string) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }, []);

  // Select all items
  const selectAll = useCallback(() => {
    if (files.length === 0) return;
    if (selected.size === files.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(files.map(f => f.path)));
    }
  }, [files, selected.size]);

  // Get selected files
  const selectedFiles = useCallback((): CloudFile[] => {
    return files.filter(f => selected.has(f.path));
  }, [files, selected]);

  // Handle upload button click
  const handleUploadClick = useCallback(() => {
    // Trigger file input
    const input = document.getElementById("cloud-file-upload") as HTMLInputElement;
    if (input) {
      input.click();
    }
  }, []);

  // Handle file input change
  const handleFileChange = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const fileList = e.target.files;
    if (!fileList || fileList.length === 0 || !onUpload) return;
    try {
      await onUpload(fileList);
      // Refresh the file list after upload
      await loadFiles(currentPath);
    } finally {
      // Reset input so same file can be selected again
      setFileInputKey(prev => prev + 1);
    }
  }, [currentPath, loadFiles, onUpload]);

  // Handle new folder creation
  const handleCreateFolder = useCallback(async () => {
    if (!newFolderName.trim() || !onCreateDir) return;
    try {
      await onCreateDir(newFolderName);
      setNewFolderName("");
      setShowNewFolder(false);
      await loadFiles(currentPath);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [currentPath, loadFiles, newFolderName, onCreateDir]);

  // Handle delete selected items
  const handleDelete = useCallback(async () => {
    const selectedItems = selectedFiles();
    if (selectedItems.length === 0 || !onDelete) return;
    try {
      await onDelete(selectedItems);
      setSelected(new Set());
      await loadFiles(currentPath);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [currentPath, loadFiles, onDelete, selectedFiles]);

  // Load files when component mounts or path prop changes
  useEffect(() => {
    void loadFiles(initialPath);
  }, [initialPath, loadFiles]);

  // Breadcrumb components
  const breadcrumbs = getBreadcrumbs(currentPath);

  // Format file size display
  const formatSize = useCallback((size: number | null): string => {
    if (size === null) return "-";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
    if (size < 1024 * 1024 * 1024) return `${Math.round(size / (1024 * 1024))} MB`;
    return `${Math.round(size / (1024 * 1024 * 1024))} GB`;
  }, []);

  // Format date display
  const formatDate = useCallback((date: string | null): string => {
    if (!date) return "-";
    try {
      const d = new Date(date);
      return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch {
      return date;
    }
  }, []);

  // Reserve space for routing; hidden file input for upload
  return (
    <div className="cloud-file-browser" data-path={currentPath}>
      <input
        key={fileInputKey}
        id="cloud-file-upload"
        type="file"
        style={{ display: "none" }}
        multiple
        onChange={handleFileChange}
      />

      {/* Breadcrumb navigation */}
      <nav className="breadcrumb flex items-center gap-2 mb-4 text-sm" aria-label="Breadcrumb">
        <button
          className="breadcrumb-item flex items-center gap-1 text-color-dim hover:text-color-foreground disabled:opacity-50"
          onClick={navigateUp}
          disabled={getParentPath(currentPath) === null}
          aria-label="Parent directory"
          title="Parent directory"
        >
          <span aria-hidden="true">↑</span>
          <span>Up</span>
        </button>
        {breadcrumbs.map((crumb, idx) => (
          <span key={crumb.path} className="flex items-center gap-1">
            <span className="text-color-dim">/</span>
            {idx === breadcrumbs.length - 1 ? (
              <span className="text-color-foreground font-medium" aria-current="page">
                {crumb.name}
              </span>
            ) : (
              <button
                className="breadcrumb-item hover:text-color-accent"
                onClick={() => navigateTo(crumb.path)}
              >
                {crumb.name}
              </button>
            )}
          </span>
        ))}
      </nav>

      {/* Action bar */}
      <div className="action-bar flex flex-wrap items-center gap-2 mb-4">
        <button
          className="btn btn-secondary text-sm px-3 py-1"
          onClick={handleUploadClick}
          disabled={!onUpload}
        >
          Upload
        </button>
        <button
          className="btn btn-secondary text-sm px-3 py-1"
          onClick={() => setShowNewFolder(true)}
          disabled={!onCreateDir}
        >
          New Folder
        </button>
        <button
          className="btn btn-danger text-sm px-3 py-1"
          onClick={handleDelete}
          disabled={selected.size === 0 || !onDelete}
        >
          Delete ({selected.size})
        </button>
        <button
          className="btn btn-secondary text-sm px-3 py-1"
          onClick={selectAll}
        >
          {selected.size === files.length && files.length > 0 ? "Deselect All" : "Select All"}
        </button>
        <div className="spacer flex-1" />
        <button
          className="btn btn-icon text-sm px-3 py-1"
          onClick={() => void loadFiles(currentPath)}
          disabled={loading}
          title="Refresh"
        >
          <span aria-hidden="true">↻</span>
        </button>
      </div>

      {/* Error display */}
      {error && (
        <div className="error-alert mb-4 p-3 bg-color-danger bg-opacity-10 border border-color-danger rounded-lg text-color-danger text-sm">
          {error}
        </div>
      )}

      {/* File table */}
      <div className="file-table-container overflow-x-auto">
        <table className="file-table w-full text-sm">
          <thead>
            <tr className="border-b border-color-border">
              <th className="w-10 px-2 py-2 text-left">{/* Checkbox */}</th>
              <th className="px-4 py-2 text-left">Name</th>
              <th className="w-20 px-4 py-2 text-left">Type</th>
              <th className="w-24 px-4 py-2 text-right">Size</th>
              <th className="w-40 px-4 py-2 text-left">Last Modified</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-color-dim">
                  Loading…
                </td>
              </tr>
            ) : files.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-color-dim">
                  No files
                </td>
              </tr>
            ) : (
              files.map((file) => (
                <tr
                  key={file.path}
                  className={`border-b border-color-border hover:bg-color-accent-light hover:bg-opacity-50 ${selected.has(file.path) ? "bg-color-accent-light bg-opacity-20" : ""}`}
                  onClick={(e) => {
                    // Only trigger navigation if not clicking on checkbox or action buttons
                    if ((e.target as HTMLElement).tagName !== "INPUT" && 
                        (e.target as HTMLElement).tagName !== "BUTTON" &&
                        !(e.target as HTMLElement).closest("button")) {
                      handleSelectItem(file);
                    }
                  }}
                >
                  <td className="px-2 py-2">
                    <input
                      type="checkbox"
                      checked={selected.has(file.path)}
                      onChange={(e) => {
                        e.stopPropagation();
                        toggleSelect(file.path);
                      }}
                      className="w-4 h-4"
                    />
                  </td>
                  <td className="px-4 py-2">
                    <span className="flex items-center gap-2">
                      <span>{file.is_dir ? "📁" : "📄"}</span>
                      <span className={file.is_dir ? "font-medium text-color-accent" : ""}>
                        {file.name}
                      </span>
                    </span>
                  </td>
                  <td className="px-4 py-2 text-color-dim">
                    {file.is_dir ? "Directory" : file.type || "File"}
                  </td>
                  <td className="px-4 py-2 text-right text-color-dim">
                    {file.is_dir ? "-" : formatSize(file.size)}
                  </td>
                  <td className="px-4 py-2 text-color-dim">
                    {formatDate(file.modified_at)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* New folder dialog */}
      {showNewFolder && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4" onClick={() => setShowNewFolder(false)}>
          <div className="bg-color-card rounded-lg p-6 w-full max-w-sm" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-lg font-semibold mb-4">Create New Folder</h3>
            <label className="block mb-2 text-sm font-medium">
              Folder name
            </label>
            <input
              type="text"
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              className="w-full px-3 py-2 border border-color-border rounded-lg focus:outline-none focus:ring-2 focus:ring-color-accent"
              placeholder="Enter folder name"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void handleCreateFolder();
                }
              }}
            />
            <div className="flex gap-2 mt-6 justify-end">
              <button
                className="btn btn-secondary px-4 py-2"
                onClick={() => {
                  setNewFolderName("");
                  setShowNewFolder(false);
                }}
              >
                Cancel
              </button>
              <button
                className="btn btn-primary px-4 py-2"
                onClick={() => void handleCreateFolder()}
                disabled={!newFolderName.trim()}
              >
                Create
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
