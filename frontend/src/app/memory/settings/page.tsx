// src/app/memory/settings/page.tsx

/**
 * The memory settings page (`/memory/settings`).
 *
 * Reachable even when `mode === 'disabled'` -- this page is the
 * only path back to a working memory subsystem (spec §1.3 / §6.1).
 *
 * Layout:
 *
 *   * Page title (matches the dashboard's heading style).
 *   * `<SettingsForm>` -- renders the five settings sections.
 *   * `Projects with overrides` section -- the projects that
 *     have customized configuration, each row linking to the
 *     project detail page where the override is edited.
 */

'use client';

import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import { ArrowRight, FolderCog, ListChecks } from 'lucide-react';

import { useMemorySettings } from '../../../features/memory/api/queries';
import { SettingsForm } from '../../../features/memory/components/SettingsForm';
import { MemorySection } from '../../../features/memory/components/MemorySection';
import { cn } from '../../../features/memory/lib/cn';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MemorySettingsPage() {
  const { t } = useTranslation('memory');
  const settingsQuery = useMemorySettings();

  const overrides = settingsQuery.data?.project_overrides ?? {};
  const overrideEntries = Object.entries(overrides);

  return (
    <div className="space-y-6">
      {/* ============ Title ============ */}
      <div>
        <h2 className="text-lg font-semibold font-heading text-foreground">
          {t('settings.title')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('settings.description')}
        </p>
      </div>

      {/* ============ Settings form ============ */}
      <SettingsForm />

      {/* ============ Automation shortcuts ============ */}
      <MemorySection
        icon={ListChecks}
        title={t('settings.sections.automation', 'Automation')}
        subtitle={t(
          'settings.automationSubtitle',
          'Manage extraction jobs and batches',
        )}
        delay={0.15}
      >
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <Link
            href="/memory/jobs"
            className={cn(
              'group flex items-center justify-between gap-2 rounded-sm border border-border/60 bg-card px-3 py-2 text-[11px]',
              'hover:border-primary/40 hover:bg-muted/40 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
            )}
          >
            <span className="font-medium text-foreground">
              {t('jobs.title', 'Extraction Jobs')}
            </span>
            <ArrowRight
              size={11}
              aria-hidden="true"
              className="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
            />
          </Link>
          <Link
            href="/memory/automation/batch"
            className={cn(
              'group flex items-center justify-between gap-2 rounded-sm border border-border/60 bg-card px-3 py-2 text-[11px]',
              'hover:border-primary/40 hover:bg-muted/40 transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
            )}
          >
            <span className="font-medium text-foreground">
              {t('batch.title', 'Batch Extraction')}
            </span>
            <ArrowRight
              size={11}
              aria-hidden="true"
              className="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
            />
          </Link>
        </div>
      </MemorySection>

      {/* ============ Projects with overrides ============ */}
      <MemorySection
        icon={FolderCog}
        title={t('settings.sections.projectOverrides')}
        subtitle={
          overrideEntries.length > 0
            ? `${overrideEntries.length} custom`
            : t('settings.projectOverridesEmpty')
        }
        accent="purple"
        delay={0.25}
      >
        {settingsQuery.isLoading ? (
          <div className="space-y-2" aria-busy="true">
            {[0, 1].map((i) => (
              <div
                key={i}
                className="h-10 animate-pulse rounded-sm bg-muted/40"
              />
            ))}
          </div>
        ) : overrideEntries.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            {t('settings.projectOverridesEmpty')}
          </p>
        ) : (
          <ul className="space-y-2">
            {overrideEntries.map(([projectId, override]) => {
              const fieldCount = countOverrideFields(override);
              return (
                <li key={projectId}>
                  <Link
                    href={`/memory/projects/${encodeURIComponent(projectId)}`}
                    className={cn(
                      'group flex items-center justify-between gap-2 rounded-sm border border-border/60 bg-card px-3 py-2 text-[11px]',
                      'hover:border-primary/40 hover:bg-muted/40 transition-colors',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
                    )}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-foreground font-semibold">
                        {projectId}
                      </span>
                      <span className="text-[10px] text-muted-foreground">
                        {fieldCount}{' '}
                        {fieldCount === 1 ? 'custom field' : 'custom fields'}
                      </span>
                    </span>
                    <ArrowRight
                      size={11}
                      aria-hidden="true"
                      className="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
                    />
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </MemorySection>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function countOverrideFields(override: Record<string, unknown> | object): number {
  let count = 0;
  for (const value of Object.values(override)) {
    if (value === undefined) continue;
    if (value === null) continue;
    count += 1;
  }
  return count;
}
