// frontend/src/app/projects/[projectId]/notebook/page.tsx

/**
 * Project Notebook Page - NotebookView
 *
 * Manages Markdown-based notes within a project workspace.
 * Notes are stored in the `notebook/` directory of the project workspace.
 *
 * Features:
 * - List all Markdown files in the notebook directory
 * - Create new notes with Markdown content
 * - Edit notes with OCC (Optimistic Concurrency Control) support
 * - Delete notes with confirmation
 * - Preview rendered Markdown
 * - Support for internal links between notes ([[note-name]] syntax)
 * - Search notes by title and content
 *
 * Design Decisions:
 * - Uses workspaceApi for all file operations
 * - Integrates with OCCFileEditor for safe concurrent editing
 * - Notes are stored as .md files in the workspace's notebook/ directory
 * - Internal links use [[note-name]] syntax and are resolved to file paths
 */

"use client";

import React, { useState, useEffect, useCallback, useMemo } from "react";
import { useParams, useRouter } from "next/navigation";
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
  Clock,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { workspaceApi } from "@/features/workspace/workspace-api";
import { OCCConflictError } from "@/features/workspace/types";
import { OCCFileEditor } from "@/features/workspace/occ-file-editor";

const NOTEBOOK_DIR = "notebook";

interface Note {
  name: string;
  path: string;
  lastModified?: string;
}

