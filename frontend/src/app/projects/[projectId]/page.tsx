// frontend/src/app/projects/[projectId]/page.tsx

/**
 * Project Dashboard Page
 *
 * Main entry point for a specific project. Displays:
 * - Project metadata (name, description, workspace info)
 * - Quick statistics (sessions count, tasks count, last activity)
 * - Project workspace panel (right side)
 * - Navigation to sub-pages (chat, tasks, notebook, etc.)
 *
 * Design:
 * - Two-column layout: main content (left) + workspace panel (right)
 * - Responsive: workspace panel collapses on mobile
 * - Uses useProjectStore for project data
 * - Integrates with ProjectWorkspacePanel for file operations
 *
 * This page is wrapped by the projects layout.tsx which provides
 * the sidebar navigation.
 */

"use client";

import React, { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  Calendar,
  MessageSquare,
  CheckSquare,
  Folder,
  Settings,
  GitBranch,
  FileText,
  Loader2,
  ArrowLeft,
  Edit,
  Trash2,
  PanelRightClose,
  PanelRightOpen,
  FolderTree,
} from "lucide-react";
import { ProjectSourceControlPanel } from "@/features/source-control/components/project-source-control-panel";

import { Button } from "@/components/ui/button";
import { useProjectStore } from "@/stores/project-store";
import { ProjectWorkspacePanel } from "@/components/workspace/project-workspace-panel";
import { Project, WorkspaceType } from "@/features/workspace/types";

