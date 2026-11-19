// frontend/src/app/projects/[projectId]/layout.tsx

/**
 * Project Detail Layout
 *
 * Shared layout for all pages within a specific project.
 * Wraps routes like:
 *   - /projects/[projectId] (dashboard)
 *   - /projects/[projectId]/chat
 *   - /projects/[projectId]/tasks
 *   ...
 *
 * Next.js 15 Compatibility:
 * `params` is now a Promise and must be awaited in Server Components.
 */

import { ReactNode } from "react";
import { ProjectLayoutClient } from "./project-layout-client";

interface ProjectLayoutProps {
  params: Promise<{
    projectId: string;
  }>;
  children: ReactNode;
}

export default async function ProjectLayout({ params, children }: ProjectLayoutProps) {
  // Unwrap the params Promise (Next.js 15 requirement)
  const { projectId } = await params;

  return (
    <ProjectLayoutClient projectId={projectId}>
      {children}
    </ProjectLayoutClient>
  );
}
