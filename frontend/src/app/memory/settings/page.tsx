// src/app/memory/settings/page.tsx
/**
The memory settings page (`/memory/settings`).
Reachable even when `mode === 'disabled'` -- this page is the
only path back to a working memory subsystem (spec §1.3 / §6.1).
Layout:
Page title (matches the dashboard's heading style).
Sidebar with tabs that toggle sections visibility.
`<SettingsForm>` -- renders the five settings sections.
`Projects with overrides` section -- the projects that
have customized configuration, each row linking to the
project detail page where the override is edited.
*/
'use client';
import { useState } from 'react';
import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import {
  ArrowRight,
  Brain,
  BrainCircuit,
  FolderCog,
  ListChecks,
  Settings as SettingsIcon,
} from 'lucide-react';
import { useMemorySettings } from '../../../features/memory/api/queries';
import { SettingsForm } from '../../../features/memory/components/SettingsForm';
import { AISettingsForm } from '../../../features/memory/components/AISettingsForm';
import { MemoryRetrievalSettingsForm } from '../../../features/memory/components/MemoryRetrievalSettingsForm';
import { AgenticSettingsForm } from '../../../features/memory/components/AgenticSettingsForm';
import { MemorySection } from '../../../features/memory/components/MemorySection';
import { cn } from '../../../features/memory/lib/cn';

// ---------------------------------------------------------------------------
// Sidebar Navigation Items
// ---------------------------------------------------------------------------
const sidebarItems = [
  { id: 'general', labelKey: 'settings.sidebar.general', icon: SettingsIcon, defaultLabel: 'General Settings' },
  { id: 'semantic', labelKey: 'settings.sidebar.semanticSearch', icon: Brain, defaultLabel: 'Semantic Search' },
  // Phase 6: agentic retrieval + knowledge graph. A DEDICATED tab
  // rather than a section under "Semantic Search": both features are
  // LLM-heavy, both are opt-in, and both have their own model +
  // indexing prerequisites. Burying them under a tab whose heading
  // says "embeddings" would make them undiscoverable.
  { id: 'agentic', labelKey: 'settings.sidebar.agentic', icon: BrainCircuit, defaultLabel: 'Agentic & Graph' },
  { id: 'automation', labelKey: 'settings.sidebar.automation', icon: ListChecks, defaultLabel: 'Automation' },
  { id: 'projects', labelKey: 'settings.sidebar.projects', icon: FolderCog, defaultLabel: 'Projects' },
] as const;

