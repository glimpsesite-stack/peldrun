// frontend/src/app/projects/[projectId]/settings/page.tsx

/**
 * Project Settings Page
 *
 * Provides comprehensive project configuration including:
 * - Basic info: name, description
 * - Workspace configuration: path, type (internal/external)
 * - Danger zone: delete project
 *
 * Design Decisions:
 * - Uses useProjectStore for state synchronization with sidebar
 * - Uses fetch directly for update operations
 * - Requires explicit confirmation for destructive actions
 * - Shows toast notifications for user feedback
 * - Separates "safe" settings from "dangerous" operations visually
 */

"use client";

import React, { useState, useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  Settings,
  Save,
  Trash2,
  Folder,
  Calendar,
  Clock,
  AlertTriangle,
  Loader2,
  ArrowLeft,
  ExternalLink,
  HardDrive,
  Info,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useProjectStore } from "@/stores/project-store";
import { useConfirmModal } from "@/components/ui/ConfirmModal";
import { showToast, ToastContainer } from "@/components/ui/ToastNotification";
// Import Project from chatsApi to match the store's type
import { Project } from "@/lib/chatsApi";
// Import WorkspaceType for the radio button typing
import { WorkspaceType } from "@/features/workspace/types";

export default function ProjectSettingsPage() {
  const params = useParams();
  const router = useRouter();
  const { confirm, ConfirmDialog } = useConfirmModal();
  const projectId = params.projectId as string;

  const { getProject, upsertProject, removeProject, loadProjects } = useProjectStore();

  // Form state
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [workspacePath, setWorkspacePath] = useState("");
  const [workspaceType, setWorkspaceType] = useState<WorkspaceType>("internal");

  // UI state
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [project, setProject] = useState<Project | null>(null);
  const [hasChanges, setHasChanges] = useState(false);

  // Load project data
  useEffect(() => {
    const loadProjectData = async () => {
      if (!projectId) return;

      // Try to get from store first
      let currentProject = getProject(projectId);

      // If not in store, load from backend
      if (!currentProject) {
        await loadProjects(true);
        currentProject = getProject(projectId);
      }

      if (currentProject) {
        setProject(currentProject);
        setName(currentProject.name);
        setDescription(currentProject.description || "");
        setWorkspacePath(currentProject.workspace_path || "");
        setWorkspaceType(currentProject.workspace_type || "internal");
      }

      setIsLoading(false);
    };

    loadProjectData();
  }, [projectId, getProject, loadProjects]);

  // Track changes
  useEffect(() => {
    if (!project) return;

    const changed =
      name !== project.name ||
      description !== (project.description || "") ||
      workspacePath !== (project.workspace_path || "") ||
      workspaceType !== (project.workspace_type || "internal");

    setHasChanges(changed);
  }, [name, description, workspacePath, workspaceType, project]);

  // Handle save settings
  const handleSave = async () => {
    if (!projectId || !name.trim()) {
      showToast.error("Validation Error", "Project name cannot be empty.");
      return;
    }

    setIsSaving(true);

    try {
      const res = await fetch(`/api/projects/${projectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim(),
          workspace_path: workspacePath.trim(),
          workspace_type: workspaceType,
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}`);
      }

      const updatedProject = await res.json();

      // Update local state - merge with existing project data
      const fullProject: Project = {
        ...project!,
        ...updatedProject,
        name: name.trim(),
        description: description.trim(),
        workspace_path: workspacePath.trim(),
        workspace_type: workspaceType,
        updated_at: new Date().toISOString(),
      };

      setProject(fullProject);
      upsertProject(fullProject);

      showToast.success("Settings Saved", "Project settings have been updated successfully.");
    } catch (err: any) {
      console.error("[ProjectSettings] Save failed:", err);
      showToast.error("Save Failed", err?.message || "Could not save project settings.");
    } finally {
      setIsSaving(false);
    }
  };

  // Handle delete project
  const handleDelete = async () => {
    if (!project) return;

    const ok = await confirm({
      title: "Delete Project",
      description: `Are you sure you want to permanently delete "${project.name}"? This will delete all sessions, files, and settings. This action CANNOT be undone.`,
      variant: "danger",
      confirmText: "Delete Project",
    });

    if (!ok) return;

    setIsDeleting(true);

    try {
      const res = await fetch(`/api/projects/${projectId}`, {
        method: "DELETE",
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      // Remove from store
      removeProject(projectId);

      showToast.success("Project Deleted", `"${project.name}" has been permanently deleted.`);

      // Navigate back to projects list
      router.push("/projects");
    } catch (err: any) {
      console.error("[ProjectSettings] Delete failed:", err);
      showToast.error("Delete Failed", err?.message || "Could not delete project.");
    } finally {
      setIsDeleting(false);
    }
  };

  // Format date helper
  const formatDate = (dateString?: string) => {
    if (!dateString) return "Never";
    const date = new Date(dateString);
    return date.toLocaleDateString("en-US", {
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  // Loading state
  if (isLoading) {
    return (
      <div className="container mx-auto px-4 py-8 max-w-4xl">
        <div className="flex items-center justify-center h-64">
          <Loader2 size={32} className="animate-spin text-primary" />
        </div>
      </div>
    );
  }

  // Project not found
  if (!project) {
    return (
      <div className="container mx-auto px-4 py-16 max-w-2xl">
        <div className="bg-card border border-border rounded-lg p-8 text-center">
          <AlertTriangle size={48} className="mx-auto text-destructive mb-4" />
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

  return (
    <div className="container mx-auto px-4 py-8 max-w-4xl">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => router.push(`/projects/${projectId}`)}
            className="gap-1.5"
          >
            <ArrowLeft size={16} />
            Back
          </Button>
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <Settings className="text-primary" size={24} />
              Project Settings
            </h1>
            <p className="text-muted-foreground mt-1">
              Configure your project's basic information and workspace.
            </p>
          </div>
        </div>

        {hasChanges && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-amber-600 dark:text-amber-400">
              Unsaved changes
            </span>
            <Button onClick={handleSave} disabled={isSaving} className="gap-2">
              {isSaving ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <Save size={16} />
              )}
              Save Changes
            </Button>
          </div>
        )}
      </div>

      {/* Project Information (Read-only) */}
      <section className="mb-8 bg-card border border-border rounded-lg p-6">
        <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
          <Info size={18} className="text-primary" />
          Project Information
        </h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-muted/30 border border-border rounded-md p-3">
            <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
              <Folder size={14} />
              <span>Project ID</span>
            </div>
            <div className="font-mono text-sm">{project.id}</div>
          </div>
          <div className="bg-muted/30 border border-border rounded-md p-3">
            <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
              <Calendar size={14} />
              <span>Created</span>
            </div>
            <div className="text-sm">{formatDate(project.created_at)}</div>
          </div>
          <div className="bg-muted/30 border border-border rounded-md p-3">
            <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
              <Clock size={14} />
              <span>Last Updated</span>
            </div>
            <div className="text-sm">{formatDate(project.updated_at)}</div>
          </div>
          <div className="bg-muted/30 border border-border rounded-md p-3">
            <div className="flex items-center gap-2 text-sm text-muted-foreground mb-1">
              <HardDrive size={14} />
              <span>Workspace Type</span>
            </div>
            <div className="text-sm capitalize">{project.workspace_type || "internal"}</div>
          </div>
        </div>
      </section>

      {/* Basic Settings */}
      <section className="mb-8 bg-card border border-border rounded-lg p-6">
        <h2 className="text-lg font-semibold mb-4">Basic Information</h2>

        <div className="space-y-4">
          {/* Project Name */}
          <div>
            <label className="block text-sm font-medium mb-2">
              Project Name <span className="text-destructive">*</span>
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
              placeholder="My Awesome Project"
              required
            />
            <p className="text-xs text-muted-foreground mt-1">
              A descriptive name for your project. This will be displayed in the sidebar.
            </p>
          </div>

          {/* Project Description */}
          <div>
            <label className="block text-sm font-medium mb-2">
              Description
            </label>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md
                         focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
              placeholder="A brief description of what this project is about..."
              rows={3}
            />
            <p className="text-xs text-muted-foreground mt-1">
              Optional description to help you remember the purpose of this project.
            </p>
          </div>
        </div>
      </section>

      {/* Workspace Configuration */}
      <section className="mb-8 bg-card border border-border rounded-lg p-6">
        <h2 className="text-lg font-semibold mb-4 flex items-center gap-2">
          <HardDrive size={18} className="text-primary" />
          Workspace Configuration
        </h2>

        <div className="space-y-4">
          {/* Workspace Path */}
          <div>
            <label className="block text-sm font-medium mb-2">
              Workspace Path
            </label>
            <input
              type="text"
              value={workspacePath}
              onChange={(e) => setWorkspacePath(e.target.value)}
              className="w-full px-3 py-2 bg-background border border-border rounded-md font-mono text-sm
                         focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary"
              placeholder="/path/to/project/workspace"
            />
            <p className="text-xs text-muted-foreground mt-1">
              The absolute path to the project's workspace directory on disk.
              Leave empty to use the default internal workspace.
            </p>
            {workspacePath && (
              <div className="mt-2 p-2 bg-muted/30 border border-border rounded-md flex items-center gap-2">
                <ExternalLink size={12} className="text-muted-foreground" />
                <span className="text-xs font-mono text-muted-foreground truncate">
                  {workspacePath}
                </span>
              </div>
            )}
          </div>

          {/* Workspace Type */}
          <div>
            <label className="block text-sm font-medium mb-2">
              Workspace Type
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label
                className={`flex flex-col p-4 border rounded-md cursor-pointer transition-all ${
                  workspaceType === "internal"
                    ? "border-primary bg-primary/5"
                    : "border-border hover:border-primary/50"
                }`}
              >
                <input
                  type="radio"
                  name="workspaceType"
                  value="internal"
                  checked={workspaceType === "internal"}
                  onChange={() => setWorkspaceType("internal")}
                  className="sr-only"
                />
                <div className="flex items-center gap-2 mb-1">
                  <HardDrive size={16} className="text-primary" />
                  <span className="font-medium">Internal</span>
                </div>
                <p className="text-xs text-muted-foreground">
                  Workspace is managed within the PELDRUN storage directory.
                </p>
              </label>

              <label
                className={`flex flex-col p-4 border rounded-md cursor-pointer transition-all ${
                  workspaceType === "external"
                    ? "border-primary bg-primary/5"
                    : "border-border hover:border-primary/50"
                }`}
              >
                <input
                  type="radio"
                  name="workspaceType"
                  value="external"
                  checked={workspaceType === "external"}
                  onChange={() => setWorkspaceType("external")}
                  className="sr-only"
                />
                <div className="flex items-center gap-2 mb-1">
                  <ExternalLink size={16} className="text-primary" />
                  <span className="font-medium">External</span>
                </div>
                <p className="text-xs text-muted-foreground">
                  Workspace points to an external directory on your filesystem.
                </p>
              </label>
            </div>
          </div>
        </div>
      </section>

      {/* Save Button (Bottom) */}
      {hasChanges && (
        <div className="mb-8 flex justify-end">
          <Button onClick={handleSave} disabled={isSaving} className="gap-2">
            {isSaving ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Save size={16} />
            )}
            Save Changes
          </Button>
        </div>
      )}

      {/* Danger Zone */}
      <section className="bg-destructive/5 border-2 border-destructive/30 rounded-lg p-6">
        <h2 className="text-lg font-semibold mb-2 flex items-center gap-2 text-destructive">
          <AlertTriangle size={18} />
          Danger Zone
        </h2>
        <p className="text-sm text-muted-foreground mb-4">
          Destructive actions cannot be undone. Please be certain.
        </p>

        <div className="flex items-center justify-between p-4 bg-background border border-destructive/30 rounded-md">
          <div>
            <h3 className="font-semibold text-sm">Delete this project</h3>
            <p className="text-xs text-muted-foreground mt-1">
              Once you delete a project, there is no going back. This will permanently delete:
            </p>
            <ul className="text-xs text-muted-foreground mt-1 list-disc list-inside space-y-0.5">
              <li>All chat sessions within the project</li>
              <li>All workspace files and artifacts</li>
              <li>All tasks and notebook entries</li>
              <li>Project settings and metadata</li>
            </ul>
          </div>
          <Button
            variant="destructive"
            onClick={handleDelete}
            disabled={isDeleting}
            className="gap-2 shrink-0 ml-4"
          >
            {isDeleting ? (
              <Loader2 size={16} className="animate-spin" />
            ) : (
              <Trash2 size={16} />
            )}
            Delete Project
          </Button>
        </div>
      </section>

      {/* Dialogs */}
      <ConfirmDialog />
      <ToastContainer />
    </div>
  );
}
