// frontend/src/features/workspace/occ-file-editor.tsx

import React, { useState, useEffect, useCallback } from "react";
import {
  WorkspaceFileReadResponse,
  OCCConflictError,
} from "./types";
import { workspaceApi } from "./workspace-api";

interface OCCFileEditorProps {
  projectId: string;
  filePath: string;
  onSave?: (response: WorkspaceFileReadResponse) => void;
  onError?: (error: Error) => void;
}

interface ConflictState {
  isConflict: boolean;
  currentVersion: string;
  serverContent?: string;
}

/**
 * File editor component with Optimistic Concurrency Control (OCC) support.
 * Detects conflicts when multiple users edit the same file simultaneously.
 */
export const OCCFileEditor: React.FC<OCCFileEditorProps> = ({
  projectId,
  filePath,
  onSave,
  onError,
}) => {
  const [content, setContent] = useState<string>("");
  const [version, setVersion] = useState<string>("");
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<ConflictState>({
    isConflict: false,
    currentVersion: "",
  });

  // Load file on mount or when filePath changes
  const loadFile = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    setConflict({ isConflict: false, currentVersion: "" });

    try {
      const response = await workspaceApi.readFile(projectId, filePath);
      setContent(response.content);
      setVersion(response.version);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to load file";
      setError(errorMessage);
      onError?.(err instanceof Error ? err : new Error(errorMessage));
    } finally {
      setIsLoading(false);
    }
  }, [projectId, filePath, onError]);

  useEffect(() => {
    loadFile();
  }, [loadFile]);

  // Handle save with OCC
  const handleSave = async () => {
    setIsSaving(true);
    setError(null);
    setConflict({ isConflict: false, currentVersion: "" });

    try {
      const response = await workspaceApi.updateFile(projectId, {
        path: filePath,
        content,
        expected_version: version,
      });

      // Update version with new hash
      setVersion(response.version);
      onSave?.(response);
    } catch (err) {
      if (err instanceof OCCConflictError) {
        // Handle conflict: show merge options
        setConflict({
          isConflict: true,
          currentVersion: err.currentVersion,
        });

        // Optionally load server content for comparison
        try {
          const serverFile = await workspaceApi.readFile(projectId, filePath);
          setConflict((prev) => ({
            ...prev,
            serverContent: serverFile.content,
          }));
        } catch (loadErr) {
          console.error("Failed to load server content for conflict resolution:", loadErr);
        }
      } else {
        const errorMessage = err instanceof Error ? err.message : "Failed to save file";
        setError(errorMessage);
        onError?.(err instanceof Error ? err : new Error(errorMessage));
      }
    } finally {
      setIsSaving(false);
    }
  };

  // Resolve conflict by reloading server version
  const handleReload = async () => {
    await loadFile();
  };

  // Resolve conflict by keeping local changes (user can manually merge)
  const handleKeepLocal = () => {
    setConflict({ isConflict: false, currentVersion: "" });
    // User can continue editing and try to save again
    // They should manually merge changes before saving
  };

  if (isLoading) {
    return (
      <div className="occ-file-editor loading">
        <div className="spinner">Loading file...</div>
      </div>
    );
  }

  if (error && !conflict.isConflict) {
    return (
      <div className="occ-file-editor error">
        <div className="error-message">
          <span className="error-icon">⚠️</span>
          <span>{error}</span>
        </div>
        <button onClick={loadFile} className="retry-button">
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="occ-file-editor">
      {/* Conflict Resolution Dialog */}
      {conflict.isConflict && (
        <div className="conflict-dialog">
          <div className="conflict-header">
            <h3>⚠️ File Conflict Detected</h3>
            <p>
              This file has been modified by another user or process since you
              started editing.
            </p>
          </div>

          <div className="conflict-options">
            <button
              onClick={handleReload}
              className="reload-button"
              disabled={isSaving}
            >
              🔄 Reload Server Version
              <span className="button-description">
                Discard your changes and load the latest version
              </span>
            </button>

            <button
              onClick={handleKeepLocal}
              className="keep-local-button"
              disabled={isSaving}
            >
              ✏️ Keep My Changes
              <span className="button-description">
                Continue editing and manually merge changes before saving
              </span>
            </button>
          </div>

          {conflict.serverContent && (
            <div className="diff-viewer">
              <h4>Comparison:</h4>
              <div className="diff-container">
                <div className="diff-column">
                  <h5>Your Version:</h5>
                  <pre className="diff-content">{content}</pre>
                </div>
                <div className="diff-column">
                  <h5>Server Version:</h5>
                  <pre className="diff-content">{conflict.serverContent}</pre>
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* File Editor */}
      {!conflict.isConflict && (
        <>
          <div className="editor-header">
            <div className="file-info">
              <span className="file-path">{filePath}</span>
              <span className="file-version">
                Version: {version.substring(0, 8)}...
              </span>
            </div>

            <button
              onClick={handleSave}
              disabled={isSaving}
              className="save-button"
            >
              {isSaving ? "Saving..." : "💾 Save"}
            </button>
          </div>

          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            className="file-content-editor"
            placeholder="File content..."
            disabled={isSaving}
          />

          {error && (
            <div className="inline-error">
              <span className="error-icon">⚠️</span>
              <span>{error}</span>
            </div>
          )}
        </>
      )}

      <style>{`
        .occ-file-editor {
          display: flex;
          flex-direction: column;
          height: 100%;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }

        .occ-file-editor.loading,
        .occ-file-editor.error {
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          padding: 2rem;
          gap: 1rem;
        }

        .spinner {
          color: #666;
          font-size: 1rem;
        }

        .error-message {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          color: #d32f2f;
          font-size: 0.9rem;
        }

        .retry-button {
          padding: 0.5rem 1rem;
          background: #1976d2;
          color: white;
          border: none;
          border-radius: 4px;
          cursor: pointer;
          font-size: 0.9rem;
        }

        .retry-button:hover {
          background: #1565c0;
        }

        .conflict-dialog {
          background: #fff3cd;
          border: 2px solid #ffc107;
          border-radius: 8px;
          padding: 1.5rem;
          margin: 1rem;
        }

        .conflict-header h3 {
          margin: 0 0 0.5rem 0;
          color: #856404;
          font-size: 1.2rem;
        }

        .conflict-header p {
          margin: 0 0 1rem 0;
          color: #856404;
          font-size: 0.9rem;
        }

        .conflict-options {
          display: flex;
          gap: 1rem;
          margin-bottom: 1rem;
        }

        .conflict-options button {
          flex: 1;
          padding: 1rem;
          border: 2px solid;
          border-radius: 6px;
          cursor: pointer;
          text-align: left;
          transition: all 0.2s;
        }

        .reload-button {
          background: white;
          border-color: #1976d2;
          color: #1976d2;
        }

        .reload-button:hover:not(:disabled) {
          background: #e3f2fd;
        }

        .keep-local-button {
          background: white;
          border-color: #388e3c;
          color: #388e3c;
        }

        .keep-local-button:hover:not(:disabled) {
          background: #e8f5e9;
        }

        .button-description {
          display: block;
          font-size: 0.75rem;
          margin-top: 0.25rem;
          opacity: 0.8;
        }

        .diff-viewer {
          margin-top: 1rem;
          padding-top: 1rem;
          border-top: 1px solid #ffc107;
        }

        .diff-viewer h4 {
          margin: 0 0 0.5rem 0;
          font-size: 0.9rem;
          color: #856404;
        }

        .diff-container {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 1rem;
        }

        .diff-column h5 {
          margin: 0 0 0.25rem 0;
          font-size: 0.8rem;
          color: #666;
        }

        .diff-content {
          background: white;
          border: 1px solid #ddd;
          border-radius: 4px;
          padding: 0.5rem;
          font-size: 0.75rem;
          max-height: 300px;
          overflow: auto;
          white-space: pre-wrap;
          word-break: break-all;
        }

        .editor-header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 0.75rem 1rem;
          background: #f5f5f5;
          border-bottom: 1px solid #ddd;
        }

        .file-info {
          display: flex;
          flex-direction: column;
          gap: 0.25rem;
        }

        .file-path {
          font-weight: 600;
          font-size: 0.9rem;
          color: #333;
        }

        .file-version {
          font-size: 0.75rem;
          color: #666;
          font-family: monospace;
        }

        .save-button {
          padding: 0.5rem 1.5rem;
          background: #1976d2;
          color: white;
          border: none;
          border-radius: 4px;
          cursor: pointer;
          font-size: 0.9rem;
          font-weight: 500;
          transition: background 0.2s;
        }

        .save-button:hover:not(:disabled) {
          background: #1565c0;
        }

        .save-button:disabled {
          background: #ccc;
          cursor: not-allowed;
        }

        .file-content-editor {
          flex: 1;
          padding: 1rem;
          border: none;
          outline: none;
          resize: none;
          font-family: "Monaco", "Menlo", "Ubuntu Mono", monospace;
          font-size: 0.875rem;
          line-height: 1.5;
          background: white;
        }

        .file-content-editor:disabled {
          background: #f9f9f9;
          color: #999;
        }

        .inline-error {
          display: flex;
          align-items: center;
          gap: 0.5rem;
          padding: 0.75rem 1rem;
          background: #ffebee;
          border-top: 1px solid #ef5350;
          color: #c62828;
          font-size: 0.85rem;
        }
      `}</style>
    </div>
  );
};
