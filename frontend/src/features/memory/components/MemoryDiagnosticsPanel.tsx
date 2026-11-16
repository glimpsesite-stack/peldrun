// src/features/memory/components/MemoryDiagnosticsPanel.tsx

/**
 * Diagnostic overview of the AI extraction subsystem.
 *
 * A "doctor's summary" panel: at-a-glance health, a live model
 * probe, and per-run quality metrics. Designed to answer the
 * three questions an operator actually asks when something
 * looks wrong:
 *
 *   1. **"Is the subsystem on and configured?"** -- the health
 *      banner + configuration summary.
 *
 *   2. **"Can the model actually respond?"** -- the Test Model
 *      probe, which exercises the SAME gateway path a real
 *      extraction job uses (via `POST /automation/test-model`).
 *
 *   3. **"Are extractions working?"** -- recent failure list
 *      and the candidate-to-memory conversion rate across the
 *      last 20 jobs.
 *
 * What this panel is NOT
 * ----------------------
 *
 *   * It is NOT a replacement for `/memory/jobs` or
 *     `/memory/automation/batch`. Those pages handle per-item
 *     browsing and per-item actions. The panel links to them;
 *     it does not duplicate their tables.
 *
 *   * It does NOT auto-poll. Diagnostics is a manual visit --
 *     the user opens the panel, sees the state, closes it. The
 *     Test Model button provides an on-demand freshness check
 *     that is more meaningful than background polling (a
 *     "healthy" probe result is only interesting at the moment
 *     the user clicks it).
 *
 *   * It does NOT expose the full settings form. Editing AI
 *     configuration belongs in `/memory/settings`; the panel
 *     shows the current values as READ-ONLY summary and links
 *     to the settings page for changes.
 *
 * Test Model semantics
 * --------------------
 *
 * The `/automation/test-model` endpoint returns HTTP 200 for
 * BOTH success and model-level failure (see its response type
 * docs for the reasoning). The panel therefore distinguishes
 * THREE outcomes:
 *
 *   * `data.status === 'ok'`     -- model reached + parsed
 *                                   correctly. Show latency,
 *                                   tokens, preview.
 *   * `data.status === 'error'`  -- model unreachable OR
 *                                   unparseable. Show error
 *                                   type + message.
 *   * `mutation.error`           -- HTTP failure (503, 500).
 *                                   Show the network error.
 *
 * All three are rendered with distinct color + icon so the
 * operator can distinguish "model misconfigured" (fixable in
 * settings) from "backend down" (retry / restart).
 *
 * Provider override contract
 * --------------------------
 *
 * `ai.extraction.provider_id` may legitimately be `null` --
 * the extraction worker infers it from the model vault at
 * dispatch time (see `MemoryService._resolve_model_provider`).
 * The test-model endpoint, however, requires an EXPLICIT
 * `provider_id` in the request body (it does not have access
 * to the worker's inference path).
 *
 * To bridge that gap the panel exposes two small override
 * inputs. They PRE-FILL from the current settings when values
 * are present, and stay empty otherwise (with a helpful
 * placeholder hinting at the common providers). The user types
 * the provider only when they need to -- which is exactly once
 * per workspace, in the null-provider case.
 */

'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  Clock,
  Cpu,
  Database,
  Flame,
  Gauge,
  Loader2,
  Zap,
} from 'lucide-react';

import { cn } from '../lib/cn';
import {
  useAISettings,
  useJobsList,
} from '../api/queries';
import { useTestModel } from '../api/mutations';
import { useAIEnabled } from '../hooks/useAIEnabled';
import { nanoUsdToUsd } from '../types/batch';
import {
  isTerminalJobStatus,
  type MemoryJob,
} from '../types/jobs';
import type { TestModelResponse } from '../types/responses';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface MemoryDiagnosticsPanelProps {
  /** Extra classes for the outer container. */
  className?: string;
}

// ---------------------------------------------------------------------------
// Health model
// ---------------------------------------------------------------------------

type _HealthLevel = 'ok' | 'warning' | 'error' | 'off' | 'idle';

