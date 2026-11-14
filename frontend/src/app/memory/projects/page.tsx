// src/app/memory/projects/page.tsx

'use client';

import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import { FolderOpen, RefreshCw } from 'lucide-react';

import { useProjectsList } from '../../../features/memory/api/queries';
import { ProjectCard } from '../../../features/memory/components/ProjectCard';
import { EmptyState } from '../../../features/memory/components/EmptyState';
import { MemoryBanner } from '../../../features/memory/components/MemoryBanner';

export default function ProjectsListPage() {
  const { t } = useTranslation('memory');
  const projectsQuery = useProjectsList();

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold font-heading text-foreground">
          {t('projects.title')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('projects.empty.description')}
        </p>
      </div>

      {projectsQuery.isLoading ? (
        <div
          className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3"
          aria-busy="true"
        >
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div
              key={i}
              className="h-32 animate-pulse rounded-lg border border-border bg-muted/40"
            />
          ))}
        </div>
      ) : projectsQuery.error ? (
        <MemoryBanner
          tone="error"
          message={projectsQuery.error.message}
          action={
            <button
              type="button"
              onClick={() => projectsQuery.refetch()}
              className="inline-flex items-center gap-1 rounded border border-current px-2 py-0.5 font-medium hover:opacity-80"
            >
              <RefreshCw size={10} aria-hidden="true" />
              {t('common.retry')}
            </button>
          }
        />
      ) : (projectsQuery.data?.projects ?? []).length === 0 ? (
        <EmptyState
          icon={FolderOpen}
          title={t('projects.empty.title')}
          description={t('projects.empty.description')}
          size="spacious"
          action={
            <Link
              href="/memory/browse"
              className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              {t('nav.browse')}
            </Link>
          }
        />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {projectsQuery.data!.projects.map((project) => (
            <Link
              key={project.project_id}
              href={`/memory/projects/${encodeURIComponent(project.project_id)}`}
              className="rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background"
            >
              <ProjectCard project={project} />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
