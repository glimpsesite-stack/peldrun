// frontend/src/app/projects/new/page.tsx

/**
 * Create New Project Page
 *
 * Provides a focused form for creating a new project workspace.
 * On successful creation, redirects the user to the new project's dashboard.
 *
 * Design Decisions:
 * - Uses `createProject` from chatsApi (existing, battle-tested function)
 * - Updates useProjectStore immediately for sidebar consistency
 * - Shows toast notifications for success/error feedback
 * - Validates project name client-side before submission
 * - Disables submit button during creation to prevent double-submission
 * - Cancel button navigates back to the projects list
 *
 * This page is intentionally simple — advanced workspace configuration
 * (path, type) can be done later in the project's Settings page.
 */

"use client";

import React, { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  Plus,
  Folder,
  ArrowLeft,
  Loader2,
  AlertCircle,
  CheckCircle2,
  HardDrive,
  ExternalLink,
  FolderOpen,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { createProject } from "@/lib/chatsApi";
import { useProjectStore } from "@/stores/project-store";
import { showToast, ToastContainer } from "@/components/ui/ToastNotification";
import { DirectoryPicker } from "@/components/ui/directory-picker";

export default function NewProjectPage() {
  const router = useRouter();
  const { upsertProject } = useProjectStore();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [workspaceType, setWorkspaceType] =
    useState<"internal" | "external">("internal");
  const [workspacePath, setWorkspacePath] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  // Auto-focus the name input on mount
  useEffect(() => {
    const input = document.getElementById("project-name-input");
    if (input) {
      input.focus();
    }
  }, []);

  // Client-side validation
  const validate = (): boolean => {
    const trimmedName = name.trim();

    if (!trimmedName) {
      setValidationError("Project name is required.");
      return false;
    }

    if (trimmedName.length < 2) {
      setValidationError("Project name must be at least 2 characters long.");
      return false;
    }

    if (trimmedName.length > 100) {
      setValidationError("Project name must be 100 characters or less.");
      return false;
    }

    // External workspace requires a non-empty path. The backend
    // will additionally verify the path exists (or can be created).
    if (workspaceType === "external" && !workspacePath.trim()) {
      setValidationError(
        "Select a workspace folder, or switch back to Internal."
      );
      return false;
    }

    setValidationError(null);
    return true;
  };

  // Handle form submission
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!validate()) return;

    setIsCreating(true);

    try {
      const newProject = await createProject(
        name.trim(),
        description.trim(),
        workspaceType === "external" ? workspacePath.trim() : "",
        workspaceType,
      );

      if (!newProject) {
        throw new Error("Server did not return the created project.");
      }

      // Update the store immediately so the sidebar reflects the new project
      upsertProject(newProject);

      showToast.success(
        "Project Created",
        `"${newProject.name}" is ready to use.`
      );

      // Navigate to the new project's dashboard
      router.push(`/projects/${newProject.id}`);
    } catch (err: any) {
      // Backend path-validation errors (e.g. "Cannot create or access
      // external workspace …") arrive here verbatim — surface them in
      // the toast so the user knows exactly what failed.
      console.error("[NewProjectPage] Creation failed:", err);
      showToast.error(
        "Creation Failed",
        err?.message || "Could not create the project. Please try again."
      );
    } finally {
      setIsCreating(false);
    }
  };

  // Handle cancel
  const handleCancel = () => {
    router.push("/projects");
  };

  // Handle keyboard shortcut (Ctrl/Cmd + Enter to submit)
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      handleSubmit(e as any);
    }
  };

  return (
    <div className="container mx-auto px-4 py-8 max-w-2xl">
      {/* Header */}
      <div className="flex items-center gap-3 mb-8">
        <Button
          variant="ghost"
          size="sm"
          onClick={handleCancel}
          className="gap-1.5"
        >
          <ArrowLeft size={16} />
          Back
        </Button>
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            <Plus className="text-primary" size={24} />
            Create New Project
          </h1>
          <p className="text-muted-foreground mt-1">
            Set up a new workspace to organize your work, sessions, and files.
          </p>
        </div>
      </div>

      {/* Form Card */}
      <form
        onSubmit={handleSubmit}
        onKeyDown={handleKeyDown}
        className="bg-card border border-border rounded-lg p-6 shadow-sm"
      >
        {/* Project Name */}
        <div className="mb-6">
          <label
            htmlFor="project-name-input"
            className="block text-sm font-medium mb-2"
          >
            Project Name <span className="text-destructive">*</span>
          </label>
          <input
            id="project-name-input"
            type="text"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              if (validationError) setValidationError(null);
            }}
            className={`w-full px-3 py-2 bg-background border rounded-md
                       focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
                       ${validationError ? "border-destructive" : "border-border"}`}
            placeholder="My Awesome Project"
            disabled={isCreating}
            maxLength={100}
            autoFocus
          />
          <p className="text-xs text-muted-foreground mt-1.5">
            A descriptive name for your project. This will appear in the sidebar
            and throughout the application.
          </p>
        </div>

        {/* Project Description */}
        <div className="mb-6">
          <label
            htmlFor="project-description-input"
            className="block text-sm font-medium mb-2"
          >
            Description <span className="text-muted-foreground text-xs">(optional)</span>
          </label>
          <textarea
            id="project-description-input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full px-3 py-2 bg-background border border-border rounded-md
                       focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
                       resize-none"
            placeholder="A brief description of what this project is about..."
            rows={3}
            disabled={isCreating}
            maxLength={500}
          />
          <p className="text-xs text-muted-foreground mt-1.5">
            Optional. Helps you remember the purpose of this project later.
          </p>
        </div>

        {/* Workspace Configuration */}
        <div className="mb-6">
          <label className="block text-sm font-medium mb-2">
            Workspace Location
          </label>

          {/* Type selector */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
            {(
              [
                {
                  value: "internal" as const,
                  label: "Internal",
                  desc: "Stored under the app's project directory. Recommended.",
                  Icon: HardDrive,
                },
                {
                  value: "external" as const,
                  label: "External",
                  desc: "Point to any folder on your machine.",
                  Icon: ExternalLink,
                },
              ]
            ).map(({ value, label, desc, Icon }) => {
              const isActive = workspaceType === value;
              return (
                <label
                  key={value}
                  className={`relative flex flex-col p-3 border rounded-md cursor-pointer transition-colors ${
                    isActive
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-primary/40"
                  } ${isCreating ? "opacity-60 pointer-events-none" : ""}`}
                >
                  <input
                    type="radio"
                    name="workspaceType"
                    value={value}
                    checked={isActive}
                    onChange={() => setWorkspaceType(value)}
                    className="sr-only"
                    disabled={isCreating}
                  />
                  <div className="flex items-center gap-2 mb-1">
                    <Icon
                      size={14}
                      className={
                        isActive ? "text-primary" : "text-muted-foreground"
                      }
                    />
                    <span className="font-medium text-sm">{label}</span>
                  </div>
                  <p className="text-xs text-muted-foreground leading-relaxed">
                    {desc}
                  </p>
                </label>
              );
            })}
          </div>

          {/* External path input */}
          {workspaceType === "external" && (
            <div>
              <label className="block text-xs font-medium mb-1.5 text-muted-foreground">
                Folder Path
              </label>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={workspacePath}
                  onChange={(e) => setWorkspacePath(e.target.value)}
                  className="min-w-0 flex-1 px-3 py-2 bg-background border border-border rounded-md font-mono text-xs
                             focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary
                             transition-shadow placeholder:text-muted-foreground/60"
                  placeholder="/path/to/your/folder  (or use Browse…)"
                  disabled={isCreating}
                />
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => setPickerOpen(true)}
                  disabled={isCreating}
                  className="gap-1.5 shrink-0 h-[38px] px-3"
                >
                  <FolderOpen size={13} />
                  Browse…
                </Button>
              </div>
              <p className="text-xs text-muted-foreground mt-1.5">
                The folder will be used as the project's workspace. If it
                does not exist yet, it will be created.
              </p>
            </div>
          )}

          {workspaceType === "internal" && (
            <p className="text-xs text-muted-foreground">
              Your workspace will live inside the PELDRUN data directory,
              managed automatically.
            </p>
          )}
        </div>

        {/* Validation Error */}
        {validationError && (
          <div className="mb-6 p-3 bg-destructive/10 border border-destructive/30 rounded-md flex items-start gap-2">
            <AlertCircle
              className="text-destructive shrink-0 mt-0.5"
              size={16}
            />
            <p className="text-sm text-destructive">{validationError}</p>
          </div>
        )}

        {/* Info Box */}
        <div className="mb-6 p-3 bg-muted/30 border border-border rounded-md">
          <div className="flex items-start gap-2">
            <CheckCircle2
              size={16}
              className="text-primary shrink-0 mt-0.5"
            />
            <div className="text-xs text-muted-foreground space-y-1">
              <p className="font-medium text-foreground">What you get:</p>
              <ul className="list-disc list-inside space-y-0.5 ml-1">
                <li>Dedicated workspace for files and artifacts</li>
                <li>Multiple chat sessions within the project</li>
                <li>Task management and notebook for notes</li>
                <li>Source control integration with Git</li>
                <li>Migration tools from existing sessions</li>
              </ul>
            </div>
          </div>
        </div>

        {/* Action Buttons */}
        <div className="flex items-center justify-end gap-3 pt-4 border-t border-border">
          <Button
            type="button"
            variant="outline"
            onClick={handleCancel}
            disabled={isCreating}
          >
            Cancel
          </Button>
          <Button
            type="submit"
            disabled={
              isCreating ||
              !name.trim() ||
              (workspaceType === "external" && !workspacePath.trim())
            }
            className="gap-2"
          >
            {isCreating ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                Creating...
              </>
            ) : (
              <>
                <Folder size={16} />
                Create Project
              </>
            )}
          </Button>
        </div>
      </form>

      {/* Keyboard Shortcut Hint */}
      <div className="mt-4 text-center text-xs text-muted-foreground">
        <span className="font-mono bg-muted px-1.5 py-0.5 rounded border border-border">
          Ctrl
        </span>
        {" + "}
        <span className="font-mono bg-muted px-1.5 py-0.5 rounded border border-border">
          Enter
        </span>
        {" to create project"}
      </div>

      <ToastContainer />

      {/* Cross-platform folder picker for the External workspace path.
          Uses createPortal internally, so DOM position is irrelevant. */}
      <DirectoryPicker
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        initialPath={workspacePath || undefined}
        onSelect={(p) => setWorkspacePath(p)}
        title="Select Workspace Folder"
      />
    </div>
  );
}