interface _HealthStatus {
  level: _HealthLevel;
  label: string;
  description: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function MemoryDiagnosticsPanel({
  className,
}: MemoryDiagnosticsPanelProps) {
  const ai = useAIEnabled();
  const aiSettingsQuery = useAISettings();

  // Current configured model, as a fallback for the test inputs.
  const configuredProviderId =
    aiSettingsQuery.data?.ai.extraction.provider_id ?? '';
  const configuredModelId =
    aiSettingsQuery.data?.ai.extraction.model_id ?? '';

  // Override inputs for the test probe. Empty by default so the
  // `effective*` computation falls back to the configured value.
  const [testProviderOverride, setTestProviderOverride] = useState('');
  const [testModelOverride, setTestModelOverride] = useState('');

  const effectiveTestProvider =
    testProviderOverride.trim() || configuredProviderId;
  const effectiveTestModel =
    testModelOverride.trim() || configuredModelId;
  const canRunTest = Boolean(
    effectiveTestProvider && effectiveTestModel,
  );

  const testModelMutation = useTestModel();

  // -------------------------------------------------------------------------
  // Recent jobs queries
  // -------------------------------------------------------------------------
  //
  // Two queries:
  //
  //   * `recentJobsQuery` -- the last 20 jobs (any status) for
  //     quality stats (conversion, cost, success/fail split).
  //
  //   * `recentFailuresQuery` -- the last 5 failed jobs for the
  //     diagnostic "what went wrong?" list. A separate query
  //     because the parent list might not include failures if
  //     all recent jobs succeeded (which is the healthy case).
  //
  // Both use the SAME cache key namespace as the jobs page --
  // a visit to `/memory/jobs` and a return to diagnostics share
  // the cache (assuming pagination params match).

  const recentJobsQuery = useJobsList({
    status: null,
    chat_id: null,
    trigger_type: null,
    limit: 20,
    offset: 0,
  });

  const recentFailuresQuery = useJobsList({
    status: 'failed',
    chat_id: null,
    trigger_type: null,
    limit: 5,
    offset: 0,
  });

  // -------------------------------------------------------------------------
  // Derived: health + quality stats
  // -------------------------------------------------------------------------

  const health = useMemo<_HealthStatus>(() => {
    if (ai.isLoading) {
      return {
        level: 'idle',
        label: 'Checking…',
        description: 'Loading AI settings.',
      };
    }
    if (!ai.isEnabled) {
      return {
        level: 'off',
        label: 'Disabled',
        description:
          'AI extraction is turned off in settings. No jobs will run.',
      };
    }
    if (!ai.isAvailable) {
      return {
        level: 'warning',
        label: 'Not configured',
        description:
          'Extraction is on, but no model has been selected yet.',
      };
    }
    const jobs = recentJobsQuery.data?.jobs ?? [];
    if (jobs.length === 0) {
      return {
        level: 'idle',
        label: 'Ready',
        description:
          'Configuration is complete. No jobs have run yet.',
      };
    }
    const failed = jobs.filter((j) => j.status === 'failed').length;
    const completed = jobs.filter(
      (j) => j.status === 'completed',
    ).length;
    if (failed > 0 && completed === 0) {
      return {
        level: 'error',
        label: 'All failing',
        description: `${failed} recent job(s) failed with 0 successes. Check the failures below.`,
      };
    }
    if (failed > 0) {
      return {
        level: 'warning',
        label: 'Some failures',
        description: `${failed} of ${jobs.length} recent job(s) failed.`,
      };
    }
    return {
      level: 'ok',
      label: 'Healthy',
      description: `Last ${jobs.length} job(s) completed successfully.`,
    };
  }, [ai, recentJobsQuery.data?.jobs]);

  const qualityStats = useMemo(() => {
    const jobs = recentJobsQuery.data?.jobs ?? [];
    const terminal = jobs.filter((j) =>
      isTerminalJobStatus(j.status),
    );
    const successful = terminal.filter(
      (j) => j.status === 'completed',
    );
    const totalCandidates = successful.reduce(
      (sum, j) => sum + j.candidates_count,
      0,
    );
    const totalCreated = successful.reduce(
      (sum, j) => sum + j.created_memories_count,
      0,
    );
    const totalCostNano = successful.reduce(
      (sum, j) => sum + j.cost_nano_usd,
      0,
    );
    const rejectedAtWrite = Math.max(
      0,
      totalCandidates - totalCreated,
    );
    const conversionRate =
      totalCandidates > 0 ? totalCreated / totalCandidates : 0;
    return {
      terminalJobCount: terminal.length,
      successfulJobCount: successful.length,
      totalCandidates,
      totalCreated,
      rejectedAtWrite,
      conversionRate,
      totalCostNano,
    };
  }, [recentJobsQuery.data?.jobs]);

  // -------------------------------------------------------------------------
  // Test handler
  // -------------------------------------------------------------------------

  const handleRunTest = async () => {
    if (!canRunTest) return;
    try {
      await testModelMutation.mutateAsync({
        provider_id: effectiveTestProvider,
        model_id: effectiveTestModel,
      });
    } catch {
      // Errors surface via mutation state -- no toast needed.
    }
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className={cn('space-y-4', className)}>
      {/* ============ Health banner ============ */}
      <_HealthBanner health={health} />

      {/* ============ Configuration summary ============ */}
      <_ConfigSummary
        enabled={ai.isEnabled}
        available={ai.isAvailable}
        extractionTrigger={
          aiSettingsQuery.data?.ai.extraction_trigger ?? null
        }
        configuredModel={configuredModelId || null}
        configuredProvider={configuredProviderId || null}
      />

      {/* ============ Test Model ============ */}
      <_TestModelSection
        providerOverride={testProviderOverride}
        modelOverride={testModelOverride}
        configuredProvider={configuredProviderId}
        configuredModel={configuredModelId}
        onProviderChange={setTestProviderOverride}
        onModelChange={setTestModelOverride}
        canRunTest={canRunTest}
        isPending={testModelMutation.isPending}
        result={testModelMutation.data}
        error={testModelMutation.error}
        onRun={handleRunTest}
      />

      {/* ============ Quality stats ============ */}
      <_QualityStats
        stats={qualityStats}
        isLoading={recentJobsQuery.isPending}
      />

      {/* ============ Recent failures ============ */}
      <_RecentFailures
        jobs={recentFailuresQuery.data?.jobs ?? []}
        isLoading={recentFailuresQuery.isPending}
        total={recentFailuresQuery.data?.total ?? 0}
      />

      {/* ============ Quick links ============ */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <_QuickLink
          href="/memory/jobs"
          label="View all jobs"
          description="Filter, retry, cancel individual jobs."
        />
        <_QuickLink
          href="/memory/automation/batch"
          label="Batch extraction"
          description="Run a retroactive batch across pending sessions."
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function _HealthBanner({ health }: { health: _HealthStatus }) {
  const styles: Record<
    _HealthLevel,
    { bg: string; border: string; icon: React.ReactNode; text: string }
  > = {
    ok: {
      bg: 'bg-emerald-500/5',
      border: 'border-emerald-500/20',
      icon: (
        <CheckCircle2
          size={12}
          className="shrink-0 text-emerald-600 dark:text-emerald-400"
          aria-hidden="true"
        />
      ),
      text: 'text-emerald-700 dark:text-emerald-300',
    },
    warning: {
      bg: 'bg-amber-500/5',
      border: 'border-amber-500/20',
      icon: (
        <AlertTriangle
          size={12}
          className="shrink-0 text-amber-600 dark:text-amber-400"
          aria-hidden="true"
        />
      ),
      text: 'text-amber-700 dark:text-amber-300',
    },
    error: {
      bg: 'bg-red-500/5',
      border: 'border-red-500/20',
      icon: (
        <AlertCircle
          size={12}
          className="shrink-0 text-red-600 dark:text-red-400"
          aria-hidden="true"
        />
      ),
      text: 'text-red-700 dark:text-red-300',
    },
    off: {
      bg: 'bg-muted/30',
      border: 'border-border/60',
      icon: (
        <Database
          size={12}
          className="shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
      ),
      text: 'text-muted-foreground',
    },
    idle: {
      bg: 'bg-blue-500/5',
      border: 'border-blue-500/20',
      icon: (
        <Clock
          size={12}
          className="shrink-0 text-blue-600 dark:text-blue-400"
          aria-hidden="true"
        />
      ),
      text: 'text-blue-700 dark:text-blue-300',
    },
  };

  const style = styles[health.level];

  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-sm border px-3 py-2',
        style.bg,
        style.border,
      )}
    >
      <div className="mt-0.5">{style.icon}</div>
      <div className="flex-1 min-w-0">
        <div
          className={cn(
            'text-[11px] font-semibold',
            style.text,
          )}
        >
          {health.label}
        </div>
        <div className="text-[10px] text-muted-foreground mt-0.5">
          {health.description}
        </div>
      </div>
    </div>
  );
}

function _ConfigSummary({
  enabled,
  available,
  extractionTrigger,
  configuredModel,
  configuredProvider,
}: {
  enabled: boolean;
  available: boolean;
  extractionTrigger: string | null;
  configuredModel: string | null;
  configuredProvider: string | null;
}) {
  return (
    <div className="rounded-sm border border-border/60 bg-card p-3">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-2">
        Current Configuration
      </h4>
      <div className="grid grid-cols-2 gap-2 text-[10px]">
        <_ConfigCell label="Enabled" value={enabled ? 'Yes' : 'No'} />
        <_ConfigCell
          label="Ready"
          value={available ? 'Yes' : 'No'}
        />
        <_ConfigCell
          label="Trigger"
          value={extractionTrigger ?? '—'}
        />
        <_ConfigCell
          label="Provider"
          value={configuredProvider ?? '(inferred)'}
          mono
        />
        <_ConfigCell
          label="Model"
          value={configuredModel ?? '—'}
          mono
          colSpan
        />
      </div>
      <Link
        href="/memory/settings"
        className="inline-flex items-center gap-0.5 mt-2 text-[10px] text-primary hover:underline"
      >
        Edit in settings
        <ChevronRight size={10} aria-hidden="true" />
      </Link>
    </div>
  );
}

function _ConfigCell({
  label,
  value,
  mono,
  colSpan,
}: {
  label: string;
  value: string;
  mono?: boolean;
  colSpan?: boolean;
}) {
  return (
    <div className={colSpan ? 'col-span-2' : undefined}>
      <div className="text-muted-foreground">{label}</div>
      <div
        className={cn(
          'text-foreground truncate',
          mono && 'font-mono',
        )}
        title={value}
      >
        {value}
      </div>
    </div>
  );
}

function _TestModelSection({
  providerOverride,
  modelOverride,
  configuredProvider,
  configuredModel,
  onProviderChange,
  onModelChange,
  canRunTest,
  isPending,
  result,
  error,
  onRun,
}: {
  providerOverride: string;
  modelOverride: string;
  configuredProvider: string;
  configuredModel: string;
  onProviderChange: (v: string) => void;
  onModelChange: (v: string) => void;
  canRunTest: boolean;
  isPending: boolean;
  result: TestModelResponse | undefined;
  error: Error | null;
  onRun: () => void;
}) {
  const missingProvider = !configuredProvider;

  return (
    <div className="rounded-sm border border-border/60 bg-card p-3 space-y-3">
      <div>
        <h4 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Model Probe
        </h4>
        <p className="text-[10px] text-muted-foreground mt-0.5">
          Runs a cheap extraction-shaped call to verify the model
          responds and returns parseable JSON. Does not create a
          job.
        </p>
      </div>

      {/* Provider + model inputs */}
      <div className="grid grid-cols-2 gap-2">
        <label className="space-y-0.5">
          <span className="text-[9px] uppercase tracking-wider text-muted-foreground">
            Provider
          </span>
          <input
            type="text"
            value={providerOverride}
            onChange={(e) => onProviderChange(e.target.value)}
            placeholder={configuredProvider || 'e.g. lmstudio'}
            className={cn(
              'w-full rounded-sm border border-border/60 bg-background',
              'px-2 py-1 text-[10px] font-mono text-foreground',
              'placeholder:text-muted-foreground/60',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
            )}
          />
        </label>
        <label className="space-y-0.5">
          <span className="text-[9px] uppercase tracking-wider text-muted-foreground">
            Model
          </span>
          <input
            type="text"
            value={modelOverride}
            onChange={(e) => onModelChange(e.target.value)}
            placeholder={configuredModel || 'e.g. gpt-4o-mini'}
            className={cn(
              'w-full rounded-sm border border-border/60 bg-background',
              'px-2 py-1 text-[10px] font-mono text-foreground',
              'placeholder:text-muted-foreground/60',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
            )}
          />
        </label>
      </div>

      {/* Provider-null hint */}
      {missingProvider && (
        <div className="flex items-start gap-1.5 text-[10px] text-muted-foreground">
          <AlertTriangle
            size={10}
            className="shrink-0 mt-0.5 text-amber-600 dark:text-amber-400"
            aria-hidden="true"
          />
          <span>
            Provider is not set in settings. The extraction worker
            infers it at dispatch time, but the test endpoint needs
            it explicitly -- enter it above to run the probe.
          </span>
        </div>
      )}

      {/* Run button */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onRun}
          disabled={!canRunTest || isPending}
          className={cn(
            'inline-flex items-center gap-1.5 rounded-sm border border-border/60',
            'bg-background px-2.5 py-1 text-[10px] font-medium',
            'hover:bg-muted/40 transition-colors',
            'disabled:opacity-50 disabled:cursor-not-allowed',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary',
          )}
        >
          {isPending ? (
            <Loader2
              size={10}
              className="animate-spin"
              aria-hidden="true"
            />
          ) : (
            <Zap size={10} aria-hidden="true" />
          )}
          {isPending ? 'Testing…' : 'Test model'}
        </button>
      </div>

      {/* Result display */}
      {error && (
        <div className="rounded-sm border border-red-500/20 bg-red-500/5 px-2 py-1.5 text-[10px] text-red-700 dark:text-red-300">
          <AlertCircle
            size={10}
            className="inline mr-1"
            aria-hidden="true"
          />
          HTTP error: {error.message}
        </div>
      )}

      {result && result.status === 'ok' && (
        <_TestSuccessResult data={result} />
      )}

      {result && result.status === 'error' && (
        <_TestErrorResult data={result} />
      )}
    </div>
  );
}

function _TestSuccessResult({
  data,
}: {
  data: Extract<TestModelResponse, { status: 'ok' }>;
}) {
  return (
    <div className="rounded-sm border border-emerald-500/20 bg-emerald-500/5 px-2 py-1.5 space-y-1">
      <div className="flex items-center gap-1 text-[10px] font-medium text-emerald-700 dark:text-emerald-300">
        <CheckCircle2 size={10} aria-hidden="true" />
        Model responded
      </div>
      <div className="grid grid-cols-3 gap-2 text-[10px]">
        <_MetricInline label="Latency" value={`${data.latency_ms}ms`} />
        <_MetricInline
          label="Tokens"
          value={`${data.input_tokens}→${data.output_tokens}`}
        />
        <_MetricInline
          label="Cost"
          value={`$${nanoUsdToUsd(data.cost_nano_usd).toFixed(4)}`}
        />
      </div>
      {data.raw_text_preview && (
        <div className="text-[9px] font-mono text-muted-foreground truncate">
          Response: {data.raw_text_preview.slice(0, 100)}
        </div>
      )}
    </div>
  );
}

function _TestErrorResult({
  data,
}: {
  data: Extract<TestModelResponse, { status: 'error' }>;
}) {
  return (
    <div className="rounded-sm border border-red-500/20 bg-red-500/5 px-2 py-1.5 space-y-1">
      <div className="flex items-center gap-1 text-[10px] font-medium text-red-700 dark:text-red-300">
        <AlertCircle size={10} aria-hidden="true" />
        {data.error === 'LLMParseError'
          ? 'Model responded but output was not parseable JSON'
          : 'Model call failed'}
      </div>
      <div className="text-[10px] text-muted-foreground break-words">
        {data.message}
      </div>
      {data.raw_text_preview && (
        <div className="text-[9px] font-mono text-muted-foreground whitespace-pre-wrap max-h-24 overflow-y-auto">
          Raw response: {data.raw_text_preview}
        </div>
      )}
    </div>
  );
}

function _MetricInline({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="font-mono text-[10px] text-foreground">
        {value}
      </div>
    </div>
  );
}

function _QualityStats({
  stats,
  isLoading,
}: {
  stats: {
    terminalJobCount: number;
    successfulJobCount: number;
    totalCandidates: number;
    totalCreated: number;
    rejectedAtWrite: number;
    conversionRate: number;
    totalCostNano: number;
  };
  isLoading: boolean;
}) {
  return (
    <div className="rounded-sm border border-border/60 bg-card p-3">
      <h4 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground mb-2">
        Quality (Last 20 Jobs)
      </h4>
      {isLoading ? (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-10 animate-pulse rounded-sm bg-muted/40"
            />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[10px]">
          <_StatCell
            icon={<Cpu size={10} aria-hidden="true" />}
            label="Jobs run"
            value={String(stats.terminalJobCount)}
          />
          <_StatCell
            icon={<Gauge size={10} aria-hidden="true" />}
            label="Conversion"
            value={
              stats.totalCandidates > 0
                ? `${Math.round(stats.conversionRate * 100)}%`
                : '—'
            }
          />
          <_StatCell
            icon={<Flame size={10} aria-hidden="true" />}
            label="Memories"
            value={`${stats.totalCreated} / ${stats.totalCandidates}`}
          />
          <_StatCell
            icon={<ArrowRight size={10} aria-hidden="true" />}
            label="Cost"
            value={`$${nanoUsdToUsd(stats.totalCostNano).toFixed(4)}`}
          />
        </div>
      )}
      <p className="text-[9px] text-muted-foreground mt-2 leading-snug">
        Conversion = memories created ÷ candidates that passed
        evidence validation. Candidates rejected here were deduped
        at write time, not filtered by the model.
      </p>
    </div>
  );
}

function _StatCell({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-sm border border-border/40 bg-muted/20 px-2 py-1.5">
      <div className="flex items-center gap-1 text-[9px] uppercase tracking-wider text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className="font-mono text-[11px] font-semibold text-foreground mt-0.5">
        {value}
      </div>
    </div>
  );
}

function _RecentFailures({
  jobs,
  isLoading,
  total,
}: {
  jobs: MemoryJob[];
  isLoading: boolean;
  total: number;
}) {
  return (
    <div className="rounded-sm border border-border/60 bg-card p-3">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Recent Failures
        </h4>
        {total > 0 && (
          <span className="text-[9px] text-muted-foreground">
            {total} total
          </span>
        )}
      </div>
      {isLoading ? (
        <div className="space-y-1">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="h-8 animate-pulse rounded-sm bg-muted/40"
            />
          ))}
        </div>
      ) : jobs.length === 0 ? (
        <div className="flex items-center gap-1.5 text-[10px] text-emerald-700 dark:text-emerald-300">
          <CheckCircle2 size={10} aria-hidden="true" />
          No recent failures.
        </div>
      ) : (
        <ul className="space-y-1">
          {jobs.map((job) => (
            <li
              key={job.job_id}
              className="rounded-sm border border-red-500/10 bg-red-500/5 px-2 py-1.5 space-y-0.5"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-[9px] text-muted-foreground truncate">
                  {job.job_id}
                </span>
                <span className="text-[9px] text-red-700 dark:text-red-300 shrink-0">
                  {job.error_code ?? 'unknown'}
                </span>
              </div>
              {job.error_message && (
                <div className="text-[9px] text-muted-foreground truncate">
                  {job.error_message}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function _QuickLink({
  href,
  label,
  description,
}: {
  href: string;
  label: string;
  description: string;
}) {
  return (
    <Link
      href={href}
      className={cn(
        'group flex items-center justify-between gap-2 rounded-sm border border-border/60 bg-card px-3 py-2',
        'hover:border-primary/40 hover:bg-muted/40 transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
      )}
    >
      <div className="min-w-0">
        <div className="text-[11px] font-medium text-foreground">
          {label}
        </div>
        <div className="text-[9px] text-muted-foreground truncate">
          {description}
        </div>
      </div>
      <ArrowRight
        size={11}
        aria-hidden="true"
        className="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
      />
    </Link>
  );
}
