// src/app/memory/page.tsx

/**
 * The memory dashboard home (`/memory`).
 *
 * Design language aligned with the app's SearchTab:
 *
 *   * Ambient background + sticky header come from the parent
 *     `MemoryLayout` -- this page only renders its content
 *     column (`space-y-6`).
 *   * Sections use `MemorySection` (bg-gigbg surface + accent
 *     line + FADE_UP entry).
 *   * KPI cards use `MemorySummaryCard` (gradient line + corner
 *     glow + hover lift).
 *   * Alerts use `MemoryBanner` (tone-coded colors + icon).
 *
 * Content order:
 *
 *   1. Page title + period label.
 *   2. Disabled banner (when `mode === 'disabled'`).
 *   3. Four KPI cards (runs, tokens, budget, drop rate) -- no
 *      section wrapper, matching SearchTab's top-level cards.
 *   4. Warning banner for high budget / drop rate.
 *   5. Tokens-over-time chart (weekly granularity).
 *   6. Quick actions (four links).
 *   7. Recent projects (top 5 by last_updated).
 *
 * Every section renders independently -- if the summary query
 * fails, the quick actions and project list still work.
 */

'use client';

import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import {
  Activity,
  BarChart3,
  Brain,
  Coins,
  Download,
  FolderOpen,
  Gauge,
  Plus,
  TrendingDown,
  Upload,
  type LucideIcon,
} from 'lucide-react';