export default function NotebookPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.projectId as string;

  const [notes, setNotes] = useState<Note[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedNote, setSelectedNote] = useState<Note | null>(null);
  const [viewMode, setViewMode] = useState<"list" | "edit" | "preview">("list");
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [previewContent, setPreviewContent] = useState<string>("");

  // Load notes on mount
  useEffect(() => {
    loadNotes();
  }, [projectId]);

  const loadNotes = async () => {
    setIsLoading(true);
    setError(null);

    try {
      const files = await workspaceApi.listFiles(projectId, NOTEBOOK_DIR);
      const markdownFiles = files
        .filter((f) => f.endsWith(".md"))
        .map((f) => ({
          name: f.split("/").pop()?.replace(".md", "") || f,
          path: f,
        }));
      setNotes(markdownFiles);
    } catch (err) {
      // If the notebook directory doesn't exist, create it
      if (err instanceof Error && err.message.includes("404")) {
        setNotes([]);
      } else {
        const errorMessage = err instanceof Error ? err.message : "Failed to load notes";
        setError(errorMessage);
        console.error("[NotebookPage] Failed to load notes:", err);
      }
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreateNote = async (name: string, content: string) => {
    const safeName = name.trim().replace(/[^a-zA-Z0-9-_ ]/g, "").replace(/\s+/g, "-");
    if (!safeName) {
      alert("Invalid note name. Use letters, numbers, hyphens, and underscores only.");
      return;
    }

    const notePath = `${NOTEBOOK_DIR}/${safeName}.md`;

    try {
      // Create the note with OCC (expected_version is empty for new files)
      await workspaceApi.updateFile(projectId, {
        path: notePath,
        content,
        expected_version: "",
      });

      setShowCreateModal(false);
      await loadNotes();

      // Select the newly created note
      const newNote = { name: safeName, path: notePath };
      setSelectedNote(newNote);
      setViewMode("edit");
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to create note";
      alert(errorMessage);
    }
  };

  const handleDeleteNote = async (note: Note) => {
    if (!confirm(`Are you sure you want to delete "${note.name}"? This action cannot be undone.`)) {
      return;
    }

    try {
      await workspaceApi.deleteFile(projectId, note.path);
      if (selectedNote?.path === note.path) {
        setSelectedNote(null);
        setViewMode("list");
      }
      await loadNotes();
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to delete note";
      alert(errorMessage);
    }
  };

  const handleSelectNote = async (note: Note) => {
    setSelectedNote(note);
    setViewMode("preview");

    try {
      const fileContent = await workspaceApi.readFile(projectId, note.path);
      setPreviewContent(fileContent.content);
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Failed to load note";
      alert(errorMessage);
    }
  };

  const handleSaveNote = async (content: string) => {
    if (!selectedNote) return;

    try {
      const response = await workspaceApi.updateFile(projectId, {
        path: selectedNote.path,
        content,
        expected_version: "", // Will be updated by OCCFileEditor
      });
      setPreviewContent(content);
    } catch (err) {
      if (err instanceof OCCConflictError) {
        alert("This note has been modified by another user. Please reload and merge your changes.");
      } else {
        const errorMessage = err instanceof Error ? err.message : "Failed to save note";
        alert(errorMessage);
      }
    }
  };

  // Filter notes by search query
  const filteredNotes = useMemo(() => {
    if (!searchQuery.trim()) return notes;
    const query = searchQuery.toLowerCase();
    return notes.filter((note) => note.name.toLowerCase().includes(query));
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

  // Render markdown to HTML (basic implementation)
  const renderMarkdown = (content: string): string => {
    let html = content
      // Headers
      .replace(/^### (.*$)/gim, '<h3 class="text-lg font-semibold mt-4 mb-2">$1</h3>')
      .replace(/^## (.*$)/gim, '<h2 class="text-xl font-semibold mt-5 mb-3">$1</h2>')
      .replace(/^# (.*$)/gim, '<h1 class="text-2xl font-bold mt-6 mb-4">$1</h1>')
      // Bold and italic
      .replace(/\*\*\*(.*?)\*\*\*/gim, '<strong><em>$1</em></strong>')
      .replace(/\*\*(.*?)\*\*/gim, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/gim, '<em>$1</em>')
      // Code blocks
      .replace(/```(\w+)?\n([\s\S]*?)```/gim, '<pre class="bg-muted p-3 rounded-md overflow-x-auto my-3"><code>$2</code></pre>')
      // Inline code
      .replace(/`([^`]+)`/gim, '<code class="bg-muted px-1.5 py-0.5 rounded text-sm">$1</code>')
      // Links
      .replace(/\[([^\]]+)\]\(([^)]+)\)/gim, '<a href="$2" class="text-primary hover:underline" target="_blank" rel="noopener noreferrer">$1</a>')
      // Internal links
      .replace(/\[\[([^\]]+)\]\]/gim, (match, noteName) => {
        const linkedNote = notes.find((n) => n.name === noteName);
        if (linkedNote) {
          return `<a href="#" onclick="window.selectNote('${linkedNote.path}'); return false;" class="text-primary hover:underline font-medium">$1</a>`;
        }
        return `<span class="text-muted-foreground line-through">$1</span>`;
      })
      // Lists
      .replace(/^\s*-\s+(.*)$/gim, '<li class="ml-4">$1</li>')
      .replace(/^\s*\d+\.\s+(.*)$/gim, '<li class="ml-4 list-decimal">$1</li>')
      // Paragraphs
      .replace(/\n\n/gim, '</p><p class="mb-3">')
      // Line breaks
      .replace(/\n/gim, '<br/>');

    return `<div class="prose prose-sm max-w-none">${html}</div>`;
  };

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
    <div className="flex h-full">
      {/* Notes List Sidebar */}
      <div className="w-80 border-r border-border bg-card flex flex-col">
        {/* Header */}
        <div className="p-4 border-b border-border">
          <div className="flex items-center justify-between mb-3">
            <h1 className="text-lg font-semibold flex items-center gap-2">
              <BookOpen className="text-primary" size={20} />
              Notebook
            </h1>
            <Button
              size="sm"
              onClick={() => setShowCreateModal(true)}
              className="gap-1.5"
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
              {filteredNotes.map((note) => (
                <div
                  key={note.path}
                  className={`group flex items-center gap-2 px-3 py-2 mx-2 rounded-md cursor-pointer transition-colors ${
                    selectedNote?.path === note.path
                      ? "bg-primary/10 text-primary"
                      : "hover:bg-muted"
                  }`}
                  onClick={() => handleSelectNote(note)}
                >
                  <FileText size={14} className="shrink-0" />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium truncate">
                      {note.name}
                    </div>
                  </div>
                  <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setSelectedNote(note);
                        setViewMode("edit");
                      }}
                      className="p-1 hover:bg-background rounded"
                      title="Edit"
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
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Footer Stats */}
        <div className="p-3 border-t border-border text-xs text-muted-foreground">
          {notes.length} note{notes.length !== 1 ? "s" : ""}
        </div>
      </div>

      {/* Note Content Area */}
      <div className="flex-1 flex flex-col bg-background">
        {!selectedNote ? (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center text-muted-foreground">
              <BookOpen size={48} className="mx-auto mb-4 opacity-50" />
              <p className="text-sm">Select a note to view or edit</p>
            </div>
          </div>
        ) : viewMode === "edit" ? (
          <div className="flex-1 flex flex-col">
            <div className="h-12 border-b border-border bg-card px-4 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Edit size={14} className="text-muted-foreground" />
                <span className="text-sm font-medium">Editing: {selectedNote.name}</span>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setViewMode("preview")}
                  className="gap-1.5"
                >
                  <Eye size={14} />
                  Preview
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
            <div className="flex-1 overflow-auto">
              <OCCFileEditor
                projectId={projectId}
                filePath={selectedNote.path}
                onSave={(response) => {
                  setPreviewContent(response.content);
                }}
              />
            </div>
          </div>
        ) : (
          <div className="flex-1 flex flex-col">
            <div className="h-12 border-b border-border bg-card px-4 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Eye size={14} className="text-muted-foreground" />
                <span className="text-sm font-medium">{selectedNote.name}</span>
              </div>
              <div className="flex items-center gap-2">
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
            <div className="flex-1 overflow-auto p-6">
              <div
                className="max-w-3xl mx-auto"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(previewContent) }}
              />

              {/* Internal Links Section */}
              {internalLinks.length > 0 && (
                <div className="max-w-3xl mx-auto mt-8 pt-6 border-t border-border">
                  <h3 className="text-sm font-semibold mb-3 flex items-center gap-2">
                    <LinkIcon size={14} />
                    Linked Notes
                  </h3>
                  <div className="flex flex-wrap gap-2">
                    {internalLinks.map((linkName) => {
                      const linkedNote = notes.find((n) => n.name === linkName);
                      return (
                        <button
                          key={linkName}
                          onClick={() => {
                            if (linkedNote) {
                              handleSelectNote(linkedNote);
                            }
                          }}
                          className={`px-3 py-1.5 text-xs rounded-md border transition-colors ${
                            linkedNote
                              ? "bg-primary/10 border-primary/30 text-primary hover:bg-primary/20"
                              : "bg-muted/50 border-border text-muted-foreground cursor-not-allowed"
                          }`}
                          disabled={!linkedNote}
                        >
                          {linkName}
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
    </div>
  );
}

// ============================================================================
// Create Note Modal
// ============================================================================

interface CreateNoteModalProps {
  onClose: () => void;
  onCreate: (name: string, content: string) => void;
}

function CreateNoteModal({ onClose, onCreate }: CreateNoteModalProps) {
  const [name, setName] = useState("");
  const [content, setContent] = useState("# New Note\n\nStart writing here...\n");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    onCreate(name, content);
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-card border border-border rounded-lg p-6 max-w-2xl w-full max-h-[80vh] overflow-auto">
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
            <label className="block text-sm font-medium mb-1">Note Name *</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50"
              placeholder="my-note-name"
              autoFocus
              required
            />
            <p className="text-xs text-muted-foreground mt-1">
              Use letters, numbers, hyphens, and underscores. Spaces will be converted to hyphens.
            </p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">Content</label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono text-sm"
              rows={12}
            />
            <p className="text-xs text-muted-foreground mt-1">
              Supports Markdown. Use [[note-name]] for internal links.
            </p>
          </div>

          <div className="flex gap-2 pt-4">
            <Button type="submit" className="flex-1">
              Create Note
            </Button>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
