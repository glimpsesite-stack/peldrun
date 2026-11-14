// src/app/memory/usage/page.tsx

/**
 * Usage dashboard (`/memory/usage`).
 *
 * Design aligned with SearchTab and the memory Dashboard:
 *
 *   * Header with title + period selector (a segmented control).
 *   * Four KPI cards via `UsageKpiCards` (uses `MemorySummaryCard`
 *     internally).
 *   * Two chart sections via `MemorySection`, each with a `<select>`
 *     in its action slot to change granularity / dimension without
 *     leaving the page.
 *
 * Failure isolation preserved from the pre-redesign version:
 *
 *   * Each query (summary / timeseries / breakdown) fails
 *     independently. A failed summary does not hide the charts,
 *     and a failed chart does not hide the summary.
 *
 * A11y:
 *
 *   * Charts are wrapped in `role="img"` + `aria-label` so screen
 *     readers announce the chart's purpose and the current period.
 *   * The period selector uses `aria-pressed` on its buttons.
 *   * Each `<select>` has an `aria-label`.
 */

'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  BarChart3,
  PieChart,
  RefreshCw,
  TrendingUp,
} from 'lucide-react';

import type {
  UsagePeriod,
  UsageGranularity,
} from '../../../features/memory/types/enums';
import type { MemoryApiError } from '../../../features/memory/types/errors';
import {
  useMemorySettings,
  useUsageBreakdown,
  useUsageSummary,
  useUsageTimeseries,
} from '../../../features/memory/api/queries';
import { cn } from '../../../features/memory/lib/cn';
import { formatPercent } from '../../../features/memory/lib/format';
import { UsageKpiCards } from '../../../features/memory/components/UsageKpiCards';
import { UsageBreakdownChart } from '../../../features/memory/components/UsageBreakdownChart';
import { UsageTimeseriesChart } from '../../../features/memory/components/UsageTimeseriesChart';
import { EmptyState } from '../../../features/memory/components/EmptyState';
import { MemorySection } from '../../../features/memory/components/MemorySection';
import { MemoryBanner } from '../../../features/memory/components/MemoryBanner';

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

const PERIOD_OPTIONS: Array<{ value: UsagePeriod; labelKey: string }> = [
  { value: 'day', labelKey: 'usage.period.day' },
  { value: 'week', labelKey: 'usage.period.week' },
  { value: 'month', labelKey: 'usage.period.month' },
  { value: 'all', labelKey: 'usage.period.all' },
];

const GRANULARITY_OPTIONS: Array<{
  value: UsageGranularity;
  labelKey: string;
}> = [
  { value: 'day', labelKey: 'usage.timeseries.granularity.day' },
  { value: 'week', labelKey: 'usage.timeseries.granularity.week' },
  { value: 'month', labelKey: 'usage.timeseries.granularity.month' },
];

