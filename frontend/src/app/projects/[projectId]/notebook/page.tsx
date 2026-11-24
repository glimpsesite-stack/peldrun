// frontend/src/app/projects/[projectId]/notebook/page.tsx

/**
 * Project Notebook Page - NotebookView
 *
 * Manages Markdown-based notes within a project workspace.
 *
 * Notes are stored as `.md` files inside the `notebook/` directory.
 * Display metadata (title + short description) lives separately in
 * `notebook/index.json` so the user can rename a note's title without
 * touching its file path or content.
 *
 * Features:
 * - List all Markdown files in the notebook directory
 * - Sidebar shows the human-readable title and short description
 * - Rename title / edit description inline from the sidebar
 * - Create new notes (title + optional description) and open the editor
 * - Delete notes with confirmation
 * - Edit notes with OCC (Optimistic Concurrency Control) support
 * - Preview rendered Markdown via MarkdownRenderer
 * - Internal links between notes ([[note-name]] syntax)
 * - Self-healing index: reconciles index.json with the actual .md files
 */

"use client";

import React, { useState, useEffect, useMemo, useRef } from "react";
import { useParams } from "next/navigation";
import { createPortal } from "react-dom";
import {
  BookOpen,
  Plus,
  Loader2,
  AlertCircle,
  Search,
  Trash2,
  Edit,
  Eye,
  FileText,
  X,
  Link as LinkIcon,
  Maximize2,
  Minimize2,
  Pencil,
  Check,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { MarkdownRenderer } from "@/components/chat/markdown-renderer";
import { workspaceApi } from "@/features/workspace/workspace-api";
import { OCCConflictError } from "@/features/workspace/types";
import { NotebookEditor } from "@/features/workspace/notebook-editor";
import { PrintModal } from "@/features/workspace/print-modal";
import { Printer } from "lucide-react";
import { useConfirmModal } from "@/components/ui/ConfirmModal";
import { showToast } from "@/components/ui/ToastNotification";

const NOTEBOOK_DIR = "notebook";
const INDEX_PATH = `${NOTEBOOK_DIR}/index.json`;
const INDEX_VERSION = 1;

// ----------------------------------------------------------------------------
// Types
// ----------------------------------------------------------------------------

/** A note entry exposed to the UI. */
interface Note {
  /** Filename without .md (stable identifier used by [[wikilinks]]). */
  name: string;
  /** Workspace-relative path. Immutable across the lifetime of the note. */
  path: string;
  /** Human-readable title shown in the sidebar. Editable. */
  title: string;
  /** Short description shown under the title. Editable. */
  description: string;
}

/** Shape persisted to notebook/index.json. */
interface NotebookIndex {
  version: number;
  notes: Array<{
    path: string;
    title: string;
    description: string;
  }>;
}

// ----------------------------------------------------------------------------
// Index helpers
// ----------------------------------------------------------------------------

/** Extracts a filename (without .md) from a workspace-relative path. */
function fileNameFromPath(filePath: string): string {
  return filePath.split("/").pop()?.replace(/\.md$/, "") || filePath;
}

/** Converts "thinking-moode" -> "Thinking Moode" (fallback title). */
function fallbackTitleFromName(name: string): string {
  return name
    .replace(/[-_]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Generates a filesystem-safe slug from a title.
 * Unicode-aware so non-Latin titles still produce a valid filename.
 */
function slugifyTitle(title: string): string {
  return title
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\-_ ]/gu, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

/** Parses raw index.json content. Returns an empty index on any failure. */
function parseIndex(raw: string | null | undefined): NotebookIndex {
  if (!raw) return { version: INDEX_VERSION, notes: [] };
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      return { version: INDEX_VERSION, notes: [] };
    }
    const root = parsed as { version?: unknown; notes?: unknown };
    if (!Array.isArray(root.notes)) {
      return { version: INDEX_VERSION, notes: [] };
    }
    const notes: NotebookIndex["notes"] = [];
    for (const entry of root.notes) {
      if (!entry || typeof entry !== "object") continue;
      const e = entry as {
        path?: unknown;
        title?: unknown;
        description?: unknown;
      };
      if (typeof e.path !== "string") continue;
      notes.push({
        path: e.path,
        title: typeof e.title === "string" ? e.title : "",
        description: typeof e.description === "string" ? e.description : "",
      });
    }
    return {
      version:
        typeof root.version === "number" ? root.version : INDEX_VERSION,
      notes,
    };
  } catch {
    return { version: INDEX_VERSION, notes: [] };
  }
}

/** Serializes the index to a stable, human-readable JSON string. */
function serializeIndex(index: NotebookIndex): string {
  const sortedNotes = [...index.notes].sort((a, b) =>
    a.path.localeCompare(b.path)
  );
  return JSON.stringify(
    { version: index.version, notes: sortedNotes },
    null,
    2
  );
}

/** Maps a UI Note[] to the persistence shape used by index.json. */
function notesToIndex(noteList: Note[]): NotebookIndex {
  return {
    version: INDEX_VERSION,
    notes: noteList.map((n) => ({
      path: n.path,
      title: n.title,
      description: n.description,
    })),
  };
}

/**
 * Reconciles the index with the actual .md files present on disk.
 *
 * - Entries whose file no longer exists are dropped.
 * - Files not present in the index are added with a fallback title.
 * - Existing entries keep their original order; new ones are appended.
 */
function reconcileNotes(filePaths: string[], index: NotebookIndex): Note[] {
  const seen = new Set<string>();
  const notes: Note[] = [];

  for (const entry of index.notes) {
    if (!filePaths.includes(entry.path)) continue;
    seen.add(entry.path);
    const name = fileNameFromPath(entry.path);
    notes.push({
      name,
      path: entry.path,
      title: entry.title.trim() || fallbackTitleFromName(name),
      description: entry.description,
    });
  }

  for (const filePath of filePaths) {
    if (seen.has(filePath)) continue;
    const name = fileNameFromPath(filePath);
    notes.push({
      name,
      path: filePath,
      title: fallbackTitleFromName(name),
      description: "",
    });
  }

  return notes;
}

// ----------------------------------------------------------------------------
// Component
// ----------------------------------------------------------------------------

export default function NotebookPage() {
  const params = useParams();
  const projectId = params.projectId as string;
  const { confirm, ConfirmDialog } = useConfirmModal();

  const [notes, setNotes] = useState<Note[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedNote, setSelectedNote] = useState<Note | null>(null);
  const [viewMode, setViewMode] = useState<"list" | "edit" | "preview">("list");
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [previewContent, setPreviewContent] = useState<string>("");
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [isLoadingContent, setIsLoadingContent] = useState(false);
  const [showPrintModal, setShowPrintModal] = useState(false);
  const [printOverlayData, setPrintOverlayData] = useState<Array<{ note: Note, content: string }> | null>(null);

  // Tracks the latest requested note path to prevent race conditions
  const latestRequestRef = useRef<string | null>(null);

  // Inline meta editing (title + description) state.
  const [editingPath, setEditingPath] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState("");
  const [editingDescription, setEditingDescription] = useState("");
  const [isSavingMeta, setIsSavingMeta] = useState(false);

  // Load notes on mount
  useEffect(() => {
    loadNotes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // Fullscreen editor: lock body scroll and exit on Escape.
  useEffect(() => {
    if (!isFullscreen) return;

    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsFullscreen(false);
      }
    };

    window.addEventListener("keydown", handleKeyDown);

    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = originalOverflow;
    };
  }, [isFullscreen]);

  /** Persists the given notes list as notebook/index.json. */
  const persistIndex = async (noteList: Note[]): Promise<void> => {
    const content = serializeIndex(notesToIndex(noteList));

    // Fetch the current version of index.json (if it exists) so we can
    // satisfy OCC on update. Missing file => expected_version stays "".
    let expectedVersion = "";
    try {
      const current = await workspaceApi.readFile(projectId, INDEX_PATH);
      expectedVersion =
        (current as { version?: string }).version || "";
    } catch {
      // File doesn't exist yet; the backend will create it.
    }

    await workspaceApi.updateFile(projectId, {
      path: INDEX_PATH,
      content,
      expected_version: expectedVersion,
    });
  };

  const loadNotes = async () => {
    setIsLoading(true);
    setError(null);

    try {
      const files = await workspaceApi.listFiles(projectId, NOTEBOOK_DIR);
      const markdownFiles = files.filter((f) => f.endsWith(".md"));

      // Read the index if it exists; otherwise treat it as empty.
      let index: NotebookIndex = { version: INDEX_VERSION, notes: [] };
      try {
        const idxFile = await workspaceApi.readFile(projectId, INDEX_PATH);
        index = parseIndex(idxFile.content);
      } catch {
        // index.json not present yet; it will be created after reconciliation.
      }

      const reconciled = reconcileNotes(markdownFiles, index);
      setNotes(reconciled);

      // Persist only if the reconciled index differs from what was on disk.
      const beforeJson = serializeIndex(index);
      const afterJson = serializeIndex(notesToIndex(reconciled));
      if (beforeJson !== afterJson) {
        try {
          await persistIndex(reconciled);
        } catch (persistErr) {
          console.warn(
            "[NotebookPage] Failed to persist reconciled index:",
            persistErr
          );
        }
      }
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "";
      const isNotFoundError =
        errorMessage.includes("404") ||
        errorMessage.toLowerCase().includes("not found") ||
        errorMessage.toLowerCase().includes("directory not found");

      if (isNotFoundError) {
        setNotes([]);
      } else {
        setError(errorMessage || "Failed to load notes");
        console.error("[NotebookPage] Failed to load notes:", err);
      }
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreateNote = async (title: string, description: string) => {
    const trimmedTitle = title.trim();
    if (!trimmedTitle) {
      showToast.error("Validation Error", "Title is required.");
      return;
    }

    let safeName = slugifyTitle(trimmedTitle);
    if (!safeName) {
      safeName = `note-${Date.now()}`;
    }

    // Resolve filename collisions with a numeric suffix.
    const existingNames = new Set(notes.map((n) => n.name));
    let finalName = safeName;
    let counter = 2;
    while (existingNames.has(finalName)) {
      finalName = `${safeName}-${counter}`;
      counter += 1;
    }

    const notePath = `${NOTEBOOK_DIR}/${finalName}.md`;
    // Content is empty: title/description live in the index, not the file.
    const content = "";

    try {
      await workspaceApi.updateFile(projectId, {
        path: notePath,
        content,
        expected_version: "",
      });

      const newNote: Note = {
        name: finalName,
        path: notePath,
        title: trimmedTitle,
        description: description.trim(),
      };
      const updatedNotes = [...notes, newNote];

      // Persist the index; rollback the file if this fails.
      try {
        await persistIndex(updatedNotes);
      } catch (indexErr) {
        try {
          await workspaceApi.deleteFile(projectId, notePath);
        } catch {
          /* best-effort rollback */
        }
        throw indexErr;
      }

      setNotes(updatedNotes);
      setShowCreateModal(false);
      setPreviewContent(content);
      setSelectedNote(newNote);
      setViewMode("edit");
      showToast.success("Note Created", `"${trimmedTitle}" is ready for editing.`);
    } catch (err) {
      const errorMessage =
        err instanceof Error ? err.message : "Failed to create note";
      showToast.error("Creation Failed", errorMessage);
    }
  };

  const handleDeleteNote = async (note: Note) => {
    const confirmed = await confirm({
      title: "Delete Note",
      description: `Are you sure you want to delete "${note.title}"? This action cannot be undone.`,
      confirmText: "Delete",
      variant: "danger",
    });
    if (!confirmed) return;

    try {
      await workspaceApi.deleteFile(projectId, note.path);

      const updatedNotes = notes.filter((n) => n.path !== note.path);
      setNotes(updatedNotes);

      if (selectedNote?.path === note.path) {
        setSelectedNote(null);
        setViewMode("list");
      }

      // Non-fatal: self-healing on next load cleans up a stale entry.
      try {
        await persistIndex(updatedNotes);
      } catch (indexErr) {
        console.warn(
          "[NotebookPage] Failed to persist index after delete:",
          indexErr
        );
      }
      showToast.success("Note Deleted", "The note has been permanently removed.");
    } catch (err) {
      const errorMessage =
        err instanceof Error ? err.message : "Failed to delete note";
      alert(errorMessage);
    }
  };

  const handlePrint = async (size: string, orientation: string, target: "current" | "all") => {
    try {
      let printNotes: Array<{ note: Note, content: string }> = [];

      if (target === "all") {
        // Fetch all notes in parallel
        printNotes = await Promise.all(
          notes.map(async (note) => {
            try {
              const fileContent = await workspaceApi.readFile(projectId, note.path);
              return { note, content: fileContent.content };
            } catch {
              return { note, content: "*Failed to load this note content.*" };
            }
          })
        );
      } else if (selectedNote) {
        printNotes = [{ note: selectedNote, content: previewContent }];
      }

      // 1. Inject print CSS to isolate the overlay and format pages
      const styleId = "dynamic-print-style";
      let styleEl = document.getElementById(styleId);
      if (!styleEl) {
        styleEl = document.createElement("style");
        styleEl.id = styleId;
        document.head.appendChild(styleEl);
      }
      styleEl.innerHTML = `
        @media print {
          @page { size: ${size} ${orientation}; margin: 15mm; }
          /* Hide EVERYTHING except our custom print overlay */
          body > *:not(#print-overlay-root) { display: none !important; }
          #print-overlay-root { display: block !important; }
          #print-overlay-root {
            position: static !important;
            display: block !important;
            height: auto !important;
            overflow: visible !important;
            background: white !important;
            padding: 0 !important;
          }
          #print-overlay-root * {
            visibility: visible !important;
            color: black !important;
            border-color: #ccc !important;
          }
          #print-overlay-root pre, #print-overlay-root code {
            background: #f5f5f5 !important;
            border: 1px solid #e5e5e5 !important;
          }
          #print-overlay-root a { text-decoration: underline !important; color: #0066cc !important; }
          .print-page-break { break-after: page; }
        }
      `;

      // 2. Render the print preview overlay
      setPrintOverlayData(printNotes);

      // 3. Trigger native print dialog after React renders the overlay
      setTimeout(() => {
        window.print();

        // 4. Cleanup after print
        window.onafterprint = () => {
          setPrintOverlayData(null);
          document.getElementById(styleId)?.remove();
          window.onafterprint = null;
        };

        // Fallback cleanup (in case onafterprint isn't supported)
        setTimeout(() => {
          setPrintOverlayData(null);
          document.getElementById(styleId)?.remove();
        }, 5000);
      }, 500); // 500ms to ensure MarkdownRenderer finishes rendering
    } catch (err) {
      showToast.error("Print Failed", "Could not prepare the document for printing.");
    }
  };

  const handleOpenNote = async (
    note: Note,
    targetMode: "preview" | "edit" = "preview"
  ) => {
    // Avoid reloading if it's the same note and already loaded
    if (selectedNote?.path === note.path && !isLoadingContent && viewMode === targetMode) {
      return;
    }

    setSelectedNote(note);
    setViewMode(targetMode);
    setIsLoadingContent(true);
    setPreviewContent(""); // Clear stale content immediately
    latestRequestRef.current = note.path;

    try {
      const fileContent = await workspaceApi.readFile(projectId, note.path);

      // Only update state if this is still the latest request
      if (latestRequestRef.current === note.path) {
        setPreviewContent(fileContent.content);
      }
    } catch (err) {
      if (latestRequestRef.current === note.path) {
        const errorMessage =
          err instanceof Error ? err.message : "Failed to load note";
        showToast.error("Load Failed", errorMessage);
      }
    } finally {
      if (latestRequestRef.current === note.path) {
        setIsLoadingContent(false);
      }
    }
  };

  // ---- Inline title / description editing ----------------------------------

  const handleStartEditMeta = (note: Note, e: React.MouseEvent) => {
    e.stopPropagation();
    setEditingPath(note.path);
    setEditingTitle(note.title);
    setEditingDescription(note.description);
  };

  const handleCancelEditMeta = () => {
    setEditingPath(null);
    setEditingTitle("");
    setEditingDescription("");
  };

  const handleSaveMeta = async () => {
    if (!editingPath) return;
    const target = notes.find((n) => n.path === editingPath);
    if (!target) {
      setEditingPath(null);
      return;
    }

    const newTitle = editingTitle.trim();
    const newDescription = editingDescription.trim();

    if (!newTitle) {
      showToast.warning("Invalid Title", "Title cannot be empty.");
      return;
    }

    if (newTitle === target.title && newDescription === target.description) {
      setEditingPath(null);
      return;
    }

    const updatedNote: Note = {
      ...target,
      title: newTitle,
      description: newDescription,
    };
    const updatedNotes = notes.map((n) =>
      n.path === editingPath ? updatedNote : n
    );

    setIsSavingMeta(true);
    try {
      await persistIndex(updatedNotes);
      setNotes(updatedNotes);
      if (selectedNote?.path === editingPath) {
        setSelectedNote(updatedNote);
      }
      setEditingPath(null);
      showToast.success("Note Updated", "Title and description saved successfully.");
    } catch (err) {
      console.error("[NotebookPage] Failed to update metadata:", err);
      const errorMessage =
        err instanceof Error && err.message
          ? err.message
          : "Failed to update note metadata. Please try again.";
      showToast.error("Update Failed", errorMessage);
    } finally {
      setIsSavingMeta(false);
    }
  };

  // ---- Derived data --------------------------------------------------------

  // Filter notes by search query (title + description + filename).
  const filteredNotes = useMemo(() => {
    if (!searchQuery.trim()) return notes;
    const q = searchQuery.toLowerCase();
    return notes.filter(
      (n) =>
        n.title.toLowerCase().includes(q) ||
        n.description.toLowerCase().includes(q) ||
        n.name.toLowerCase().includes(q)
    );
  }, [notes, searchQuery]);

  // Extract internal links from markdown content
  const extractInternalLinks = (content: string): string[] => {
    const linkRegex = /\[\[([^\]]+)\]\]/g;
    const links: string[] = [];
    let match;
    while ((match = linkRegex.exec(content)) !== null) {
      links.push(match[1]);
    }
    return [...new Set(links)];
  };

  const internalLinks = useMemo(() => {
    if (!selectedNote) return [];
    return extractInternalLinks(previewContent);
  }, [selectedNote, previewContent]);

  // Resolve [[note-name]] links for any content
  const resolveWikilinks = (content: string, currentNotes: Note[]) => {
    if (!content) return "";
    return content.replace(
      /\[\[([^\]]+)\]\]/g,
      (_match, noteName: string) => {
        const linkedNote = currentNotes.find((n) => n.name === noteName);
        if (linkedNote) {
          const label = linkedNote.title.replace(/[\[\]]/g, "");
          return `[${label}](#note-${encodeURIComponent(linkedNote.path)})`;
        }
        return `~~${noteName}~~`;
      }
    );
  };

  const resolvedPreviewContent = useMemo(() => {
    return resolveWikilinks(previewContent, notes);
  }, [previewContent, notes]);

  if (isLoading && notes.length === 0) {
    return (
      <div className="container mx-auto px-4 py-8">
        <div className="flex items-center justify-center h-64">
          <Loader2 size={32} className="animate-spin text-primary" />
        </div>
      </div>
    );
  }

  return (
    <div
      className={`flex h-full ${
        isFullscreen ? "fixed inset-0 z-50 bg-background" : ""
      }`}
    >
      {/* Notes List Sidebar */}
      {!isFullscreen && (
        <div className="w-80 border-r border-border bg-sidebg flex flex-col min-h-0">
          {/* Header */}
          <div className="p-4 border-b border-border">
            <div className="flex items-center justify-between mb-3">
              <h1 className="text-lg font-semibold flex items-center gap-2">
                <BookOpen className="text-primary" size={20} />
                Notebook
              </h1>
              <Button
                size="sm"
                variant="save"
                onClick={() => setShowCreateModal(true)}
                className="gap-1.5 text-[#fff]"
              >
                <Plus size={14} />
                New
              </Button>
            </div>

            {/* Search */}
            <div className="relative">
              <Search
                size={14}
                className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground"
              />
              <input
                type="text"
                placeholder="Search notes..."
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full h-8 pl-8 pr-2 text-sm bg-background border border-border rounded-md
                           focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
              />
            </div>
          </div>

          {/* Notes List */}
          <div className="flex-1 overflow-y-auto">
            {error && (
              <div className="m-3 p-3 bg-destructive/10 border border-destructive/30 rounded-md text-xs text-destructive">
                <AlertCircle size={14} className="inline mr-1" />
                {error}
              </div>
            )}

            {filteredNotes.length === 0 ? (
              <div className="p-6 text-center text-sm text-muted-foreground">
                {searchQuery ? "No notes found" : "No notes yet"}
                {!searchQuery && (
                  <p className="text-xs mt-2">
                    Create your first note to get started
                  </p>
                )}
              </div>
            ) : (
              <div className="py-2">
                {filteredNotes.map((note) => {
                  const isEditing = editingPath === note.path;
                  return (
                    <div
                      key={note.path}
                      className={`group flex items-start gap-2 px-3 py-2 mx-2 rounded-md cursor-pointer transition-colors ${
                        selectedNote?.path === note.path
                          ? "bg-primary/10 text-primary"
                          : "hover:bg-muted"
                      }`}
                      onClick={() => !isEditing && handleOpenNote(note, "preview")}
                    >
                      {isLoadingContent && selectedNote?.path === note.path ? (
                        <Loader2 size={14} className="shrink-0 mt-1 animate-spin text-primary" />
                      ) : (
                        <FileText size={14} className="shrink-0 mt-1" />
                      )}
                      <div className="flex-1 min-w-0">
                        {isEditing ? (
                          <div
                            className="space-y-1.5"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <input
                              autoFocus
                              type="text"
                              value={editingTitle}
                              onChange={(e) => setEditingTitle(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  handleSaveMeta();
                                } else if (e.key === "Escape") {
                                  e.preventDefault();
                                  handleCancelEditMeta();
                                }
                              }}
                              className="w-full text-sm bg-background border border-primary rounded px-1.5 py-0.5
                                         focus:outline-none focus:ring-1 focus:ring-primary/50"
                              placeholder="Title"
                              disabled={isSavingMeta}
                            />
                            <input
                              type="text"
                              value={editingDescription}
                              onChange={(e) =>
                                setEditingDescription(e.target.value)
                              }
                              onKeyDown={(e) => {
                                if (e.key === "Enter") {
                                  e.preventDefault();
                                  handleSaveMeta();
                                } else if (e.key === "Escape") {
                                  e.preventDefault();
                                  handleCancelEditMeta();
                                }
                              }}
                              className="w-full text-xs bg-background border border-border rounded px-1.5 py-0.5
                                         focus:outline-none focus:ring-1 focus:ring-primary/50"
                              placeholder="Short description (optional)"
                              disabled={isSavingMeta}
                            />
                            <div className="flex items-center gap-1">
                              <button
                                type="button"
                                onClick={handleSaveMeta}
                                disabled={
                                  isSavingMeta || !editingTitle.trim()
                                }
                                className="p-1 rounded bg-primary/10 text-primary hover:bg-primary/20 disabled:opacity-50"
                                title="Save (Enter)"
                              >
                                {isSavingMeta ? (
                                  <Loader2 size={12} className="animate-spin" />
                                ) : (
                                  <Check size={12} />
                                )}
                              </button>
                              <button
                                type="button"
                                onClick={handleCancelEditMeta}
                                disabled={isSavingMeta}
                                className="p-1 rounded hover:bg-muted disabled:opacity-50"
                                title="Cancel (Esc)"
                              >
                                <X size={12} />
                              </button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <div className="text-sm font-medium truncate">
                              {note.title}
                            </div>
                            {note.description && (
                              <div className="text-[11px] text-muted-foreground truncate">
                                {note.description}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                      {!isEditing && (
                        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity mt-0.5">
                          <button
                            onClick={(e) => handleStartEditMeta(note, e)}
                            className="p-1 hover:bg-background rounded"
                            title="Rename / edit description"
                          >
                            <Pencil size={12} />
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleOpenNote(note, "edit");
                            }}
                            className="p-1 hover:bg-background rounded"
                            title="Edit content"
                          >
                            <Edit size={12} />
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteNote(note);
                            }}
                            className="p-1 hover:bg-background rounded text-destructive"
                            title="Delete"
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Footer Stats */}
          <div className="p-3 border-t border-border text-xs text-muted-foreground">
            {notes.length} note{notes.length !== 1 ? "s" : ""}
          </div>
        </div>
      )}

      {/* Note Content Area */}
      <div className="flex-1 flex flex-col bg-background min-h-0">
        {!selectedNote ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center text-muted-foreground">
              <BookOpen size={48} className="mx-auto mb-4 opacity-50" />
              <p className="text-sm">Select a note to view or edit</p>
            </div>
          </div>
        ) : isLoadingContent ? (
          <div className="flex-1 flex flex-col items-center justify-center bg-card">
            <Loader2 size={32} className="animate-spin text-primary mb-4" />
            <p className="text-sm text-muted-foreground">Loading note...</p>
          </div>
        ) : viewMode === "edit" ? (
          <div className="flex-1 flex flex-col min-h-0">
            <div className="h-12 border-b border-border bg-muted px-4 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Edit size={14} className="text-muted-foreground" />
                <span className="text-sm font-medium">
                  Editing: {selectedNote.title}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setIsFullscreen((value) => !value)}
                  className="gap-1.5"
                  title={
                    isFullscreen ? "Exit fullscreen (Esc)" : "Fullscreen"
                  }
                >
                  {isFullscreen ? (
                    <Minimize2 size={14} />
                  ) : (
                    <Maximize2 size={14} />
                  )}
                  {isFullscreen ? "Exit" : "Fullscreen"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setIsFullscreen(false);
                    setViewMode("preview");
                  }}
                  className="gap-1.5"
                >
                  <Eye size={14} />
                  Preview
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setIsFullscreen(false);
                    setSelectedNote(null);
                    setViewMode("list");
                  }}
                >
                  <X size={14} />
                </Button>
              </div>
            </div>
            <div className="flex-1 min-h-0 overflow-hidden">
              <NotebookEditor
                key={selectedNote.path}
                projectId={projectId}
                filePath={selectedNote.path}
                initialContent={previewContent}
                onSave={(content) => {
                  // Title/description are managed separately in index.json.
                  setPreviewContent(content);
                }}
              />
            </div>
          </div>
        ) : (
          <div className="flex-1 flex flex-col min-h-0">
            <div className="h-12 border-b border-border bg-card px-4 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Eye size={14} className="text-muted-foreground" />
                <span className="text-sm font-medium">
                  {selectedNote.title}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setShowPrintModal(true)}
                  className="gap-1.5"
                >
                  <Printer size={14} />
                  Print
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setViewMode("edit")}
                  className="gap-1.5"
                >
                  <Edit size={14} />
                  Edit
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setSelectedNote(null);
                    setViewMode("list");
                  }}
                >
                  <X size={14} />
                </Button>
              </div>
            </div>

            {/* Preview Content */}
            <div className="flex-1 overflow-auto p-6 bg-card">
              <div
                id="notebook-print-area"
                className="max-w-7xl mx-auto"
                onClick={(e) => {
                  const target = (e.target as HTMLElement).closest("a");
                  const href = target?.getAttribute("href");
                  if (href && href.startsWith("#note-")) {
                    e.preventDefault();
                    const notePath = decodeURIComponent(
                      href.replace("#note-", "")
                    );
                    const linkedNote = notes.find(
                      (n) => n.path === notePath
                    );
                    if (linkedNote) {
                      handleOpenNote(linkedNote, "preview");
                    }
                  }
                }}
              >
                <MarkdownRenderer content={resolvedPreviewContent} />
              </div>

              {/* Internal Links Section */}
              {internalLinks.length > 0 && (
                <div className="max-w-3xl mx-auto mt-8 pt-6 border-t border-border">
                  <h3 className="text-sm font-semibold mb-3 flex items-center gap-2">
                    <LinkIcon size={14} />
                    Linked Notes
                  </h3>
                  <div className="flex flex-wrap gap-2">
                    {internalLinks.map((linkName) => {
                      const linkedNote = notes.find(
                        (n) => n.name === linkName
                      );
                      return (
                        <button
                          key={linkName}
                          onClick={() => {
                            if (linkedNote) {
                              handleOpenNote(linkedNote, "preview");
                            }
                          }}
                          className={`px-3 py-1.5 text-xs rounded-md border transition-colors ${
                            linkedNote
                              ? "bg-primary/10 border-primary/30 text-primary hover:bg-primary/20"
                              : "bg-muted/50 border-border text-muted-foreground cursor-not-allowed"
                          }`}
                          disabled={!linkedNote}
                        >
                          {linkedNote ? linkedNote.title : linkName}
                          {!linkedNote && " (missing)"}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Create Note Modal */}
      {showCreateModal && (
        <CreateNoteModal
          onClose={() => setShowCreateModal(false)}
          onCreate={handleCreateNote}
        />
      )}

      <ConfirmDialog />

      {/* Print Settings Modal */}
      {showPrintModal && (
        <PrintModal
          onClose={() => setShowPrintModal(false)}
          onPrint={handlePrint}
          hasSelectedNote={!!selectedNote}
        />
      )}

      {/* Dynamic Print Overlay - Uses createPortal to bypass app containers during print */}
      {printOverlayData && typeof document !== "undefined" && createPortal(
        <div id="print-overlay-root" className="hidden print:block bg-white text-black p-8 min-h-screen">
          <div className="max-w-4xl mx-auto space-y-8">
            {printOverlayData.map((item, i) => (
              <div
                key={item.note.path}
                className={i < printOverlayData.length - 1 ? "print-page-break pb-8 border-b border-gray-300" : ""}
              >
                <h1 className="text-2xl font-bold mb-2">{item.note.title}</h1>
                {item.note.description && (
                  <p className="text-gray-600 mb-6 text-sm">{item.note.description}</p>
                )}
                <div className="max-w-none">
                  <MarkdownRenderer content={resolveWikilinks(item.content, notes)} />
                </div>
              </div>
            ))}
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
// ----------------------------------------------------------------------------
// Create Note Modal
// ----------------------------------------------------------------------------

interface CreateNoteModalProps {
  onClose: () => void;
  /** Receives the desired title and optional description. */
  onCreate: (title: string, description: string) => Promise<void> | void;
}

function CreateNoteModal({ onClose, onCreate }: CreateNoteModalProps) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || isSubmitting) return;
    setIsSubmitting(true);
    try {
      await onCreate(title, description);
    } catch {
      // The parent already surfaced the error; keep the modal open.
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-card border border-border rounded-xl p-6 max-w-lg w-full shadow-xl">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold flex items-center gap-2">
            <Plus size={20} />
            Create New Note
          </h2>
          <Button variant="ghost" size="sm" onClick={onClose}>
            <X size={16} />
          </Button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">
              Title *
            </label>
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-1 focus:ring-rb/10"
              placeholder="My Thinking Mode"
              autoFocus
              required
              disabled={isSubmitting}
            />
            <p className="text-xs text-muted-foreground mt-1">
              Shown in the sidebar. The filename is generated automatically
              from the title and remains stable for internal links.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">
              Short description{" "}
              <span className="text-muted-foreground">(optional)</span>
            </label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-1 focus:ring-rb/10"
              placeholder="What is this note about?"
              disabled={isSubmitting}
            />
          </div>

          <div className="rounded-lg border border-card bg-muted p-3 text-xxs text-muted-foreground">
            The note will open directly inside the rich editor box with the
            fixed formatting toolbar and floating selection toolbar active.
          </div>

          <div className="flex gap-2 pt-2">
            <Button
              type="submit"
              className="flex-1"
              variant="save"
              disabled={isSubmitting || !title.trim()}
            >
              {isSubmitting ? (
                <>
                  <Loader2 size={14} className="animate-spin mr-1" />
                  Creating...
                </>
              ) : (
                "Create & Edit Note"
              )}
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={onClose}
              disabled={isSubmitting}
            >
              Cancel
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
