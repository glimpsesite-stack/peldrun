// frontend/src/components/workspace/project-workspace-panel.tsx

/**
 * Project Workspace Panel - Project-scoped workspace management.
 *
 * This is a project-specific version of WorkspacePanel that:
 * - Uses projectId as the primary scope (not activeChatId/activeJobId)
 * - Integrates with the new Workspace API (OCC, Git discovery, etc.)
 * - Uses useWorkspaceResolver hook for state management
 * - Supports the five tabs: Preview, Files, Edit, Artifacts, Log
 * - Provides Git repository selector integration
 *
 * Key differences from workspace-panel.tsx:
 * - Primary scope is project, not session/chat
 * - File operations use OCC (Optimistic Concurrency Control)
 * - Git repository discovery and selection
 * - Migration support for session files
 * - Task management integration (future)
 */

"use client";

import React, { useState, useEffect, useCallback, useRef, useMemo } from "react";
import {
  Monitor,
  FolderTree,
  Package,
  TerminalSquare,
  FileCode2,
  Download,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { PreviewTab } from "@/components/workspace/preview-tab";
import { FilesTab } from "@/components/workspace/files-tab";
import { ArtifactsTab } from "@/components/workspace/artifacts-tab";
import { LogsTab } from "@/components/workspace/logs-tab";
import { EditorTab } from "@/components/workspace/editor-tab";
import { RepositorySelector } from "@/features/source-control/components/repository-selector";
import { useWorkspaceResolver, WorkspaceTab } from "@/features/workspace/workspace-resolver";
import { GitRepositoryInfo } from "@/features/workspace/types";

export type ProjectTabId = "preview" | "files" | "artifacts" | "logs" | "editor";

export interface ProjectWorkspaceFileItem {
  name: string;
  path: string;
  size?: number;
}

export function getProjectFileCategory(filepath: string): "preview" | "artifacts" | "editor" {
  if (!filepath) return "preview";
  const lower = filepath.toLowerCase();
  const ext = lower.split(".").pop() || "";

  if (["html", "htm", "svg", "md", "markdown", "txt"].includes(ext)) {
    return "preview";
  }

  if (
    [
      "png", "jpg", "jpeg", "webp", "gif", "ico", "bmp",
      "mp4", "webm", "ogg", "mov", "avi", "mkv", "m4v",
      "pdf",
    ].includes(ext)
  ) {
    return "artifacts";
  }

  return "editor";
}

export interface ProjectWorkspacePanelProps {
  projectId: string;
  activeChatId?: string | null;
  activeJobId?: string | null;
  overrideFile?: string | null;
  overrideDraft?: { filename: string; content: string } | null;
}

export function ProjectWorkspacePanel({
  projectId,
  activeChatId,
  activeJobId,
  overrideFile,
  overrideDraft,
}: ProjectWorkspacePanelProps) {
  const [activeTab, setActiveTab] = useState<ProjectTabId>("preview");
  const [selectedFilePath, setSelectedFilePath] = useState<string | null>(null);
  const [draftContent, setDraftContent] = useState<string | null>(null);
  const [projectFiles, setProjectFiles] = useState<ProjectWorkspaceFileItem[]>([]);
  const [filesLoading, setFilesLoading] = useState<boolean>(false);
  const [exporting, setExporting] = useState<boolean>(false);
  const [selectedRepository, setSelectedRepository] = useState<GitRepositoryInfo | null>(null);

  const [workspaceState, workspaceActions] = useWorkspaceResolver(projectId, "files");

  const effectiveScopeId = activeChatId || activeJobId || null;
  const hasAutoSelectedRef = useRef<boolean>(false);
  const selectedFilePathRef = useRef<string | null>(null);
  selectedFilePathRef.current = selectedFilePath;

  const activeTabRef = useRef<ProjectTabId>(activeTab);
  activeTabRef.current = activeTab;
  const lastPreviewFileRef = useRef<string | null>(null);

  if (selectedFilePath && getProjectFileCategory(selectedFilePath) === "preview") {
    lastPreviewFileRef.current = selectedFilePath;
  }

  const handleUserSelectFile = useCallback((filePath: string) => {
    if (!filePath) return;
    const cleanPath = filePath.replace(/\\/g, "/");
    setSelectedFilePath(cleanPath);
    setDraftContent(null);

    const category = getProjectFileCategory(cleanPath);
    if (category === "preview") {
      lastPreviewFileRef.current = cleanPath;
      setActiveTab("preview");
    } else if (category === "artifacts") {
      setActiveTab("artifacts");
    } else {
      setActiveTab("editor");
    }
  }, []);

  const handleAutoArtifact = useCallback((filePath: string) => {
    if (!filePath) return;
    const cleanPath = filePath.replace(/\\/g, "/");
    const category = getProjectFileCategory(cleanPath);

    if (category === "preview") {
      lastPreviewFileRef.current = cleanPath;
      setSelectedFilePath(cleanPath);
      setActiveTab("preview");
    } else if (category === "artifacts") {
      if (activeTabRef.current !== "preview") {
        setSelectedFilePath(cleanPath);
        setActiveTab("artifacts");
      }
    }
  }, []);

  const handleEditorSaved = useCallback((savedPath: string) => {
    const cleanPath = savedPath.replace(/\\/g, "/");
    setSelectedFilePath(cleanPath);
    const category = getProjectFileCategory(cleanPath);
    if (category === "preview") {
      lastPreviewFileRef.current = cleanPath;
      setActiveTab("preview");
    }
  }, []);

  const fetchProjectFiles = useCallback(async () => {
    if (!projectId) {
      setProjectFiles([]);
      return;
    }
    try {
      setFilesLoading(true);
      // loadFiles() resolves to void and commits the new list into resolver
      // state AFTER this call returns. Reading workspaceState.files here
      // would use a stale closure snapshot (empty on first load), which is
      // why the panel auto-selected nothing on a cold page load. Mapping
      // and auto-selection now react to workspaceState.files in a dedicated
      // effect below, so they always see the committed list.
      await workspaceActions.loadFiles();
    } catch (err) {
      console.error("[ProjectWorkspacePanel] Failed to fetch project files:", err);
    } finally {
      setFilesLoading(false);
    }
  }, [projectId, workspaceActions]);

  // CRITICAL FIX: Use a ref to hold the latest fetchProjectFiles to prevent infinite loops
  const fetchProjectFilesRef = useRef(fetchProjectFiles);
  useEffect(() => {
    fetchProjectFilesRef.current = fetchProjectFiles;
  }, [fetchProjectFiles]);

  // React to the resolver's committed file list instead of reading it
  // inside fetchProjectFiles (stale closure). Runs on mount, after every
  // successful loadFiles(), and after artifact-triggered refreshes, so
  // auto-selection works on a cold page load of an existing project.
  useEffect(() => {
    const files: ProjectWorkspaceFileItem[] = (workspaceState.files || []).map((path) => ({
      name: path.split("/").pop() || path,
      path: path,
    }));
    setProjectFiles(files);

    if (hasAutoSelectedRef.current || selectedFilePathRef.current || files.length === 0) {
      return;
    }

    const webAppFile =
      files.find((f) => f.name.toLowerCase() === "index.html" && (f.size ?? 1) > 0) ||
      files.find((f) => {
        const l = f.name.toLowerCase();
        return (l.endsWith(".html") || l.endsWith(".htm")) && (f.size ?? 1) > 0;
      }) ||
      files.find((f) => getProjectFileCategory(f.path) === "preview" && (f.size ?? 1) > 0) ||
      files.find((f) => f.name.toLowerCase() === "index.html") ||
      files.find((f) => {
        const l = f.name.toLowerCase();
        return l.endsWith(".html") || l.endsWith(".htm");
      }) ||
      files.find((f) => getProjectFileCategory(f.path) === "preview");

    if (webAppFile) {
      hasAutoSelectedRef.current = true;
      lastPreviewFileRef.current = webAppFile.path;
      setSelectedFilePath(webAppFile.path);
      setActiveTab("preview");
      return;
    }

    const mediaFile = files.find((f) => getProjectFileCategory(f.path) === "artifacts");
    if (mediaFile) {
      hasAutoSelectedRef.current = true;
      setSelectedFilePath(mediaFile.path);
      setActiveTab("artifacts");
      return;
    }

    hasAutoSelectedRef.current = true;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceState.files]);

  // CRITICAL FIX: Dependency array contains ONLY [projectId]
  useEffect(() => {
    hasAutoSelectedRef.current = false;
    setSelectedFilePath(null);
    lastPreviewFileRef.current = null;
    setProjectFiles([]);
    if (projectId) {
      fetchProjectFilesRef.current();
    }
  }, [projectId]);

  useEffect(() => {
    const handleSwitchTab = (e: Event) => {
      const ce = e as CustomEvent<ProjectTabId | { tab?: ProjectTabId; file?: string; userInitiated?: boolean }>;
      const targetTab = typeof ce.detail === "string" ? ce.detail : ce.detail?.tab;
      const targetFile = typeof ce.detail === "object" ? ce.detail?.file : undefined;
      const isUserAction = typeof ce.detail === "object" ? Boolean(ce.detail?.userInitiated) : false;

      if (targetFile) {
        if (isUserAction) {
          handleUserSelectFile(targetFile);
        } else {
          handleAutoArtifact(targetFile);
        }
      } else if (
        targetTab &&
        ["preview", "files", "artifacts", "logs", "editor"].includes(targetTab)
      ) {
        if (isUserAction || targetTab === "preview" || targetTab === "artifacts") {
          setActiveTab(targetTab as ProjectTabId);
        }
      }
    };

    const handleArtifactCreated = (e: Event) => {
      const ce = e as CustomEvent<{ artifact?: string; path?: string; relative_path?: string; file?: string }>;
      const artPath = (ce.detail?.relative_path || ce.detail?.path || ce.detail?.artifact || ce.detail?.file || "").replace(/\\/g, "/");
      if (artPath) {
        handleAutoArtifact(artPath);
        fetchProjectFilesRef.current();
      }
    };

    window.addEventListener("peldrun:switch-tab", handleSwitchTab);
    window.addEventListener("peldrun:artifact-created", handleArtifactCreated);
    window.addEventListener("peldrun:artifact-updated", handleArtifactCreated);

    return () => {
      window.removeEventListener("peldrun:switch-tab", handleSwitchTab);
      window.removeEventListener("peldrun:artifact-created", handleArtifactCreated);
      window.removeEventListener("peldrun:artifact-updated", handleArtifactCreated);
    };
  }, [handleAutoArtifact, handleUserSelectFile]);

  const handleExportZip = async () => {
    if (!projectId) return;
    setExporting(true);
    try {
      console.log("Project ZIP export not yet implemented");
    } catch (err) {
      console.error("Export project ZIP failed:", err);
    } finally {
      setExporting(false);
    }
  };

  useEffect(() => {
    if (overrideFile) {
      handleUserSelectFile(overrideFile);
    }
  }, [overrideFile, handleUserSelectFile]);

  useEffect(() => {
    if (overrideDraft) {
      setSelectedFilePath(overrideDraft.filename);
      setDraftContent(overrideDraft.content);
      setActiveTab("editor");
    }
  }, [overrideDraft]);

  // CRITICAL FIX: Memoize this callback to prevent child re-renders
  const handleRepositorySelect = useCallback((repo: GitRepositoryInfo | null) => {
    setSelectedRepository(repo);
  }, []);

  const tabs: { id: ProjectTabId; label: string; icon: React.ReactNode }[] = [
    { id: "preview", label: "Preview", icon: <Monitor size={14} /> },
    { id: "files", label: "Files", icon: <FolderTree size={14} /> },
    { id: "artifacts", label: "Artifacts", icon: <Package size={14} /> },
    { id: "logs", label: "Logs", icon: <TerminalSquare size={14} /> },
    ...(selectedFilePath || draftContent
      ? [{ id: "editor" as ProjectTabId, label: "Editor", icon: <FileCode2 size={14} /> }]
      : []),
  ];

  const previewablePath =
    selectedFilePath && getProjectFileCategory(selectedFilePath) === "preview"
      ? selectedFilePath
      : lastPreviewFileRef.current;

  return (
    <div className="flex flex-col h-full bg-custom border-l border-border font-sans">
      <div className="h-auto border-b border-darkest/50 bg-[#000] backdrop-blur-sm px-4 py-2 shrink-0">
        <div className="mb-2">
          <RepositorySelector
            projectId={projectId}
            selectedRepositoryId={selectedRepository?.id || null}
            onSelect={handleRepositorySelect}
          />
        </div>

        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1 overflow-x-auto">
            {tabs.map((tab) => {
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 text-xxs font-medium transition-all cursor-pointer ${
                    isActive
                      ? "bg-primary/20 text-[#ccc] shadow-peldrun-xs text-xs border-slate-100 border-b-2"
                      : "text-lightest hover:text-[#ffffff]"
                  }`}
                >
                  {tab.icon}
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {selectedFilePath && (
              <span className="text-[11px] font-mono text-muted-foreground truncate max-w-[130px] hidden sm:inline" title={selectedFilePath}>
                {selectedFilePath}
              </span>
            )}

            {projectId && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => fetchProjectFilesRef.current()}
                disabled={filesLoading}
                className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground cursor-pointer"
                title="Refresh project workspace files"
              >
                <RefreshCw size={12} className={filesLoading ? "animate-spin text-primary" : ""} />
              </Button>
            )}

            {projectId && projectFiles.length > 0 && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleExportZip}
                disabled={exporting}
                className="h-7 px-2.5 text-xs font-sans gap-1.5 cursor-pointer text-muted-foreground hover:text-foreground border-border/60 hover:bg-muted/60 shadow-peldrun-xs"
                title="Export project workspace as ZIP"
              >
                {exporting ? (
                  <>
                    <Loader2 size={12} className="animate-spin text-primary" />
                    <span>Exporting...</span>
                  </>
                ) : (
                  <>
                    <Download size={12} className="text-primary" />
                    <span>Export ZIP</span>
                  </>
                )}
              </Button>
            )}
          </div>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-hidden bg-background">
        {activeTab === "preview" && (
          <PreviewTab
            currentHtmlPath={previewablePath}
            activeJobId={effectiveScopeId}
            projectId={projectId}
          />
        )}
        {activeTab === "files" && (
          <FilesTab onSelectFile={handleUserSelectFile} activeJobId={effectiveScopeId} projectId={projectId} />
        )}
        {activeTab === "artifacts" && (
          <ArtifactsTab
            activeJobId={effectiveScopeId}
            chatId={activeChatId}
            selectedFile={selectedFilePath}
          />
        )}
        {activeTab === "logs" && (
          <LogsTab
            activeJobId={activeJobId || effectiveScopeId}
            chatId={activeChatId || effectiveScopeId}
          />
        )}
        {activeTab === "editor" && (
          <EditorTab
            filePath={selectedFilePath}
            initialContent={draftContent}
            activeJobId={effectiveScopeId}
            projectId={projectId}
            onSave={handleEditorSaved}
          />
        )}
      </div>
    </div>
  );
}

export default ProjectWorkspacePanel;
