/**
 * frontend/src/components/workspace/files-tab.tsx
 *
 * Authoritative Workspace Files Tree tab.
 * Dynamically builds and renders hierarchical folders from flat relative paths.
 * Supports expanding/collapsing directories, nested file inspection, and direct editing trigger.
 */

"use client";

import React, { useEffect, useState, useMemo, useCallback } from "react";
import { Folder, FolderOpen, FileText, Download, Trash2, RefreshCw, Filter, ChevronRight, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/Icon/Icon";
import { getFileIconName } from "@/lib/file-icon-mapper";

interface FileItem {
  name: string;
  path: string;
  isDir: boolean;
  size?: number;
  extension?: string;
  modified?: number;
  children?: FileItem[];
}

export interface FilesTabProps {
  onSelectFile?: (path: string) => void;
  onOpenFile?: (path: string) => void;
  activeJobId?: string | null;
  /**
   * When provided, the tree lists the project's workspace (external or
   * internal) via the Workspace API instead of the chat/global listings.
   * Required inside ProjectWorkspacePanel: the agent writes deliverables
   * into the project workspace, which the legacy chat-scoped endpoints
   * and the /api/files artifact walk cannot see.
   */
  projectId?: string | null;
}

function buildFileTree(flatFiles: FileItem[]): FileItem[] {
  if (flatFiles.length === 0) return [];

  const rootItems: FileItem[] = [];
  const dirMap = new Map<string, FileItem>();

  for (const file of flatFiles) {
    const rawPath = (file.path || file.name).replace(/\\/g, "/").replace(/^\/+/, "");
    const parts = rawPath.split("/").filter(Boolean);

    if (parts.length <= 1) {
      rootItems.push({
        ...file,
        name: parts[0] || file.name,
        path: rawPath,
        isDir: false,
      });
      continue;
    }

    let currentDirPath = "";
    let parentChildren = rootItems;

    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      currentDirPath = currentDirPath ? `${currentDirPath}/${part}` : part;

      let dirNode = dirMap.get(currentDirPath);
      if (!dirNode) {
        dirNode = {
          name: part,
          path: currentDirPath,
          isDir: true,
          children: [],
        };
        dirMap.set(currentDirPath, dirNode);
        parentChildren.push(dirNode);
      }
      parentChildren = dirNode.children!;
    }

    parentChildren.push({
      ...file,
      name: parts[parts.length - 1],
      path: rawPath,
      isDir: false,
    });
  }

  // Sort: folders first, then files alphabetically
  const sortRecursive = (items: FileItem[]) => {
    items.sort((a, b) => {
      if (a.isDir && !b.isDir) return -1;
      if (!a.isDir && b.isDir) return 1;
      return a.name.localeCompare(b.name);
    });
    for (const item of items) {
      if (item.isDir && item.children) {
        sortRecursive(item.children);
      }
    }
  };

  sortRecursive(rootItems);
  return rootItems;
}

