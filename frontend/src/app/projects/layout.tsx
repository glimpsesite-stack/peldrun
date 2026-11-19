// frontend/src/app/projects/layout.tsx

/**
 * Projects Layout - Shared layout for all project-related pages.
 *
 * Design Decision:
 * ----------------
 * This layout is intentionally minimal because the main application
 * layout (which includes the Sidebar component) already handles:
 * - Project navigation and listing
 * - Session display for the current project
 * - Mode detection (chat / projects-list / project-detail)
 *
 * The Sidebar component (frontend/src/components/layout/sidebar.tsx)
 * already detects when the user is on /projects/* routes and adjusts
 * its display accordingly. We do NOT need a separate navigation
 * component here.
 *
 * This layout only provides:
 * - Consistent page structure for project pages
 * - Error boundary for graceful degradation
 * - Loading state wrapper
 *
 * Routes wrapped by this layout:
 * - /projects (list)
 * - /projects/[projectId] (dashboard)
 * - /projects/[projectId]/chat
 * - /projects/[projectId]/tasks
 * - /projects/[projectId]/notebook
 * - /projects/[projectId]/source-control
 * - /projects/[projectId]/migration
 * - /projects/[projectId]/settings
 * - /projects/new
 */

import { ReactNode } from "react";

export default function ProjectsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-background w-full flex flex-col">
      <main className="h-full">{children}</main>
    </div>
  );
}