import {
  useMemorySettings,
  useProjectsList,
  useUsageSummary,
  useUsageTimeseries,
} from '../../features/memory/api/queries';
import { UsageTimeseriesChart } from '../../features/memory/components/UsageTimeseriesChart';
import { ProjectCard } from '../../features/memory/components/ProjectCard';
import { EmptyState } from '../../features/memory/components/EmptyState';
import { MemorySection } from '../../features/memory/components/MemorySection';
import { MemorySummaryCard } from '../../features/memory/components/MemorySummaryCard';
import { MemoryBanner } from '../../features/memory/components/MemoryBanner';
import { cn } from '../../features/memory/lib/cn';
import {
  formatTokens,
  formatPercent,
  formatCount,
} from '../../features/memory/lib/format';

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function MemoryDashboardPage() {
  const { t } = useTranslation('memory');

  const settingsQuery = useMemorySettings();
  const summaryQuery = useUsageSummary({
    period: 'month',
    chat_id: null,
    project_id: null,
  });
  const timeseriesQuery = useUsageTimeseries({
    period: 'week',
    granularity: 'day',
  });
  const projectsQuery = useProjectsList();

  const mode = settingsQuery.data?.mode;
  const isDisabled = mode === 'disabled';
  const warnThreshold = settingsQuery.data?.token_budget.warn_threshold;

  const summary = summaryQuery.data;
  const isEmpty = summary !== undefined && summary.run_count === 0;

  const recentProjects = [...(projectsQuery.data?.projects ?? [])]
    .sort((a, b) => b.last_updated - a.last_updated)
    .slice(0, 5);

  // Budget accent: green under 50%, amber 50-79%, red 80%+
  const budgetAccent = !summary
    ? '#3b82f6'
    : summary.budget_utilization >= 0.8
      ? '#f43f5e'
      : summary.budget_utilization >= 0.5
        ? '#f59e0b'
        : '#10b981';

  // Drop-rate accent: green under 10%, amber 10-29%, red 30%+
  const dropAccent = !summary
    ? '#3b82f6'
    : summary.drop_rate >= 0.3
      ? '#f43f5e'
      : summary.drop_rate >= 0.1
        ? '#f59e0b'
        : '#10b981';

  return (
    <div className="space-y-6">
      {/* ============ Title ============ */}
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold font-heading text-foreground">
            {t('dashboard.title')}
          </h2>
          <p className="text-[11px] text-muted-foreground">
            {t('dashboard.subtitle')}
          </p>
        </div>
        {mode && (
          <span className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
            {t('usage.period.label')}: {t('usage.period.month')}
          </span>
        )}
      </div>

      {/* ============ Disabled banner ============ */}
      {isDisabled && (
        <MemoryBanner
          tone="warning"
          title={t('mode.disabled.name')}
          message={t('mode.disabled.description')}
          action={
            <Link
              href="/memory/settings"
              className="inline-flex h-7 items-center gap-1.5 rounded-md bg-amber-600 px-2.5 text-[11px] font-medium text-white hover:bg-amber-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2"
            >
              {t('mode.enable')}
            </Link>
          }
        />
      )}

      {/* ============ KPI cards ============ */}
      {summaryQuery.isLoading ? (
        <div
          className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4"
          aria-busy="true"
        >
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-32 animate-pulse rounded-md border border-border bg-muted/40"
            />
          ))}
        </div>
      ) : summaryQuery.error ? (
        <MemoryBanner
          tone="error"
          message={summaryQuery.error.message}
        />
      ) : isEmpty ? (
        <EmptyState
          icon={Brain}
          title={t('usage.empty')}
          description={t('dashboard.quickActionsHint.newMemory')}
          size="spacious"
        />
      ) : summary ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <MemorySummaryCard
            label={t('usage.kpis.runs')}
            value={formatCount(summary.run_count)}
            icon={<Activity size={18} />}
            accent="#3b82f6"
            subtitle={t('usage.period.month')}
            delay={0}
          />
          <MemorySummaryCard
            label={t('usage.kpis.totalTokens')}
            value={formatTokens(summary.total_tokens, t)}
            icon={<Coins size={18} />}
            accent="#8b5cf6"
            subtitle={`${formatPercent(summary.budget_utilization)} ${t('usage.kpis.budgetUtil').toLowerCase()}`}
            delay={0.05}
          />
          <MemorySummaryCard
            label={t('usage.kpis.budgetUtil')}
            value={formatPercent(summary.budget_utilization)}
            icon={<Gauge size={18} />}
            accent={budgetAccent}
            subtitle={`${formatCount(summary.entries_used)} / ${formatCount(summary.entries_retrieved)}`}
            delay={0.1}
          />
          <MemorySummaryCard
            label={t('usage.kpis.dropRate')}
            value={formatPercent(summary.drop_rate)}
            icon={<TrendingDown size={18} />}
            accent={dropAccent}
            subtitle={`${formatCount(summary.entries_dropped)} ${t('usage.kpis.entriesDropped').toLowerCase()}`}
            delay={0.15}
          />
        </div>
      ) : null}

      {/* ============ Warning banners ============ */}
      {summary &&
        warnThreshold !== undefined &&
        summary.budget_utilization >= warnThreshold && (
          <MemoryBanner
            tone="warning"
            message={t('usage.budgetWarning', {
              pct: formatPercent(summary.budget_utilization),
            })}
          />
        )}

      {/* ============ Tokens over time ============ */}
      {timeseriesQuery.data && timeseriesQuery.data.points.length > 0 && (
        <MemorySection
          icon={BarChart3}
          title={t('usage.timeseries.title')}
          subtitle={t('usage.period.week')}
          accent="primary"
          delay={0.2}
        >
          <UsageTimeseriesChart
            data={timeseriesQuery.data}
            height={180}
          />
        </MemorySection>
      )}

      {/* ============ Quick actions ============ */}
      <MemorySection
        icon={Plus}
        title={t('dashboard.quickActions')}
        subtitle={t('dashboard.subtitle')}
        accent="emerald"
        delay={0.25}
      >
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <QuickActionTile
            href="/memory/browse"
            icon={Plus}
            label={t('entry.drafts.createTitle')}
            hint={t('dashboard.quickActionsHint.newMemory')}
            accent="primary"
          />
          <QuickActionTile
            href="/memory/import"
            icon={Upload}
            label={t('nav.import')}
            hint={t('dashboard.quickActionsHint.import')}
            accent="emerald"
          />
          <QuickActionTile
            href="/memory/export"
            icon={Download}
            label={t('nav.export')}
            hint={t('dashboard.quickActionsHint.export')}
            accent="cyan"
          />
          <QuickActionTile
            href="/memory/usage"
            icon={BarChart3}
            label={t('nav.usage')}
            hint={t('dashboard.quickActionsHint.usage')}
            accent="purple"
          />
        </div>
      </MemorySection>

      {/* ============ Recent projects ============ */}
      <MemorySection
        icon={FolderOpen}
        title={t('dashboard.recentActivity')}
        subtitle={
          projectsQuery.data
            ? `${formatCount(projectsQuery.data.projects.length)} ${t('nav.projects').toLowerCase()}`
            : undefined
        }
        accent="cyan"
        delay={0.3}
        action={
          recentProjects.length > 0 ? (
            <Link
              href="/memory/projects"
              className="inline-flex items-center gap-1 rounded px-1 text-[11px] text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            >
              {t('nav.projects')}
            </Link>
          ) : undefined
        }
      >
        {projectsQuery.isLoading ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="h-24 animate-pulse rounded-md bg-muted/40"
              />
            ))}
          </div>
        ) : recentProjects.length === 0 ? (
          <EmptyState
            icon={FolderOpen}
            title={t('dashboard.recentActivityEmpty')}
            description={t('projects.empty.description')}
          />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {recentProjects.map((project) => (
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
      </MemorySection>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Local sub-component: quick action tile
// ---------------------------------------------------------------------------

const QUICK_ACCENT: Record<
  'primary' | 'emerald' | 'cyan' | 'purple',
  { icon: string; border: string }
> = {
  primary: {
    icon: 'text-primary bg-primary/10',
    border: 'hover:border-primary/40',
  },
  emerald: {
    icon: 'text-emerald-500 bg-emerald-500/10',
    border: 'hover:border-emerald-500/40',
  },
  cyan: {
    icon: 'text-cyan-500 bg-cyan-500/10',
    border: 'hover:border-cyan-500/40',
  },
  purple: {
    icon: 'text-purple-500 bg-purple-500/10',
    border: 'hover:border-purple-500/40',
  },
};

interface QuickActionTileProps {
  href: string;
  icon: LucideIcon;
  label: string;
  hint: string;
  accent: 'primary' | 'emerald' | 'cyan' | 'purple';
}

function QuickActionTile({
  href,
  icon: Icon,
  label,
  hint,
  accent,
}: QuickActionTileProps) {
  const styles = QUICK_ACCENT[accent];
  return (
    <Link
      href={href}
      className={cn(
        'group flex flex-col gap-2.5 rounded-md border border-border bg-card p-4 text-start transition-all',
        'hover:bg-muted/30 hover:shadow-sm',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-gigbg',
        styles.border,
      )}
    >
      <div
        className={cn(
          'flex h-8 w-8 items-center justify-center rounded-md transition-transform group-hover:scale-110',
          styles.icon,
        )}
        aria-hidden="true"
      >
        <Icon size={14} strokeWidth={2.2} />
      </div>
      <div className="min-w-0">
        <p className="text-xs font-semibold text-foreground">{label}</p>
        <p className="mt-0.5 text-[10px] leading-snug text-muted-foreground">
          {hint}
        </p>
      </div>
    </Link>
  );
}