export function FilesTab({ onSelectFile, onOpenFile, activeJobId, projectId }: FilesTabProps) {
  const [allFiles, setAllFiles] = useState<FileItem[]>([]);
  const [taskFiles, setTaskFiles] = useState<FileItem[]>([]);
  const [projectFiles, setProjectFiles] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [filterCurrentOnly, setFilterCurrentOnly] = useState(true);
  // User-driven expand/collapse overrides. Any directory not present in
  // this map falls back to the name-based default computed in
  // isDirCollapsedByDefault(): dot-directories are collapsed, others open.
  const [dirOverrides, setDirOverrides] = useState<Map<string, boolean>>(new Map());

  const handleItemClick = (path: string) => {
    if (onSelectFile) onSelectFile(path);
    if (onOpenFile) onOpenFile(path);
  };

  // Resolves the effective collapsed state for a directory node.
  // Explicit user overrides win; otherwise:
  //   - dot-directories (e.g. .git, .cache, .venv) default to collapsed
  //   - all other directories default to expanded
  const isDirCollapsedByDefault = (item: FileItem): boolean => {
    if (dirOverrides.has(item.path)) return dirOverrides.get(item.path)!;
    return item.name.startsWith(".");
  };

  const toggleDir = (dirPath: string, currentlyCollapsed: boolean) => {
    setDirOverrides((prev) => {
      const next = new Map(prev);
      next.set(dirPath, !currentlyCollapsed);
      return next;
    });
  };

  const fetchFiles = useCallback(async () => {
    setLoading(true);
    try {
      // ── Project mode: list the project workspace (internal or external) ──
      // The Workspace API returns a flat JSON array of workspace-relative
      // path strings. buildFileTree synthesizes the folder hierarchy from
      // those paths, so only file entries are needed here.
      if (projectId) {
        const resProject = await fetch(
          `/api/projects/${encodeURIComponent(projectId)}/workspace/files?path=&t=${Date.now()}`,
          { cache: "no-store" }
        );
        if (resProject.ok) {
          const dataProject = await resProject.json();
          // Primary shape: string[]. Tolerate {files:[...]} / {entries:[...]}
          // wrappers and object entries so a backend variation does not
          // blank the tree.
          const entries: any[] = Array.isArray(dataProject)
            ? dataProject
            : dataProject?.files || dataProject?.entries || [];
          const normalized: FileItem[] = entries
            .map((entry: any): FileItem | null => {
              const rawPath =
                typeof entry === "string"
                  ? entry
                  : entry?.path || entry?.relative_path || entry?.name || "";
              if (!rawPath) return null;
              if (
                typeof entry === "object" &&
                entry !== null &&
                (entry.is_dir || entry.isDir || entry.type === "directory")
              ) {
                return null;
              }
              const cleanPath = rawPath.replace(/\\/g, "/").replace(/^\/+/, "");
              return {
                name: cleanPath.split("/").pop() || cleanPath,
                path: cleanPath,
                isDir: false,
                size: typeof entry === "object" && entry !== null ? entry.size : undefined,
                modified: typeof entry === "object" && entry !== null ? entry.modified : undefined,
              };
            })
            .filter((f: FileItem | null): f is FileItem => f !== null);
          setProjectFiles(normalized);
        } else {
          setProjectFiles([]);
        }
        // Project mode is self-contained; skip the chat/global listings.
        return;
      }

      // ── Legacy chat mode (unchanged behavior) ──
      // 1. Fetch generic workspace files
      const resAll = await fetch("/api/files?t=" + Date.now());
      if (resAll.ok) {
        const dataAll = await resAll.json();
        setAllFiles(dataAll.files || []);
      }
      // 2. Fetch active job deliverable files
      if (activeJobId) {
        const resTask = await fetch(`/api/run/jobs/${activeJobId}/files?t=` + Date.now());
        if (resTask.ok) {
          const dataTask = await resTask.json();
          setTaskFiles(dataTask.files || []);
        }
      } else {
        setTaskFiles([]);
      }
    } catch (err) {
      console.error("Failed to load workspace files:", err);
    } finally {
      setLoading(false);
    }
  }, [projectId, activeJobId]);

  const handleDelete = async (filePath: string) => {
    if (!confirm(`Are you sure you want to delete ${filePath}?`)) return;
    try {
      // Project mode: delete through the Workspace API so the target is
      // resolved inside the project workspace. The generic /api/files
      // DELETE cannot resolve workspace-relative project paths and could
      // hit a same-named legacy file elsewhere.
      const res = projectId
        ? await fetch(
            `/api/projects/${encodeURIComponent(projectId)}/workspace/file?path=${encodeURIComponent(filePath)}`,
            { method: "DELETE" }
          )
        : await fetch(`/api/files?path=${encodeURIComponent(filePath)}`, {
            method: "DELETE",
          });
      if (res.ok) {
        fetchFiles();
      }
    } catch (err) {
      console.error("Failed to delete file:", err);
    }
  };

  useEffect(() => {
    fetchFiles();
  }, [fetchFiles]);
  useEffect(() => {
    const handleSaved = () => fetchFiles();
    window.addEventListener("peldrun:file-saved", handleSaved);
    window.addEventListener("peldrun:artifact-created", handleSaved);
    window.addEventListener("peldrun:artifact-updated", handleSaved);
    return () => {
      window.removeEventListener("peldrun:file-saved", handleSaved);
      window.removeEventListener("peldrun:artifact-created", handleSaved);
      window.removeEventListener("peldrun:artifact-updated", handleSaved);
    };
  }, [fetchFiles]);

  // In project mode the tree always reflects the project workspace; the
  // chat-centric "This Task / All Workspace" toggle does not apply there.
  const rawList = projectId
    ? projectFiles
    : filterCurrentOnly && activeJobId
      ? taskFiles
      : allFiles;
  const hierarchicalTree = useMemo(() => buildFileTree(rawList), [rawList]);

  const renderTree = (items: FileItem[]) => {
    if (items.length === 0) {
      return (
        <div className="p-6 text-center text-xs text-muted-foreground">
          {filterCurrentOnly
            ? "No deliverables produced in this specific task yet."
            : "No files found in workspace."}
        </div>
      );
    }

    return (
      <ul className="space-y-0.5 select-none">
        {items.map((item) => {
          const isDirCollapsed = isDirCollapsedByDefault(item);
          // Resolve the language/tool-specific icon for files. Returns
          // null for directories and unrecognized files, in which case
          // we fall back to the generic FileText icon below.
          const fileIconName = item.isDir ? null : getFileIconName(item.path);

          return (
            <li key={item.path} className="text-xs">
              <div className="flex items-center justify-between p-1 rounded hover:bg-custom group transition">
                <div
                  className="flex items-center gap-1.5 truncate cursor-pointer flex-1"
                  onClick={() => {
                    if (item.isDir) {
                      toggleDir(item.path, isDirCollapsed);
                    } else {
                      handleItemClick(item.path);
                    }
                  }}
                >
                  {item.isDir ? (
                    <>
                      {isDirCollapsed ? (
                        <ChevronRight size={12} className="text-black shrink-0" />
                      ) : (
                        <ChevronDown size={12} className="text-black shrink-0" />
                      )}
                      {isDirCollapsed ? (
                        <Folder size={14} className="text-amber-400 shrink-0" />
                      ) : (
                        <FolderOpen size={14} className="text-amber-400 shrink-0" />
                      )}
                      <span className="font-semibold text-black/70 truncate">{item.name}</span>
                    </>
                  ) : (
                    <>
                      <span className="w-3" />
                      {fileIconName ? (
                        <Icon
                          name={fileIconName}
                          size={14}
                          className="shrink-0"
                        />
                      ) : (
                        <FileText size={14} className="text-gray-400 shrink-0" />
                      )}
                      <span className="truncate text-black">{item.name}</span>
                      {item.size !== undefined && (
                        <span className="text-[10px] text-[#ccc] ml-1">
                          ({(item.size / 1024).toFixed(1)} KB)
                        </span>
                      )}
                    </>
                  )}
                </div>

                {!item.isDir && (
                  <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <a
                      href={projectId ? `/api/files/raw-project/${encodeURIComponent(projectId)}/${encodeURI(item.path)}` : `/api/files/download?path=${encodeURIComponent(item.path)}`}
                      download
                      className="p-1 text-[#eee] hover:text-[#eee]"
                      title="Download"
                    >
                      <Download size={12} />
                    </a>
                    <button
                      onClick={() => handleDelete(item.path)}
                      className="p-1 text-[#eee] hover:text-[#eee]"
                      title="Delete"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                )}
              </div>

              {item.isDir && item.children && item.children.length > 0 && !isDirCollapsed && (
                <div className="pl-3.5 border-l border-border/40 ml-2 mt-0.5">
                  {renderTree(item.children)}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    );
  };

  return (
    <div className="flex flex-col h-full bg-background text-black font-sans text-xs">
      <div className="flex items-center justify-between p-3 border-b border-custom bg-custom backdrop-blur-sm">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-xs tracking-wider text-[#eee]font-mono">
            WORKSPACE FILES
          </span>
          <button
            onClick={() => { if (!projectId) setFilterCurrentOnly(!filterCurrentOnly); }}
            className={`px-1.5 py-0.5 rounded text-[10px] flex items-center gap-1 transition cursor-pointer font-sans ${
              filterCurrentOnly
                ? "bg-black text-rby border border-rb font-medium"
                : "bg-muted text-muted-foreground hover:text-foreground"
            }`}
            title="Toggle between current task deliverables and global workspace"
          >
            <Filter size={10} />
            {projectId ? "Project" : filterCurrentOnly ? "This Task" : "All Workspace"}
          </button>
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={fetchFiles}
          disabled={loading}
          className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground cursor-pointer"
          title="Refresh files"
        >
          <RefreshCw size={12} className={loading ? "animate-spin text-primary" : ""} />
        </Button>
      </div>
      {/* Loading state: shown while the initial fetch (or a refresh that
          empties the tree) is in flight. Reuses the same RefreshCw icon
          and theme tokens already present in the file — no new styles. */}
      <div className="flex-1 overflow-y-auto p-3 font-mono">
        {loading && hierarchicalTree.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-12 text-xs text-muted-foreground select-none">
            <RefreshCw size={18} className="animate-spin text-primary" />
            <span>Loading files...</span>
          </div>
        ) : (
          renderTree(hierarchicalTree)
        )}
      </div>
    </div>
  );
}

export default FilesTab;
