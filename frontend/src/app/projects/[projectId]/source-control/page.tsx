// frontend/src/app/projects/[projectId]/source-control/page.tsx

/**
 * Project Source Control Page
 *
 * Provides a project-level view of Git repositories, branches,
 * and commit history, decoupled from a specific chat session.
 *
 * Design Decision:
 * Reuses the existing SourceControlPanel component to avoid
 * duplicating Git logic, but scopes it to the project level.
 */

"use client";

import React from "react";
import { useParams } from "next/navigation";
import { ProjectSourceControlPanel } from "@/features/source-control/components/project-source-control-panel";

export default function ProjectSourceControlPage() {
  const params = useParams();
  const projectId = params.projectId as string;

  return (
    <div className="flex flex-col h-full bg-background">
      {/* Header specific to Project Source Control */}
      <div className="h-14 border-b border-border bg-card px-6 flex items-center justify-between shrink-0">
        <div>
          <h1 className="text-lg font-semibold">Project Source Control</h1>
          <p className="text-xs text-muted-foreground">
            Manage repositories, branches, and history for this project.
          </p>
        </div>
      </div>

      {/*
        Reuse the existing ProjectSourceControlPanel.
        Note: You may need to slightly adapt ProjectSourceControlPanel props
        to accept `projectId` instead of (or alongside) `chatId`
        to fetch project-level workspace data.
      */}
      <div className="flex-1 min-h-0 overflow-hidden">
        <ProjectSourceControlPanel
          projectId={projectId}
          defaultMode="browse"
        />
      </div>
    </div>
  );
}