type TabId = typeof sidebarItems[number]['id'];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------
export default function MemorySettingsPage() {
  const { t } = useTranslation('memory');
  const [activeTab, setActiveTab] = useState<TabId>('general');
  const settingsQuery = useMemorySettings();
  const overrides = settingsQuery.data?.project_overrides ?? {};
  const overrideEntries = Object.entries(overrides);

  return (
    <div className="flex flex-col lg:flex-row gap-6">
      {/* ============ Sidebar ============ */}
      <aside className="lg:w-64 lg:flex-shrink-0">
        <div className="lg:sticky lg:top-4">
          {/* Page Title */}
          <div className="mb-4">
            <h2 className="text-lg font-semibold font-heading text-foreground">
              {t('settings.title')}
            </h2>
            <p className="text-[11px] text-muted-foreground">
              {t('settings.description')}
            </p>
          </div>

          {/* Navigation Tabs */}
          <nav className="space-y-1">
            {sidebarItems.map((item) => {
              const Icon = item.icon;
              const isActive = activeTab === item.id;
              return (
                <button
                  key={item.id}
                  onClick={() => setActiveTab(item.id)}
                  className={cn(
                    'group w-full flex items-center gap-2 rounded-sm px-3 py-2 text-[11px] transition-all',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
                    isActive
                      ? 'bg-primary/10 text-primary font-semibold border-l-2 border-primary'
                      : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground',
                  )}
                >
                  <Icon
                    size={14}
                    aria-hidden="true"
                    className={cn(
                      'shrink-0 transition-colors',
                      isActive ? 'text-primary' : 'text-muted-foreground group-hover:text-foreground',
                    )}
                  />
                  <span className="font-medium text-left">
                    {t(item.labelKey, item.defaultLabel)}
                  </span>
                </button>
              );
            })}
          </nav>
        </div>
      </aside>

      {/* ============ Main Content ============ */}
      <main className="flex-1 min-w-0 space-y-6">
        {/* ============ General Settings ============ */}
        <section
          id="general"
          className={cn(
            'transition-all duration-200',
            activeTab === 'general' ? 'block' : 'hidden',
          )}
        >
          <SettingsForm />
        </section>

        {/* ============ Semantic search (Phase 4 + Phase 5A) ============ */}
        <section
          id="semantic"
          className={cn(
            'transition-all duration-200 space-y-6',
            activeTab === 'semantic' ? 'block' : 'hidden',
          )}
        >
          {/* Retrieval strategy: mode, fusion tuning, LLM reranker. */}
          <MemoryRetrievalSettingsForm />

          {/* Embedding model: pointer to the dedicated page that
              manages model selection, reindex, and dimension state. */}
          <MemorySection
            icon={Brain}
            title={t('settings.sections.semanticSearch')}
            subtitle={t('settings.embedding.sectionSubtitle')}
            accent="cyan"
            delay={0.25}
          >
            <Link
              href="/memory/embeddings"
              className={cn(
                'group flex items-center justify-between gap-2 rounded-sm border border-border/60 bg-card px-3 py-2 text-[11px]',
                'hover:border-primary/40 hover:bg-muted/40 transition-colors',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
              )}
            >
              <div className="min-w-0 flex-1">
                <span className="block font-medium text-foreground">
                  {t('settings.embedding.linkTitle')}
                </span>
                <span className="text-[10px] text-muted-foreground">
                  {settingsQuery.data?.embedding
                    ? settingsQuery.data.embedding.enabled
                      ? t('settings.embedding.state.enabledWithModel', {
                          model:
                            settingsQuery.data.embedding.model_key ??
                            t('settings.embedding.state.noModel'),
                        })
                      : t('settings.embedding.state.disabled')
                    : t('settings.embedding.linkDescription')}
                </span>
              </div>
              <ArrowRight
                size={11}
                aria-hidden="true"
                className="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
              />
            </Link>
          </MemorySection>
        </section>

        {/* ============ Agentic search + knowledge graph (Phase 6) ============ */}
        <section
          id="agentic"
          className={cn(
            'transition-all duration-200 space-y-6',
            activeTab === 'agentic' ? 'block' : 'hidden',
          )}
        >
          {/* Nine retrieval fields + four `ai` fields in one form:
              the search flags, the agentic model, and the graph
              INDEXING controls. Splitting them across tabs would
              recreate the exact confusion Phase 6 ships with --
              turning search on and never populating the graph. */}
          <AgenticSettingsForm />
        </section>

        {/* ============ Automation & AI Settings ============ */}
        <section
          id="automation"
          className={cn(
            'transition-all duration-200 space-y-4',
            activeTab === 'automation' ? 'block' : 'hidden',
          )}
        >
          {/* Full AI/extraction/batch settings form.
              Renders every MemoryAISettings field including
              `batch_auto_schedule_hours` -- the primary goal
              of Task 4.13. */}
          <AISettingsForm />

          {/* Quick links to the operations surfaces where the
              configured settings actually take effect. */}
          <MemorySection
            icon={ListChecks}
            title={t('settings.sections.quickLinks', 'Quick Links')}
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
              <Link
                href="/memory/diagnostics"
                className={cn(
                  'group flex items-center justify-between gap-2 rounded-sm border border-border/60 bg-card px-3 py-2 text-[11px]',
                  'hover:border-primary/40 hover:bg-muted/40 transition-colors',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
                )}
              >
                <span className="font-medium text-foreground">
                  {t('diagnostics.title', 'Diagnostics')}
                </span>
                <ArrowRight
                  size={11}
                  aria-hidden="true"
                  className="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
                />
              </Link>
            </div>
          </MemorySection>
        </section>

        {/* ============ Projects with overrides ============ */}
        <section
          id="projects"
          className={cn(
            'transition-all duration-200',
            activeTab === 'projects' ? 'block' : 'hidden',
          )}
        >
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
        </section>
      </main>
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
