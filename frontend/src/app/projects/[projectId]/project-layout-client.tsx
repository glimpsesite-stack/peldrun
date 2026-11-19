// frontend/src/app/projects/[projectId]/project-layout-client.tsx

/**
 * Project Layout Client Component
 *
 * Client-side wrapper for the project detail layout.
 * Provides:
 * - Project metadata loading and validation
 * - Breadcrumb navigation (Projects > Project Name > Current Page)
 * - Project context for child components
 * - Loading and error states
 *
 * Design Decision:
 * ----------------
 * The main application sidebar (frontend/src/components/layout/sidebar.tsx)
 * already handles project navigation and session listing. This component
 * focuses ONLY on:
 * - Top-level breadcrumb navigation
 * - Project context validation
 * - Page structure consistency
 *
 * It does NOT duplicate sidebar functionality.
 */

"use client";

import React, { useEffect, useState } from "react";
import { useParams, usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import {
  ChevronRight,
  Folder,
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

/**
 * Maps pathname segments to human-readable labels for breadcrumbs.
 */
const PAGE_LABELS: Record<string, string> = {
  chat: "Conversations",
  tasks: "Tasks",
  notebook: "Notebook",
  "source-control": "Source Control",
  migration: "Migration",
  settings: "Settings",
};

export function ProjectLayoutClient({
  projectId,
  children,
}: ProjectLayoutClientProps) {
  const params = useParams();
  const pathname = usePathname();
  const router = useRouter();
  const { getProject, isLoading, loadProjects } = useProjectStore();

  const [project, setProject] = useState<Project | null>(null);
  const [isProjectLoading, setIsProjectLoading] = useState(true);

  // Load project data
  useEffect(() => {
    if (!projectId) return;

    const loadData = async () => {
      // 1. Try to get from store first
      let currentProject = getProject(projectId);

      // 2. If not in store, trigger a fetch (force=true to ensure network call)
      if (!currentProject) {
        try {
          console.log(
            "[DEBUG] ProjectLayout: fetching projects for",
            projectId
          );
          await loadProjects(true);
          currentProject = getProject(projectId);
          console.log(
            "[DEBUG] ProjectLayout: after fetch, getProject =",
            currentProject,
            "| store keys:",
            Object.keys(
              useProjectStore.getState().projectsById || {}
            )
          );
        } catch (error) {
          console.error(
            "[DEBUG] ProjectLayout: fetch FAILED",
            error
          );
        }
      }

      // 3. Update state explicitly, even if it's null (to show "Not Found" UI)
      setProject(currentProject || null);
      setIsProjectLoading(false);
    };

    loadData();
  }, [projectId, getProject, loadProjects]);

  // Build breadcrumb items
  const breadcrumbs = React.useMemo(() => {
    const items: Array<{ label: string; href: string | null }> = [
      { label: "Projects", href: "/projects" },
    ];

    if (project) {
      items.push({
        label: project.name,
        href: `/projects/${projectId}`,
      });
    }

    // Parse current page from pathname
    const pathSegments = pathname.split("/").filter(Boolean);
    // pathSegments: ["projects", projectId, ...rest]
    const rest = pathSegments.slice(2);

    if (rest.length > 0) {
      const currentPage = rest[0];
      const label = PAGE_LABELS[currentPage] || currentPage;
      items.push({ label, href: null }); // Current page is not a link
    }

    return items;
  }, [pathname, project, projectId]);

  // Loading state
  if (isProjectLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <Loader2 size={32} className="animate-spin text-primary" />
          <p className="text-sm text-muted-foreground">Loading project...</p>
        </div>
      </div>
    );
  }

  // Project not found
  if (!project) {
    return (
      <div className="container mx-auto px-4 py-16 max-w-2xl">
        <div className="bg-card border border-border rounded-lg p-8 text-center">
          <AlertCircle
            size={48}
            className="mx-auto text-destructive mb-4"
          />
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
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      {/* Breadcrumb Navigation */}
      <div className="border-b border-border bg-card px-6 py-3">
        <nav className="flex items-center gap-2 text-sm">
          {breadcrumbs.map((item, index) => (
            <React.Fragment key={index}>
              {index > 0 && (
                <ChevronRight
                  size={14}
                  className="text-muted-foreground shrink-0"
                />
              )}
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
            </React.Fragment>
          ))}
        </nav>
      </div>

      {/* Main Content */}
      <div className="h-[calc(100vh-57px)]">{children}</div>
    </div>
  );
}