export default function ProjectDashboardPage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.projectId as string;

  const { getProject, loadProjects } = useProjectStore();

  // Helper to safely map store project to full Project type.
  //
  // Moved ABOVE the state initialiser so it can be used synchronously
  // during the first render. Arrow-function constants are NOT hoisted,
  // so a definition placed below `useState` would be `undefined` at
  // the moment the initialiser runs.
  const mapToFullProject = (storedProject: any): Project => ({
    ...storedProject,
    workspace_path: storedProject.workspace_path || "",
    workspace_type: (storedProject.workspace_type as WorkspaceType) || "internal",
    updated_at: storedProject.updated_at || storedProject.created_at,
  });

  // ── F5 / hard-reload fix ──────────────────────────────────────
  //
  // Initialise `project` SYNCHRONOUSLY from the store on the very
  // first render. This is critical for the hard-reload case:
  //
  //   * Client-side navigation: the store is already populated by
  //     the sidebar's initial load, so the first render can
  //     immediately show the project data.
  //
  //   * Hard reload (F5): the store starts empty, BUT the wrapping
  //     `ProjectLayoutClient` fetches the project BEFORE it renders
  //     its children — so by the time this component mounts, the
  //     project is already in the store. Reading it here avoids a
  //     one-frame flash of "Project Not Found".
  //
  // `hasResolved` distinguishes "we haven't checked yet" from "we
  // checked, and the project genuinely does not exist". Without it,
  // the previous check `!isLoading && !project` would spuriously
  // render "Not Found" during the brief window between mount and
  // the fetch effect resolving.
  const [project, setProject] = useState<Project | null>(() => {
    if (typeof window === "undefined") return null;
    const stored = useProjectStore.getState().getProject(projectId);
    return stored ? mapToFullProject(stored) : null;
  });
  const [hasResolved, setHasResolved] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return useProjectStore.getState().getProject(projectId) != null;
  });
  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");
  const [rightPanel, setRightPanel] = useState<"workspace" | "source-control" | null>("workspace");

  // Load project data only when we don't already have it.
  //
  // Because `ProjectLayoutClient` fetches the project before rendering
  // its children, `hasResolved` is usually `true` on mount (F5 case)
  // or immediately on first paint (client-nav case). This effect then
  // becomes a no-op, which:
  //
  //   * Avoids a redundant network round-trip.
  //   * Eliminates the double-fetch race that previously caused the
  //     "Project Not Found" UI to stick on hard reloads.
  //
  // The `cancelled` flag guards against setting state after the
  // component unmounts (e.g., user navigates away mid-fetch).
  useEffect(() => {
    if (!projectId || hasResolved) return;

    let cancelled = false;
    (async () => {
      try {
        await loadProjects(true);
      } catch (error) {
        console.error(
          "[ProjectDashboard] Failed to load projects:",
          error
        );
      }
      if (cancelled) return;

      const currentProject = getProject(projectId);
      setProject(
        currentProject ? mapToFullProject(currentProject) : null
      );
      setHasResolved(true);
    })();

    return () => {
      cancelled = true;
    };
  }, [projectId, getProject, loadProjects, hasResolved]);

  // Wait for initial resolution before deciding between "Not Found"
  // and "Loaded". This is the core F5 fix: without `hasResolved`, the
  // component rendered "Not Found" during the brief window between
  // mount and the fetch effect resolving.
  //
  // Two distinct states are handled here:
  //   1. `!hasResolved && !project` — we haven't checked yet (show
  //      loading, not "Not Found").
  //   2. `!project` (with `hasResolved === true` implied) — resolution
  //      completed and the project genuinely does not exist.
  if (!hasResolved && !project) {
    return (
      <div className="flex h-screen">
        <div className="flex-1 flex items-center justify-center">
          <Loader2 size={32} className="animate-spin text-primary" />
        </div>
      </div>
    );
  }

  // Resolution complete, project not found.
  if (!project) {
    return (
      <div className="container mx-auto px-4 py-16 max-w-2xl">
        <div className="bg-card border border-border rounded-lg p-8 text-center">
          <Folder size={48} className="mx-auto text-muted-foreground mb-4" />
          <h2 className="text-xl font-semibold mb-2">Project Not Found</h2>
          <p className="text-muted-foreground mb-6">
            The project you're looking for doesn't exist or has been deleted.
          </p>
          <Button onClick={() => router.push("/projects")}>
            <ArrowLeft size={16} className="mr-2" />
            Back to Projects
          </Button>
        </div>
      </div>
    );
  }

  const handleEdit = () => {
    setEditName(project.name);
    setEditDescription(project.description || "");
    setIsEditing(true);
  };

  const handleSave = async () => {
    // TODO: Implement project update API
    console.log("Save project:", { name: editName, description: editDescription });
    setIsEditing(false);
  };

  const handleDelete = async () => {
    if (!confirm(`Are you sure you want to delete "${project.name}"? This action cannot be undone.`)) {
      return;
    }

    try {
      // TODO: Implement project delete API
      console.log("Delete project:", projectId);
      router.push("/projects");
    } catch (error) {
      console.error("Failed to delete project:", error);
      alert("Failed to delete project. Please try again.");
    }
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleDateString("en-US", {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
  };

  return (
    <div className="flex h-screen">
      {/* Main Content Area */}
      <div className="flex-1 flex flex-col overflow-auto">
        {/* Header */}
        <div className="border-b border-border bg-card p-6">
          <div className="flex items-start justify-between mb-4">
            <div className="flex-1">
              {isEditing ? (
                <div className="space-y-3">
                  <input
                    type="text"
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    className="w-full text-2xl font-bold bg-background border border-border rounded-md px-3 py-2"
                    autoFocus
                  />
                  <textarea
                    value={editDescription}
                    onChange={(e) => setEditDescription(e.target.value)}
                    placeholder="Project description..."
                    className="w-full bg-background border border-border rounded-md px-3 py-2 text-sm"
                    rows={3}
                  />
                  <div className="flex gap-2">
                    <Button onClick={handleSave} size="sm">
                      Save
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setIsEditing(false)}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              ) : (
                <>
                  <h1 className="text-2xl font-bold mb-2">{project.name}</h1>
                  <p className="text-muted-foreground">
                    {project.description || "No description"}
                  </p>
                </>
              )}
            </div>

            {!isEditing && (
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="sm" onClick={handleEdit}>
                  <Edit size={16} className="mr-1" />
                  Edit
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-destructive hover:text-destructive"
                  onClick={handleDelete}
                >
                  <Trash2 size={16} className="mr-1" />
                  Delete
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => router.push(`/projects/${projectId}/settings`)}
                >
                  <Settings size={16} className="mr-1" />
                  Settings
                </Button>

                {/* Open Panel Button (visible only when panel is closed) */}
                {rightPanel === null && (
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setRightPanel("workspace")}
                    className="gap-1.5 ml-2"
                    title="Open Workspace / Source Control Panel"
                  >
                    <PanelRightOpen size={16} />
                    <span className="hidden md:inline">Open Panel</span>
                  </Button>
                )}
              </div>
            )}
          </div>

          {/* Project Stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div className="bg-muted/50 border border-border rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                <MessageSquare size={16} className="text-primary" />
                <span className="text-sm font-medium">Sessions</span>
              </div>
              <div className="text-2xl font-bold">0</div>
              <div className="text-xs text-muted-foreground mt-1">
                Active conversations
              </div>
            </div>

            <div className="bg-muted/50 border border-border rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                <CheckSquare size={16} className="text-primary" />
                <span className="text-sm font-medium">Tasks</span>
              </div>
              <div className="text-2xl font-bold">0</div>
              <div className="text-xs text-muted-foreground mt-1">
                Pending tasks
              </div>
            </div>

            <div className="bg-muted/50 border border-border rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                <Folder size={16} className="text-primary" />
                <span className="text-sm font-medium">Files</span>
              </div>
              <div className="text-2xl font-bold">0</div>
              <div className="text-xs text-muted-foreground mt-1">
                Workspace files
              </div>
            </div>

            <div className="bg-muted/50 border border-border rounded-lg p-4">
              <div className="flex items-center gap-2 mb-2">
                <Calendar size={16} className="text-primary" />
                <span className="text-sm font-medium">Created</span>
              </div>
              <div className="text-sm font-semibold">
                {formatDate(project.created_at)}
              </div>
              <div className="text-xs text-muted-foreground mt-1">
                Project creation date
              </div>
            </div>
          </div>
        </div>

        {/* Quick Actions */}
        <div className="p-6 border-b border-border">
          <h2 className="text-sm font-semibold mb-3 text-muted-foreground uppercase tracking-wider">
            Quick Actions
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Button
              variant="outline"
              className="h-auto py-4 flex flex-col items-start gap-2"
              onClick={() => router.push(`/projects/${projectId}/chat`)}
            >
              <MessageSquare size={20} className="text-primary" />
              <div className="text-left">
                <div className="font-semibold">Start Chat</div>
                <div className="text-xs text-muted-foreground">
                  Begin a new conversation
                </div>
              </div>
            </Button>

            <Button
              variant="outline"
              className="h-auto py-4 flex flex-col items-start gap-2"
              onClick={() => router.push(`/projects/${projectId}/tasks`)}
            >
              <CheckSquare size={20} className="text-primary" />
              <div className="text-left">
                <div className="font-semibold">Manage Tasks</div>
                <div className="text-xs text-muted-foreground">
                  View and organize tasks
                </div>
              </div>
            </Button>

            <Button
              variant="outline"
              className="h-auto py-4 flex flex-col items-start gap-2"
              onClick={() => router.push(`/projects/${projectId}/notebook`)}
            >
              <FileText size={20} className="text-primary" />
              <div className="text-left">
                <div className="font-semibold">Notebook</div>
                <div className="text-xs text-muted-foreground">
                  Project notes and docs
                </div>
              </div>
            </Button>

            <Button
              variant="outline"
              className="h-auto py-4 flex flex-col items-start gap-2"
              onClick={() => router.push(`/projects/${projectId}/source-control`)}
            >
              <GitBranch size={20} className="text-primary" />
              <div className="text-left">
                <div className="font-semibold">Source Control</div>
                <div className="text-xs text-muted-foreground">
                  Git repositories
                </div>
              </div>
            </Button>
          </div>
        </div>

        {/* Project Info */}
        <div className="p-6">
          <h2 className="text-sm font-semibold mb-3 text-muted-foreground uppercase tracking-wider">
            Project Information
          </h2>
          <div className="bg-muted/30 border border-border rounded-lg p-4 space-y-3">
            <div className="flex items-start gap-3">
              <Folder size={16} className="text-muted-foreground mt-0.5" />
              <div className="flex-1">
                <div className="text-sm font-medium">Workspace Path</div>
                <div className="text-xs text-muted-foreground font-mono mt-1 break-all">
                  {project.workspace_path || "Not configured"}
                </div>
              </div>
            </div>

            <div className="flex items-start gap-3">
              <Settings size={16} className="text-muted-foreground mt-0.5" />
              <div className="flex-1">
                <div className="text-sm font-medium">Workspace Type</div>
                <div className="text-xs text-muted-foreground mt-1 capitalize">
                  {project.workspace_type || "internal"}
                </div>
              </div>
            </div>

            <div className="flex items-start gap-3">
              <Calendar size={16} className="text-muted-foreground mt-0.5" />
              <div className="flex-1">
                <div className="text-sm font-medium">Last Updated</div>
                <div className="text-xs text-muted-foreground mt-1">
                  {project.updated_at ? formatDate(project.updated_at) : "Never"}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Right Panel (Workspace / Source Control) */}
      {rightPanel !== null && (
        <div className="w-[33.333333%] border-l border-border hidden lg:flex flex-col shrink-0 bg-background">
          {/* Toggle Header */}
          <div className="h-12 border-b border-border bg-card px-3 flex items-center justify-between shrink-0">
            <div className="flex items-center bg-muted/40 rounded-md border border-border p-0.5">
              <button
                type="button"
                onClick={() => setRightPanel("workspace")}
                className={`px-2.5 py-1.5 rounded-sm transition-all cursor-pointer flex items-center gap-1.5 text-xs ${
                  rightPanel === "workspace"
                    ? "bg-background text-foreground shadow-sm font-medium"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted/80"
                }`}
              >
                <FolderTree size={14} />
                <span>Workspace</span>
              </button>
              <button
                type="button"
                onClick={() => setRightPanel("source-control")}
                className={`px-2.5 py-1.5 rounded-sm transition-all cursor-pointer flex items-center gap-1.5 text-xs ${
                  rightPanel === "source-control"
                    ? "bg-background text-foreground shadow-sm font-medium"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted/80"
                }`}
              >
                <GitBranch size={14} />
                <span>Source Control</span>
              </button>
            </div>
            <button
              type="button"
              onClick={() => setRightPanel(null)}
              className="p-1.5 rounded-md text-muted-foreground hover:bg-muted hover:text-foreground transition-all cursor-pointer"
              title="Close Right Panel"
            >
              <PanelRightClose size={16} />
            </button>
          </div>

          {/* Panel Content */}
          <div className="flex-1 min-h-0 overflow-hidden">
            {rightPanel === "workspace" && (
              <ProjectWorkspacePanel projectId={projectId} />
            )}
            {rightPanel === "source-control" && (
              <ProjectSourceControlPanel projectId={projectId} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}
