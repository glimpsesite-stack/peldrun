// frontend/src/app/projects/[projectId]/project-layout-client.tsx
"use client";

import React, { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { motion, AnimatePresence } from "motion/react";
import {
  ChevronRight,
  Loader2,
  AlertCircle,
  ArrowLeft,
} from "lucide-react";
import { useProjectStore } from "@/stores/project-store";
import { Project } from "@/lib/chatsApi";

interface ProjectLayoutClientProps {
  projectId: string;
  children: React.ReactNode;
}

const PAGE_LABELS: Record<string, string> = {
  chat: "Conversations",
  tasks: "Tasks",
  notebook: "Notebook",
  "source-control": "Source Control",
  migration: "Migration",
  settings: "Settings",
};

const EASE_OUT: [number, number, number, number] = [0.16, 1, 0.3, 1];

export function ProjectLayoutClient({
  projectId,
  children,
}: ProjectLayoutClientProps) {
  const pathname = usePathname();
  const router = useRouter();
  const { getProject, upsertProject, loadProjects } = useProjectStore();

  const [project, setProject] = useState<Project | null>(null);
  const [isProjectLoading, setIsProjectLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) return;

    let cancelled = false;

    // One full resolution sweep: store → loadProjects → direct fetch.
    // Returns the resolved Project, or null when all three fail.
    const attemptResolve = async (): Promise<Project | null> => {
      // ── Path 1: store fast path (persist-hydrated on F5) ──
      let currentProject: Project | null = getProject(projectId) || null;
      if (currentProject) return currentProject;

      // ── Path 2: force-refresh via loadProjects ──
      try {
        await loadProjects(true);
        currentProject = getProject(projectId) || null;
        if (currentProject) return currentProject;
      } catch (err) {
        console.warn(
          "[ProjectLayout] loadProjects failed:",
          err
        );
      }

      // ── Path 3: direct fetch fallback ──
      //
      // CORRECT URL: /api/chats/projects/{id} — chats.router is
      // mounted at /api/chats, not /api. The old candidates
      // (/api/projects/{id} and /projects/{id}) returned 405 and
      // HTML respectively, so this fallback used to silently fail
      // on F5 whenever Paths 1 and 2 missed.
      const candidateUrls = [
        `/api/chats/projects/${projectId}`,
      ];
      for (const url of candidateUrls) {
        try {
          const res = await fetch(url, {
            credentials: "include",
            headers: { Accept: "application/json" },
          });
          if (!res.ok) continue;
          const data = await res.json();
          const raw = data?.project ?? data;
          if (raw && (raw.id || raw.project_id)) {
            const normalized = {
              ...raw,
              id: raw.id || raw.project_id,
            } as Project;
            try {
              upsertProject(normalized);
            } catch {
              /* non-fatal — store already has data */
            }
            return normalized;
          }
        } catch {
          /* try next candidate URL */
        }
      }

      return null;
    };

    const loadData = async () => {
      let currentProject = await attemptResolve();

      // ── Retry once after a short delay ──
      //
      // On F5 the persist middleware and the initial effects can
      // race: the store may still be empty when the first sweep
      // runs, and loadProjects may fail transiently if the backend
      // is not fully warm. A single 400ms retry handles both cases
      // without introducing a visible flash of "Loading…" for the
      // common (success) case.
      if (!currentProject && !cancelled) {
        await new Promise((resolve) => setTimeout(resolve, 400));
        if (cancelled) return;
        currentProject = await attemptResolve();
      }

      if (cancelled) return;

      if (!currentProject) {
        console.warn(
          "[ProjectLayout] Project could not be resolved after retry:",
          projectId
        );
      }
      setLoadError(
        currentProject
          ? null
          : "Unable to load project. Please try again."
      );
      setProject(currentProject);
      setIsProjectLoading(false);
    };

    loadData();

    return () => {
      cancelled = true;
    };
  }, [projectId, getProject, upsertProject, loadProjects]);

  const breadcrumbs = React.useMemo(() => {
    const items: Array<{ label: string; href: string | null }> = [
      { label: "Projects", href: "/projects" },
    ];
    if (project) {
      items.push({ label: project.name, href: `/projects/${projectId}` });
    }
    const rest = pathname.split("/").filter(Boolean).slice(2);
    if (rest.length > 0) {
      const currentPage = rest[0];
      const label = PAGE_LABELS[currentPage] || currentPage;
      items.push({ label, href: null });
    }
    return items;
  }, [pathname, project, projectId]);

  /* ── Loading ── */
  if (isProjectLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="flex flex-col items-center gap-3"
        >
          <div className="relative">
            <motion.div
              className="absolute inset-0 rounded-full bg-accent/25 blur-xl"
              animate={{ scale: [1, 1.5, 1], opacity: [0.4, 0.7, 0.4] }}
              transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
            />
            <Loader2 size={28} className="animate-spin text-accent relative" />
          </div>
          <p className="text-sm text-muted-foreground">Loading project…</p>
        </motion.div>
      </div>
    );
  }

  /* ── Load error (distinct from Not Found) ── */
  //
  // Shown when all three load paths failed — the project may exist,
  // but we could not reach the backend to verify. This prevents the
  // misleading "Project Not Found" message during transient outages.
  if (!project && loadError) {
    return (
      <motion.div
        className="container mx-auto px-4 py-16 max-w-2xl"
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: EASE_OUT }}
      >
        <div className="bg-card border border-border rounded-xl p-8 text-center shadow-sm">
          <motion.div
            initial={{ scale: 0.85 }}
            animate={{ scale: 1 }}
            transition={{ type: "spring", stiffness: 220, damping: 18 }}
            className="inline-flex p-3 bg-amber-500/10 rounded-full mb-4"
          >
            <AlertCircle size={28} className="text-amber-500" />
          </motion.div>
          <h2 className="text-xl font-semibold mb-2">Failed to load project</h2>
          <p className="text-muted-foreground mb-6">{loadError}</p>
          <div className="flex items-center justify-center gap-3 flex-wrap">
            <button
              onClick={() => window.location.reload()}
              className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90 transition-colors"
            >
              <Loader2 size={16} />
              Retry
            </button>
            <button
              onClick={() => router.push("/projects")}
              className="inline-flex items-center gap-2 px-4 py-2 border border-border rounded-md hover:bg-muted transition-colors"
            >
              <ArrowLeft size={16} />
              Back to Projects
            </button>
          </div>
        </div>
      </motion.div>
    );
  }

  /* ── Not found ── */
  if (!project) {
    return (
      <motion.div
        className="container mx-auto px-4 py-16 max-w-2xl"
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: EASE_OUT }}
      >
        <div className="bg-card border border-border rounded-xl p-8 text-center shadow-sm">
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
            The project you're looking for doesn't exist or has been deleted.
          </p>
          <button
            onClick={() => router.push("/projects")}
            className="inline-flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90 transition-colors"
          >
            <ArrowLeft size={16} />
            Back to Projects
          </button>
        </div>
      </motion.div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      {/* Breadcrumbs */}
      <motion.div
        className="border-b border-border bg-card px-6 py-3"
        initial={{ opacity: 0, y: -4 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, ease: EASE_OUT }}
      >
        <nav className="flex items-center gap-1.5 text-sm">
          {breadcrumbs.map((item, index) => (
            <React.Fragment key={index}>
              {index > 0 && (
                <ChevronRight
                  size={13}
                  className="text-muted-foreground/60 shrink-0"
                />
              )}
              <motion.span
                initial={{ opacity: 0, x: -4 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{
                  delay: index * 0.05,
                  duration: 0.3,
                  ease: EASE_OUT,
                }}
              >
                {item.href ? (
                  <Link
                    href={item.href}
                    className="text-muted-foreground hover:text-foreground transition-colors"
                  >
                    {item.label}
                  </Link>
                ) : (
                  <span className="text-foreground font-medium">
                    {item.label}
                  </span>
                )}
              </motion.span>
            </React.Fragment>
          ))}
        </nav>
      </motion.div>

      {/* Page transition */}
      <AnimatePresence mode="wait">
        <motion.div
          // Stable key: `/chat/new` and `/chat/[chatId]` share one key,
          // so the post-send URL rewrite does NOT remount ChatContainer
          // (which would kill the live SSE stream). Other routes keep
          // their unique pathname key.
          key={
            pathname.startsWith(`/projects/${projectId}/chat/`) ||
            pathname === `/projects/${projectId}/chat`
              ? `chat-session-${projectId}`
              : pathname
          }
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -6 }}
          transition={{ duration: 0.22, ease: "easeOut" }}
          className="h-[calc(100vh-57px)]"
        >
          {children}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}
