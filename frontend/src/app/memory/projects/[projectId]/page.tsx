// src/app/memory/projects/[projectId]/page.tsx

/**
 * Project detail page (`/memory/projects/[projectId]`).
 *
 * Three sections:
 *
 *   1. Statistics -- `ProjectStatsPanel` inside a MemorySection.
 *   2. Bulk actions -- `BulkActionsBar` inside a MemorySection.
 *   3. Settings override -- `ProjectOverrideForm` (self-sectioned).
 *
 * Routes and path depth
 * ---------------------
 * This file is 4 levels deep from `src/`, so imports use `../../../../`.
 */

'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  ArrowLeft,
  BarChart3,
  FolderOpen,
  RefreshCw,
  Zap,
} from 'lucide-react';

import {
  useMemorySettings,
  useProjectStats,
} from '../../../../features/memory/api/queries';
import { ProjectStatsPanel } from '../../../../features/memory/components/ProjectStatsPanel';
import { ProjectOverrideForm } from '../../../../features/memory/components/ProjectOverrideForm';
import { BulkActionsBar } from '../../../../features/memory/components/BulkActionsBar';
import { EmptyState } from '../../../../features/memory/components/EmptyState';
import { MemorySection } from '../../../../features/memory/components/MemorySection';
import { MemoryBanner } from '../../../../features/memory/components/MemoryBanner';
import { cn } from '../../../../features/memory/lib/cn';

export default function ProjectDetailPage() {
  const { t } = useTranslation('memory');
  const params = useParams<{ projectId?: string | string[] }>();

  const projectId = Array.isArray(params?.projectId)
    ? params.projectId[0]
    : params?.projectId;

  const settingsQuery = useMemorySettings();
  const mode = settingsQuery.data?.mode;
  const isDisabled = mode === 'disabled';
  const canReadStats = mode === 'read_write' || mode === 'read_only';

  const statsQuery = useProjectStats(projectId);

  if (!projectId) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title={t('errors.invalid_project_id')}
        description={t('projects.empty.description')}
        size="spacious"
        action={
          <Link
            href="/memory/projects"
            className="inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
          >
            <ArrowLeft size={12} aria-hidden="true" />
            {t('nav.projects')}
          </Link>
        }
      />
    );
  }

  return (
    <div className="space-y-6">
      {/* ============ Header ============ */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Link
          href="/memory/projects"
          className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-[11px] font-medium text-muted-foreground hover:text-foreground hover:bg-muted/40 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
        >
          <ArrowLeft size={11} aria-hidden="true" />
          {t('nav.projects')}
        </Link>
        <Link
          href={`/memory/browse?scope=${encodeURIComponent(`project:${projectId}`)}`}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-card px-2.5 text-[11px] font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 transition-colors"
        >
          <FolderOpen size={11} aria-hidden="true" />
          {t('nav.browse')}
        </Link>
      </div>

      <div>
        <h2 className="text-lg font-semibold font-heading text-foreground font-mono">
          {t('projects.detail.title', { id: projectId })}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('projects.detail.stats')}
        </p>
      </div>

      {/* ============ Disabled banner ============ */}
      {isDisabled && (
        <MemoryBanner
          tone="warning"
          message={t('errors.mode_disabled')}
          action={
            <Link
              href="/memory/settings"
              className="underline"
            >
              {t('errors.withSettings')}
            </Link>
          }
        />
      )}

      {/* ============ Stats ============ */}
      <MemorySection
        icon={BarChart3}
        title={t('projects.detail.stats')}
        subtitle={t('projects.detail.typeDistribution')}
        accent="primary"
        delay={0}
      >
        {!canReadStats ? (
          <p className="py-4 text-center text-[11px] text-muted-foreground">
            {t('projects.detail.emptyStats')}
          </p>
        ) : statsQuery.isLoading ? (
          <div className="space-y-3" aria-busy="true">
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
              {[0, 1, 2, 3].map((i) => (
                <div
                  key={i}
                  className="h-16 animate-pulse rounded-sm bg-muted"
                />
              ))}
            </div>
            <div className="h-3 w-2/3 animate-pulse rounded bg-muted" />
          </div>
        ) : statsQuery.error ? (
          <MemoryBanner
            tone="error"
            message={statsQuery.error.message}
            action={
              <button
                type="button"
                onClick={() => statsQuery.refetch()}
                className="inline-flex items-center gap-1 rounded border border-current px-2 py-0.5 font-medium hover:opacity-80"
              >
                <RefreshCw size={10} aria-hidden="true" />
                {t('common.retry')}
              </button>
            }
          />
        ) : statsQuery.data ? (
          <ProjectStatsPanel stats={statsQuery.data.stats} />
        ) : null}
      </MemorySection>

      {/* ============ Bulk actions ============ */}
      <MemorySection
        icon={Zap}
        title={t('projects.bulk.archiveButton').split(' ').slice(0, 2).join(' ')}
        subtitle={`${statsQuery.data?.stats.total ?? 0} ${t('common.empty') === 'Empty' ? 'entries' : ''}`.trim()}
        accent="rose"
        delay={0.05}
      >
        <BulkActionsBar
          projectId={projectId}
          total={statsQuery.data?.stats.total ?? 0}
        />
      </MemorySection>

      {/* ============ Settings override ============ */}
      <ProjectOverrideForm projectId={projectId} />
    </div>
  );
}
