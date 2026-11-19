// frontend/src/app/projects/[projectId]/settings/page.tsx

"use client";

/**
 * Project Settings Page — Motion-Enhanced
 *
 * Provides:
 * - Basic info editing (name, description)
 * - Workspace configuration (path, type)
 * - Danger zone (delete project)
 *
 * Layout notes:
 * - The parent ProjectLayoutClient constrains children to
 *   `h-[calc(100vh-57px)]` — this page provides its own
 *   scroll container so long content remains reachable.
 * - Height is set via inline `style` (not h-full) to avoid
 *   Tailwind calc parsing issues and guarantee a definite
 *   height for `overflow-y-auto` to work.
 * - Scroll container is full width; the inner container
 *   (max-w-4xl) centers content within it.
 */

import React, { useState, useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import { motion, AnimatePresence } from "motion/react";
import {
  Settings,
  Save,
  Trash2,
  Calendar,
  Clock,
  AlertTriangle,
  Loader2,
  ArrowLeft,
  ExternalLink,
  HardDrive,
  Info,
  Hash,
  AlertCircle,
  Sparkles,
  FolderOpen,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { useProjectStore } from "@/stores/project-store";
import { useConfirmModal } from "@/components/ui/ConfirmModal";
import { showToast, ToastContainer } from "@/components/ui/ToastNotification";
import { DirectoryPicker } from "@/components/ui/directory-picker";
import { Project } from "@/lib/chatsApi";
import { WorkspaceType } from "@/features/workspace/types";

/* ───────── Animation primitives ───────── */

const EASE_OUT: [number, number, number, number] = [0.16, 1, 0.3, 1];

const fadeUp = {
  hidden: { opacity: 0, y: 14 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.45, ease: EASE_OUT },
  },
};

const stagger = {
  hidden: {},
  visible: { transition: { staggerChildren: 0.07, delayChildren: 0.05 } },
};

/* ───────── Section wrapper ───────── */

function Section({
  children,
  className = "",
  tone = "default",
}: {
  children: React.ReactNode;
  className?: string;
  tone?: "default" | "danger";
}) {
  const base =
    "bg-card border border-border rounded-2xl p-6 shadow-sm relative overflow-hidden";
  const dangerStyles =
    tone === "danger"
      ? "bg-destructive/[0.03] border-destructive/30"
      : "";
  return (
    <motion.section
      variants={fadeUp}
      className={`${base} ${dangerStyles} ${className}`}
    >
      {children}
    </motion.section>
  );
}

/* ───────── Page ───────── */

export default function ProjectSettingsPage() {
  const params = useParams();
  const router = useRouter();
  const { confirm, ConfirmDialog } = useConfirmModal();
  const projectId = params.projectId as string;

  const { getProject, upsertProject, removeProject, loadProjects } =
    useProjectStore();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [workspacePath, setWorkspacePath] = useState("");
  const [workspaceType, setWorkspaceType] =
    useState<WorkspaceType>("internal");

  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [project, setProject] = useState<Project | null>(null);
  const [hasChanges, setHasChanges] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);

  useEffect(() => {
    const loadProjectData = async () => {
      if (!projectId) return;
      let currentProject = getProject(projectId);
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

  useEffect(() => {
    if (!project) return;
    const changed =
      name !== project.name ||
      description !== (project.description || "") ||
      workspacePath !== (project.workspace_path || "") ||
      workspaceType !== (project.workspace_type || "internal");
    setHasChanges(changed);
  }, [name, description, workspacePath, workspaceType, project]);

  /* ── Save ── */
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
      showToast.success(
        "Settings Saved",
        "Project settings have been updated successfully."
      );
    } catch (err: any) {
      console.error("[ProjectSettings] Save failed:", err);
      showToast.error(
        "Save Failed",
        err?.message || "Could not save project settings."
      );
    } finally {
      setIsSaving(false);
    }
  };

  /* ── Delete ── */
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
      const res = await fetch(`/api/chats/projects/${projectId}`, {
        method: "DELETE",
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      removeProject(projectId);
      showToast.success(
        "Project Deleted",
        `"${project.name}" has been permanently deleted.`
      );
      router.push("/projects");
    } catch (err: any) {
      console.error("[ProjectSettings] Delete failed:", err);
      showToast.error(
        "Delete Failed",
        err?.message || "Could not delete project."
      );
    } finally {
      setIsDeleting(false);
    }
  };

  const formatDate = (dateString?: string) => {
    if (!dateString) return "Never";
    return new Date(dateString).toLocaleDateString("en-US", {
      year: "numeric",
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  /* ── Loading ── */
  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="flex flex-col items-center gap-4"
        >
          <div className="relative">
            <motion.div
              className="absolute inset-0 rounded-full bg-accent/25 blur-xl"
              animate={{ scale: [1, 1.5, 1], opacity: [0.4, 0.7, 0.4] }}
              transition={{
                duration: 2,
                repeat: Infinity,
                ease: "easeInOut",
              }}
            />
            <Loader2
              size={28}
              className="animate-spin text-accent relative"
            />
          </div>
          <p className="text-sm text-muted-foreground">Loading settings…</p>
        </motion.div>
      </div>
    );
  }

  /* ── Not found ── */
  if (!project) {
    return (
      <div className="h-full overflow-y-auto">
        <motion.div
          className="container mx-auto px-4 py-16 max-w-2xl"
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, ease: EASE_OUT }}
        >
          <div className="bg-card border border-border rounded-2xl p-8 text-center shadow-sm">
            <motion.div
              initial={{ scale: 0.85 }}
              animate={{ scale: 1 }}
              transition={{ type: "spring", stiffness: 220, damping: 18 }}
              className="inline-flex p-3 bg-destructive/10 rounded-full mb-4"
            >
              <AlertCircle size={28} className="text-destructive" />
            </motion.div>
            <h2 className="text-xl font-semibold mb-2">Project Not Found</h2>
            <p className="text-muted-foreground mb-6">
              The project you're looking for doesn't exist or has been
              deleted.
            </p>
            <Button onClick={() => router.push("/projects")}>
              <ArrowLeft size={16} className="mr-2" />
              Back to Projects
            </Button>
          </div>
        </motion.div>
      </div>
    );
  }

  /* ── Info grid data ── */
  const infoCards = [
    {
      icon: Hash,
      label: "Project ID",
      value: (
        <span className="font-mono text-xs break-all">{project.id}</span>
      ),
      color: "text-slate-500 bg-slate-500/10",
    },
    {
      icon: Calendar,
      label: "Created",
      value: (
        <span className="text-sm">{formatDate(project.created_at)}</span>
      ),
      color: "text-violet-500 bg-violet-500/10",
    },
    {
      icon: Clock,
      label: "Last Updated",
      value: (
        <span className="text-sm">{formatDate(project.updated_at)}</span>
      ),
      color: "text-blue-500 bg-blue-500/10",
    },
    {
      icon: HardDrive,
      label: "Workspace Type",
      value: (
        <span className="text-sm capitalize">
          {project.workspace_type || "internal"}
        </span>
      ),
      color: "text-emerald-500 bg-emerald-500/10",
    },
  ];

  return (
    /*
     * Scroll container — explicit height ensures overflow-y-auto
     * has a definite boundary to scroll within.
     * Inline style avoids Tailwind arbitrary-value calc quirks.
     */
    <div
      className="w-full overflow-y-auto"
      style={{ height: "calc(100vh - 57px)" }}
    >
      <div className="container mx-auto px-4 py-8 max-w-4xl">
        <motion.div
          variants={stagger}
          initial="hidden"
          animate="visible"
          className="space-y-6"
        >
          {/* ── Header ── */}
          <motion.div variants={fadeUp} className="mb-2">
            <div className="flex items-start justify-between gap-4 flex-wrap">
              <div className="flex items-start gap-3">
                <motion.div
                  whileHover={{ scale: 1.05, rotate: 4 }}
                  transition={{
                    type: "spring",
                    stiffness: 320,
                    damping: 22,
                  }}
                  className="p-2.5 bg-accent/10 rounded-xl shrink-0 mt-0.5"
                >
                  <Settings size={22} className="text-accent" />
                </motion.div>
                <div>
                  <h1 className="text-2xl font-bold tracking-tight">
                    Project Settings
                  </h1>
                  <p className="text-sm text-muted-foreground mt-0.5">
                    Configure your project's basic information and
                    workspace.
                  </p>
                </div>
              </div>

              {/* Unsaved changes pill */}
              <AnimatePresence mode="wait">
                {hasChanges && (
                  <motion.div
                    key="unsaved"
                    initial={{ opacity: 0, y: -6, scale: 0.96 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, y: -6, scale: 0.96 }}
                    transition={{
                      type: "spring",
                      stiffness: 380,
                      damping: 26,
                    }}
                    className="flex items-center gap-2"
                  >
                    <motion.div
                      className="flex items-center gap-1.5 px-2.5 py-1 bg-amber-500/10 border border-amber-500/30 rounded-full text-xs text-amber-600 dark:text-amber-400"
                      animate={{ opacity: [0.85, 1, 0.85] }}
                      transition={{ duration: 2.5, repeat: Infinity }}
                    >
                      <span className="w-1.5 h-1.5 rounded-full bg-amber-500" />
                      Unsaved changes
                    </motion.div>
                    <Button
                      onClick={handleSave}
                      disabled={isSaving}
                      className="gap-2"
                    >
                      {isSaving ? (
                        <Loader2 size={15} className="animate-spin" />
                      ) : (
                        <Save size={15} />
                      )}
                      Save
                    </Button>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            {/* Back button */}
            <div className="mt-4">
              <Button
                variant="ghost"
                size="sm"
                onClick={() => router.push(`/projects/${projectId}`)}
                className="gap-1.5 -ml-2 text-muted-foreground hover:text-foreground"
              >
                <ArrowLeft size={15} />
                Back to dashboard
              </Button>
            </div>
          </motion.div>

          {/* ── Project Info (read-only) ── */}
          <Section>
            <div className="flex items-center gap-2 mb-5">
              <Info size={16} className="text-accent" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                Project Information
              </h2>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {infoCards.map((card, i) => {
                const Icon = card.icon;
                return (
                  <motion.div
                    key={card.label}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{
                      delay: 0.1 + i * 0.05,
                      duration: 0.35,
                      ease: EASE_OUT,
                    }}
                    whileHover={{
                      y: -2,
                      transition: { duration: 0.15, ease: "easeOut" },
                    }}
                    className="bg-muted/30 border border-border rounded-xl p-3.5 hover:border-accent/30 transition-colors"
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <div className={`p-1.5 rounded-md ${card.color}`}>
                        <Icon size={12} />
                      </div>
                      <span className="text-[11px] uppercase tracking-wider font-medium text-muted-foreground">
                        {card.label}
                      </span>
                    </div>
                    <div className="text-foreground">{card.value}</div>
                  </motion.div>
                );
              })}
            </div>
          </Section>

          {/* ── Basic Information ── */}
          <Section>
            <div className="flex items-center gap-2 mb-5">
              <Sparkles size={16} className="text-accent" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                Basic Information
              </h2>
            </div>

            <div className="space-y-5">
              {/* Name */}
              <div>
                <label className="block text-sm font-medium mb-1.5">
                  Project Name <span className="text-destructive">*</span>
                </label>
                <input
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="w-full px-3.5 py-2.5 bg-background border border-border rounded-lg text-sm
                             focus:outline-none focus:ring-2 focus:ring-accent/40 focus:border-accent
                             transition-shadow placeholder:text-muted-foreground/60"
                  placeholder="My Awesome Project"
                  required
                />
                <p className="text-xs text-muted-foreground mt-1.5">
                  A descriptive name for your project. This will be
                  displayed in the sidebar.
                </p>
              </div>

              {/* Description */}
              <div>
                <label className="block text-sm font-medium mb-1.5">
                  Description
                </label>
                <textarea
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  className="w-full px-3.5 py-2.5 bg-background border border-border rounded-lg text-sm
                             focus:outline-none focus:ring-2 focus:ring-accent/40 focus:border-accent
                             transition-shadow resize-none placeholder:text-muted-foreground/60"
                  placeholder="A brief description of what this project is about…"
                  rows={3}
                />
                <p className="text-xs text-muted-foreground mt-1.5">
                  Optional description to help you remember the purpose of
                  this project.
                </p>
              </div>
            </div>
          </Section>

          {/* ── Workspace Configuration ── */}
          <Section>
            <div className="flex items-center gap-2 mb-5">
              <HardDrive size={16} className="text-br" />
              <h2 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground">
                Workspace Configuration
              </h2>
            </div>

            <div className="space-y-5">
              {/* Path */}
              <div>
                <label className="block text-sm font-medium mb-1.5">
                  Workspace Path
                </label>
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={workspacePath}
                    onChange={(e) => setWorkspacePath(e.target.value)}
                    className="min-w-0 flex-1 px-3.5 py-2.5 bg-background border border-border rounded-lg font-mono text-sm
                               focus:outline-none focus:ring-2 focus:ring-accent/40 focus:border-accent
                               transition-shadow placeholder:text-muted-foreground/60"
                    placeholder="/path/to/project/workspace"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setPickerOpen(true)}
                    className="gap-1.5 shrink-0 h-[46px] px-3"
                  >
                    <FolderOpen size={14} />
                    <span className="hidden sm:inline">Browse…</span>
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground mt-1.5">
                  The absolute path to the project's workspace directory on
                  disk. Leave empty to use the default internal workspace.
                </p>
                <AnimatePresence>
                  {workspacePath && (
                    <motion.div
                      initial={{ opacity: 0, height: 0, marginTop: 0 }}
                      animate={{
                        opacity: 1,
                        height: "auto",
                        marginTop: 8,
                      }}
                      exit={{ opacity: 0, height: 0, marginTop: 0 }}
                      transition={{ duration: 0.25, ease: EASE_OUT }}
                      className="overflow-hidden"
                    >
                      <div className="p-2.5 bg-muted/40 border border-border rounded-lg flex items-center gap-2">
                        <ExternalLink
                          size={12}
                          className="text-muted-foreground shrink-0"
                        />
                        <span className="text-xs font-mono text-muted-foreground truncate">
                          {workspacePath}
                        </span>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>

              {/* Type selector */}
              <div>
                <label className="block text-sm font-medium mb-2">
                  Workspace Type
                </label>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {(
                    [
                      {
                        value: "internal" as const,
                        label: "Internal",
                        desc: "Workspace is managed within the PELDRUN storage directory.",
                        Icon: HardDrive,
                      },
                      {
                        value: "external" as const,
                        label: "External",
                        desc: "Workspace points to an external directory on your filesystem.",
                        Icon: ExternalLink,
                      },
                    ]
                  ).map(({ value, label, desc, Icon }) => {
                    const isActive = workspaceType === value;
                    return (
                      <motion.label
                        key={value}
                        whileHover={{
                          y: -2,
                          transition: { duration: 0.15, ease: "easeOut" },
                        }}
                        whileTap={{ scale: 0.99 }}
                        className={`relative flex flex-col p-4 border rounded-xl cursor-pointer transition-colors ${
                          isActive
                            ? "border-accent bg-accent/5"
                            : "border-border hover:border-accent/40"
                        }`}
                      >
                        <input
                          type="radio"
                          name="workspaceType"
                          value={value}
                          checked={isActive}
                          onChange={() => setWorkspaceType(value)}
                          className="sr-only"
                        />
                        <AnimatePresence>
                          {isActive && (
                            <motion.div
                              layoutId="workspace-selected-pill"
                              className="absolute inset-0 rounded-xl ring-2 ring-accent pointer-events-none"
                              transition={{
                                type: "spring",
                                stiffness: 380,
                                damping: 28,
                              }}
                            />
                          )}
                        </AnimatePresence>
                        <div className="relative flex items-center gap-2 mb-1.5">
                          <Icon
                            size={15}
                            className={
                              isActive
                                ? "text-accent"
                                : "text-muted-foreground"
                            }
                          />
                          <span className="font-medium text-sm">{label}</span>
                          <AnimatePresence>
                            {isActive && (
                              <motion.span
                                initial={{ scale: 0, opacity: 0 }}
                                animate={{ scale: 1, opacity: 1 }}
                                exit={{ scale: 0, opacity: 0 }}
                                transition={{
                                  type: "spring",
                                  stiffness: 400,
                                  damping: 24,
                                }}
                                className="ml-auto w-4 h-4 rounded-full bg-accent flex items-center justify-center"
                              >
                                <div className="w-1.5 h-1.5 rounded-full bg-white" />
                              </motion.span>
                            )}
                          </AnimatePresence>
                        </div>
                        <p className="relative text-xs text-muted-foreground leading-relaxed">
                          {desc}
                        </p>
                      </motion.label>
                    );
                  })}
                </div>
              </div>
            </div>
          </Section>

          {/* ── Bottom Save bar ── */}
          <AnimatePresence>
            {hasChanges && (
              <motion.div
                initial={{ opacity: 0, y: 8, height: 0 }}
                animate={{ opacity: 1, y: 0, height: "auto" }}
                exit={{ opacity: 0, y: 8, height: 0 }}
                transition={{ duration: 0.25, ease: EASE_OUT }}
                className="flex justify-end overflow-hidden"
              >
                <Button
                  onClick={handleSave}
                  disabled={isSaving}
                  className="gap-2"
                >
                  {isSaving ? (
                    <Loader2 size={15} className="animate-spin" />
                  ) : (
                    <Save size={15} />
                  )}
                  Save Changes
                </Button>
              </motion.div>
            )}
          </AnimatePresence>

          {/* ── Danger Zone ── */}
          <Section tone="danger">
            <motion.div
              className="absolute top-0 left-0 right-0 h-0.5 bg-gradient-to-r from-transparent via-destructive/60 to-transparent"
              animate={{ opacity: [0.4, 1, 0.4] }}
              transition={{ duration: 3, repeat: Infinity, ease: "easeInOut" }}
            />

            <div className="flex items-center gap-2 mb-2">
              <motion.div
                animate={{ rotate: [0, -5, 5, -3, 0] }}
                transition={{
                  duration: 0.6,
                  repeat: Infinity,
                  repeatDelay: 4,
                  ease: "easeOut",
                }}
              >
                <AlertTriangle size={16} className="text-destructive" />
              </motion.div>
              <h2 className="text-sm font-semibold uppercase tracking-wider text-destructive">
                Danger Zone
              </h2>
            </div>
            <p className="text-sm text-muted-foreground mb-5">
              Destructive actions cannot be undone. Please be certain.
            </p>

            <div className="flex flex-col md:flex-row md:items-start gap-4 p-4 bg-background border border-destructive/25 rounded-xl">
              <div className="flex-1">
                <h3 className="font-semibold text-sm mb-1.5">
                  Delete this project
                </h3>
                <p className="text-xs text-muted-foreground mb-2">
                  Once you delete a project, there is no going back. This
                  will permanently delete:
                </p>
                <ul className="text-xs text-muted-foreground space-y-1">
                  {[
                    "All chat sessions within the project",
                    "All workspace files and artifacts",
                    "All tasks and notebook entries",
                    "Project settings and metadata",
                  ].map((item, i) => (
                    <motion.li
                      key={item}
                      initial={{ opacity: 0, x: -6 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{
                        delay: 0.15 + i * 0.04,
                        duration: 0.3,
                        ease: EASE_OUT,
                      }}
                      className="flex items-center gap-2"
                    >
                      <span className="w-1 h-1 rounded-full bg-destructive/60 shrink-0" />
                      {item}
                    </motion.li>
                  ))}
                </ul>
              </div>
              <motion.div
                whileHover={{ y: -1 }}
                whileTap={{ scale: 0.97 }}
                className="md:ml-4 shrink-0"
              >
                <Button
                  variant="destructive"
                  onClick={handleDelete}
                  disabled={isDeleting}
                  className="gap-2 w-full md:w-auto"
                >
                  {isDeleting ? (
                    <Loader2 size={15} className="animate-spin" />
                  ) : (
                    <Trash2 size={15} />
                  )}
                  Delete Project
                </Button>
              </motion.div>
            </div>
          </Section>
        </motion.div>

        {/* Dialogs */}
        <ConfirmDialog />
        <ToastContainer />

        {/* Cross-platform folder picker for the External workspace path.
            Uses createPortal internally, so DOM position is irrelevant. */}
        <DirectoryPicker
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          initialPath={workspacePath || undefined}
          onSelect={(p) => {
            setWorkspacePath(p);
            // A picked folder almost always means the user wants an
            // external workspace, so flip the radio automatically.
            if (workspaceType !== "external") {
              setWorkspaceType("external");
            }
          }}
          title="Select Workspace Folder"
        />
      </div>
    </div>
  );
}