const DIMENSION_OPTIONS: Array<{
  value: 'scope' | 'project' | 'chat';
  labelKey: string;
}> = [
  { value: 'scope', labelKey: 'usage.breakdown.dimensions.scope' },
  { value: 'project', labelKey: 'usage.breakdown.dimensions.project' },
  { value: 'chat', labelKey: 'usage.breakdown.dimensions.chat' },
];

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function MemoryUsagePage() {
  const { t } = useTranslation('memory');

  const [period, setPeriod] = useState<UsagePeriod>('month');
  const [granularity, setGranularity] = useState<UsageGranularity>('day');
  const [breakdownBy, setBreakdownBy] = useState<
    'scope' | 'project' | 'chat'
  >('scope');

  const settingsQuery = useMemorySettings();
  const summaryQuery = useUsageSummary({
    period,
    chat_id: null,
    project_id: null,
  });
  const timeseriesQuery = useUsageTimeseries({ period, granularity });
  const breakdownQuery = useUsageBreakdown({
    by: breakdownBy,
    period,
    chat_id: null,
    project_id: null,
  });

  const warnThreshold = settingsQuery.data?.token_budget.warn_threshold;
  const summary = summaryQuery.data;
  const isEmpty = summary !== undefined && summary.run_count === 0;

  return (
    <div className="space-y-6">
      {/* ============ Title + period selector ============ */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold font-heading text-foreground">
            {t('usage.title')}
          </h2>
          <p className="text-[11px] text-muted-foreground">
            {t('usage.description')}
          </p>
        </div>

        <div
          className="inline-flex rounded-md border border-border bg-card p-0.5 shadow-sm"
          role="group"
          aria-label={t('usage.period.label')}
        >
          {PERIOD_OPTIONS.map((opt) => {
            const active = period === opt.value;
            return (
              <button
                key={opt.value}
                type="button"
                onClick={() => setPeriod(opt.value)}
                aria-pressed={active}
                className={cn(
                  'rounded px-2.5 py-1 text-[11px] font-medium transition-all',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 focus-visible:ring-offset-background',
                  active
                    ? 'bg-primary text-primary-foreground shadow-sm'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                )}
              >
                {t(opt.labelKey)}
              </button>
            );
          })}
        </div>
      </div>

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
          action={
            <button
              type="button"
              onClick={() => summaryQuery.refetch()}
              className="inline-flex items-center gap-1 rounded border border-current px-2 py-0.5 font-medium hover:opacity-80"
            >
              <RefreshCw size={10} aria-hidden="true" />
              {t('common.retry')}
            </button>
          }
        />
      ) : isEmpty ? (
        <EmptyState
          icon={BarChart3}
          title={t('usage.empty')}
          size="spacious"
        />
      ) : summary ? (
        <>
          <UsageKpiCards
            summary={summary}
            warnThreshold={warnThreshold}
          />
          {summary.drop_rate > 0.3 && (
            <MemoryBanner
              tone="warning"
              message={t('usage.budgetWarning', {
                pct: formatPercent(summary.drop_rate),
              })}
            />
          )}
        </>
      ) : null}

      {/* ============ Charts ============ */}
      {!isEmpty && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* ---- Timeseries ---- */}
          <MemorySection
            icon={TrendingUp}
            title={t('usage.timeseries.title')}
            subtitle={t(`usage.period.${period}`)}
            accent="primary"
            delay={0.2}
            action={
              <select
                value={granularity}
                onChange={(e) =>
                  setGranularity(e.target.value as UsageGranularity)
                }
                aria-label={t('usage.timeseries.granularity.day')}
                className="h-6 rounded border border-border bg-background px-1.5 text-[10px] font-mono text-foreground cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
              >
                {GRANULARITY_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {t(o.labelKey)}
                  </option>
                ))}
              </select>
            }
          >
            {timeseriesQuery.isLoading ? (
              <ChartSkeleton height={240} />
            ) : timeseriesQuery.error ? (
              <ErrorCard
                error={timeseriesQuery.error}
                onRetry={() => timeseriesQuery.refetch()}
              />
            ) : timeseriesQuery.data ? (
              <div
                role="img"
                aria-label={`${t('usage.timeseries.title')} — ${t(`usage.period.${period}`)}`}
              >
                <UsageTimeseriesChart
                  data={timeseriesQuery.data}
                  height={240}
                />
              </div>
            ) : null}
          </MemorySection>

          {/* ---- Breakdown ---- */}
          <MemorySection
            icon={PieChart}
            title={t('usage.breakdown.title', {
              dimension: t(
                `usage.breakdown.dimensions.${breakdownBy}`,
              ),
            })}
            subtitle={t(`usage.period.${period}`)}
            accent="cyan"
            delay={0.25}
            action={
              <select
                value={breakdownBy}
                onChange={(e) =>
                  setBreakdownBy(
                    e.target.value as 'scope' | 'project' | 'chat',
                  )
                }
                aria-label={t('usage.breakdown.dimensions.scope')}
                className="h-6 rounded border border-border bg-background px-1.5 text-[10px] font-mono text-foreground cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1"
              >
                {DIMENSION_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {t(o.labelKey)}
                  </option>
                ))}
              </select>
            }
          >
            {breakdownQuery.isLoading ? (
              <ChartSkeleton height={220} />
            ) : breakdownQuery.error ? (
              <ErrorCard
                error={breakdownQuery.error}
                onRetry={() => breakdownQuery.refetch()}
              />
            ) : breakdownQuery.data ? (
              <div
                role="img"
                aria-label={`${t('usage.breakdown.title', {
                  dimension: t(
                    `usage.breakdown.dimensions.${breakdownBy}`,
                  ),
                })} — ${t(`usage.period.${period}`)}`}
              >
                <UsageBreakdownChart
                  data={breakdownQuery.data}
                  height={220}
                />
              </div>
            ) : null}
          </MemorySection>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface ErrorCardProps {
  error: MemoryApiError;
  onRetry: () => void;
}

function ErrorCard({ error, onRetry }: ErrorCardProps) {
  const { t } = useTranslation('memory');
  return (
    <div
      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[11px] text-destructive"
      role="alert"
    >
      <span className="min-w-0 flex-1 truncate">{error.message}</span>
      <button
        type="button"
        onClick={onRetry}
        className="inline-flex items-center gap-1 rounded border border-destructive/40 bg-card px-2 py-0.5 font-medium text-destructive hover:bg-destructive/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive transition-colors"
      >
        <RefreshCw size={10} aria-hidden="true" />
        {t('common.retry')}
      </button>
    </div>
  );
}

function ChartSkeleton({ height }: { height: number }) {
  return (
    <div
      className="w-full animate-pulse rounded-md bg-muted/40"
      style={{ height }}
      aria-busy="true"
    />
  );
}
