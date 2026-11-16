// src/features/memory/components/EmbeddingsSection.tsx

/**
 * Phase 4 composition section: "Semantic search" (Section A
 * through D).
 *
 * Owns the ENTIRE Phase 4 write lifecycle
 * ---------------------------------------
 * Unlike the Phase 3 `MemoryAISettingsSection` -- which
 * participates in the parent `SettingsForm`'s dirty-state and
 * save button -- this section AUTO-SAVES on every change:
 *
 *   * Toggle (`embedding.enabled`), model selection, and
 *     retrieval mode: PATCH fires IMMEDIATELY (single change,
 *     already-deliberate user action).
 *
 *   * Advanced tuning (5 sliders + rrf_k): PATCH is
 *     DEBOUNCED by 500ms (see `handleRetrievalChange`). The
 *     user can drag a slider across the full range without
 *     triggering a request per pixel.
 *
 * Rationale (plan §13): "auto-save (debounced) for
 * toggles/dropdowns + save button للـ advanced sliders".
 * Given that the parent form does not require Phase 4 to be
 * part of its save button (the sections do not share a
 * "commit" moment), keeping the whole section self-saving
 * is simpler than threading the pending state through the
 * parent -- and it matches the Phase 4 acceptance criteria
 * (each change is independent).
 *
 * State management
 * ----------------
 *   * `embedding` and `retrieval.mode` are read DIRECTLY from
 *     props (parent refetches on PATCH success, so the UI
 *     reflects the server state).
 *
 *   * `retrieval` advanced fields are kept in LOCAL state
 *     (`localRetrieval`) because a debounced save needs to
 *     show the pending slider value immediately, before the
 *     network round-trip completes. When the parent's
 *     `retrieval` prop changes (either because our mutation
 *     succeeded or because a concurrent mutation elsewhere
 *     refetched settings), we sync local state to the fresh
 *     value -- but ONLY when we are not actively saving
 *     (otherwise a mid-flight refetch would revert the
 *     user's in-progress slider drag).
 *
 * Stale-model detection
 * ---------------------
 * `embedding.model_key` is stored independently of the
 * `GET /api/config/embedding-models` catalog. If the user
 * (or a provider rotation) removes that model from the
 * catalog, `model_key` becomes stale. We detect this by
 * checking the current `useEmbeddingModels()` catalog for a
 * matching `key` and surface a warning banner when the
 * match is missing (`model_key_not_in_snapshot` guard).
 */

'use client';

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import { Brain } from 'lucide-react';

import type {
  MemoryEmbeddingSettings,
  MemoryRetrievalSettings,
  RetrievalMode,
} from '../types/embedding';
import {
  DEFAULT_MEMORY_EMBEDDING_SETTINGS,
  DEFAULT_MEMORY_RETRIEVAL_SETTINGS,
} from '../types/embedding';
import { useEmbeddingModels } from '../api/queries';
import { usePatchSettings } from '../api/mutations';
import { MemorySection } from './MemorySection';
import { MemoryBanner } from './MemoryBanner';
import { EmbeddingModelSelector } from './EmbeddingModelSelector';
import { RetrievalModeSelector } from './RetrievalModeSelector';
import {
  AdvancedTuningPanel,
  type AdvancedTuningSaveStatus,
} from './AdvancedTuningPanel';
import { ReindexPanel } from './ReindexPanel';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Debounce window for advanced-tuning changes. Per plan §5.4.
 */
const DEBOUNCE_MS = 500;

/**
 * How long the "Saved" indicator stays visible before
 * returning to idle. Long enough for the user to notice,
 * short enough to feel responsive.
 */
const SAVED_DISPLAY_MS = 2000;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface EmbeddingsSectionProps {
  /** Current `MemorySettings.embedding` sub-tree. */
  embedding: MemoryEmbeddingSettings;

  /** Current `MemorySettings.retrieval` sub-tree. */
  retrieval: MemoryRetrievalSettings;

  /**
   * Section-level disabled flag from the parent. When true
   * (e.g. because the overall memory mode forbids writes),
   * every input in the section is disabled.
   */
  disabled?: boolean;

  /**
   * Field errors from the parent's Zod validation. Keys use
   * dot-joined paths (`"embedding.model_key"`,
   * `"retrieval.mode"`). Rare for Phase 4 -- numeric bounds
   * are enforced by `<input min max>`, and enums by the
   * radio group -- but plumbed through for future extension.
   */
  errors?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function EmbeddingsSection({
  embedding: embeddingRaw,
  retrieval: retrievalRaw,
  disabled: disabledProp = false,
  errors,
}: EmbeddingsSectionProps) {
  // Defensive defaulting -- the parent (`/memory/settings/embeddings`
  // page) is expected to pass `settings.embedding` and
  // `settings.retrieval`, which are Phase 4 fields that the
  // backend MUST return per its contract. HOWEVER, an OLDER
  // backend deployment, or a stale TanStack Query cache, may
  // return settings WITHOUT these fields. The client TYPE
  // says they exist, but the wire payload does not -- so
  // we cannot rely on TypeScript to catch this at compile
  // time. Without this guard, the entire page crashes on
  // `embedding.model_key` (the bug this code fixes).
  //
  // The spread-with-default pattern (rather than a bare `??`)
  // handles THREE failure modes:
  //   1. `embeddingRaw === undefined`  (whole object missing)
  //   2. `embeddingRaw === null`       (explicitly nulled)
  //   3. `embeddingRaw === {}`         (object present but
  //                                      field-by-field empty)
  // Case 3 is why we spread BOTH sides: `{...DEFAULT, ...{}}`
  // still yields the full default shape.
  const embedding: MemoryEmbeddingSettings = {
    ...DEFAULT_MEMORY_EMBEDDING_SETTINGS,
    ...(embeddingRaw ?? {}),
  };
  const retrieval: MemoryRetrievalSettings = {
    ...DEFAULT_MEMORY_RETRIEVAL_SETTINGS,
    ...(retrievalRaw ?? {}),
  };

  // `phase4Available` is `true` ONLY when the backend actually
  // sent both fields. It gates the warning banner below and
  // forces the whole section to read-only when false -- because
  // saving would fail (backend cannot accept fields it does
  // not know about).
  const phase4Available =
    embeddingRaw !== undefined &&
    embeddingRaw !== null &&
    retrievalRaw !== undefined &&
    retrievalRaw !== null;

  // The section is disabled either because the parent disabled
  // it (mode gate) OR because Phase 4 is not supported. Both
  // reasons produce the same read-only UX.
  const disabled = disabledProp || !phase4Available;

  const { t } = useTranslation('memory');

  // Scroll target for "go to Section A" actions on warnings.
  const sectionARef = useRef<HTMLDivElement>(null);

  // ---- Local UI state -----------------------------------------

  // Inline error banner (from a failed PATCH). Persists until
  // the next successful change or explicit dismissal.
  const [actionError, setActionError] = useState<string | null>(null);

  // Local retrieval state (debounced save).
  const [localRetrieval, setLocalRetrieval] =
    useState<MemoryRetrievalSettings>(retrieval);
  const [saveStatus, setSaveStatus] =
    useState<AdvancedTuningSaveStatus>('idle');

  // Timer refs -- cleared on unmount and on each new edit.
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // ---- Fetch catalog to detect stale model_key ----------------
  //
  // Disabled when the whole section is disabled -- there is no
  // point fetching the catalog when the user cannot act on it.
  const modelsQuery = useEmbeddingModels({ enabled: !disabled });

  const modelStillValid = useMemo(() => {
    if (embedding.model_key === null) return true;
    // Not loaded yet or failed -> treat as valid. Better to
    // not warn than to warn incorrectly during a transient
    // catalog fetch failure.
    if (!modelsQuery.data) return true;
    return modelsQuery.data.some((m) => m.key === embedding.model_key);
  }, [embedding.model_key, modelsQuery.data]);

  // ---- Sync localRetrieval from parent prop -------------------
  //
  // When the parent's `retrieval` prop changes (from a refetch
  // after ANY settings mutation), pull the fresh values into
  // local state -- BUT only when we are NOT actively saving.
  // Otherwise a mid-flight refetch would revert a slider the
  // user is still dragging.
  useEffect(() => {
    if (saveStatus === 'idle' || saveStatus === 'saved') {
      setLocalRetrieval((prev) =>
        JSON.stringify(prev) === JSON.stringify(retrieval)
          ? prev
          : retrieval,
      );
    }
    // We intentionally omit `saveStatus` from deps -- including
    // it would re-sync every time the status changes, which is
    // exactly what we want to AVOID during 'saving'. The status
    // is read from the closure above for the current render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [retrieval]);

  // ---- Cleanup timers on unmount ------------------------------
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
    };
  }, []);

  // ---- Mutations ----------------------------------------------
  const patchMutation = usePatchSettings();

  // ---- Immediate handlers (toggle, model, mode) ---------------

  // ------------------------------------------------------------------
  // Unified PATCH error formatter.
  //
  // The Phase 4 settings can fail to save for two reasons:
  //
  //   1. Transient (network, 5xx) -- the backend's message is
  //      usually informative; use it verbatim.
  //
  //   2. The backend is older than the frontend and does NOT
  //      accept `embedding` / `retrieval` in
  //      `PATCH /api/memory/settings`. The backend returns
  //      400 (often `request_validation`) with a generic
  //      message like "Request validation failed." that
  //      tells the user nothing actionable.
  //
  // Case 2 is expected during rolling deployments. Rather than
  // showing the raw 400 message (which looks like a bug),
  // we detect it and surface a targeted hint. The banner can
  // then say "backend does not support Phase 4 yet" so the
  // user knows to update the server instead of retrying.
  const formatPatchError = useCallback(
    (err: unknown): string => {
      if (
        err instanceof Error &&
        'statusCode' in err &&
        'errorType' in err
      ) {
        const apiErr = err as {
          statusCode: number;
          errorType: string;
          message: string;
        };
        const isValidationFailure =
          apiErr.statusCode === 400 ||
          apiErr.statusCode === 422 ||
          apiErr.errorType === 'request_validation' ||
          apiErr.errorType === 'invalid_settings';

        if (isValidationFailure) {
          return t('settings.embedding.unavailableMessage');
        }
        return apiErr.message;
      }
      return t('errors.generic');
    },
    [t],
  );

  const handleEmbeddingToggle = useCallback(
    (enabled: boolean) => {
      setActionError(null);
      patchMutation.mutate(
        { embedding: { enabled } },
        {
          onError: (err) => setActionError(formatPatchError(err)),
        },
      );
    },
    [patchMutation, formatPatchError],
  );

  const handleModelChange = useCallback(
    (modelKey: string | null) => {
      setActionError(null);
      patchMutation.mutate(
        { embedding: { model_key: modelKey } },
        {
          onError: (err) => setActionError(formatPatchError(err)),
        },
      );
    },
    [patchMutation, formatPatchError],
  );

  const handleModeChange = useCallback(
    (mode: RetrievalMode) => {
      setActionError(null);
      patchMutation.mutate(
        { retrieval: { mode } },
        {
          onError: (err) => setActionError(formatPatchError(err)),
        },
      );
    },
    [patchMutation, formatPatchError],
  );

  // ---- Debounced retrieval-advanced handler -------------------

  const handleRetrievalChange = useCallback(
    (next: MemoryRetrievalSettings) => {
      // Immediate local state update so the slider reflects
      // the user's drag without waiting for the network.
      setLocalRetrieval(next);
      setSaveStatus('saving');
      setActionError(null);

      // Reset both timers (the pending save and any lingering
      // "saved" indicator).
      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);

      debounceRef.current = setTimeout(() => {
        patchMutation.mutate(
          { retrieval: next },
          {
            onSuccess: () => {
              setSaveStatus('saved');
              savedTimerRef.current = setTimeout(
                () => setSaveStatus('idle'),
                SAVED_DISPLAY_MS,
              );
            },
            onError: (err) => {
              setSaveStatus('error');
              setActionError(formatPatchError(err));
            },
          },
        );
      }, DEBOUNCE_MS);
    },
    [patchMutation],
  );

  // ---- Scroll-to-Section-A ------------------------------------
  const scrollToSectionA = useCallback(() => {
    sectionARef.current?.scrollIntoView({
      behavior: 'smooth',
      block: 'center',
    });
  }, []);

  // ---- Warning flags ------------------------------------------
  const showNoModelWarning =
    embedding.enabled && embedding.model_key === null;

  const showStaleModelWarning =
    embedding.enabled &&
    embedding.model_key !== null &&
    !modelStillValid;

  // When ANY PATCH is in flight we disable interactive
  // controls -- prevents rapid-succession mutations and the
  // resulting confusing banner flicker.
  const interactiveDisabled = disabled || patchMutation.isPending;

  // ---- Render -------------------------------------------------
  return (
    <MemorySection
      icon={Brain}
      title={t('settings.sections.semanticSearch')}
      subtitle={t('settings.embedding.sectionSubtitle')}
      accent="cyan"
      delay={0.3}
    >
      <div className="space-y-4">
        {/* ============ Phase 4 unavailable ============ */}
        {/*
          The backend did not include `embedding` / `retrieval`
          in `GET /api/memory/settings`. Either this is an
          older deployment than the frontend expects, or the
          cached settings are stale (TanStack Query cached a
          response from before Phase 4 landed server-side).
          Every interactive control in the section is
          DISABLED (see `disabled` above) because saving
          would fail -- the backend would reject an unknown
          PATCH body. The user can hard-refresh if the cache
          is the problem; otherwise the backend needs an
          update.
        */}
        {!phase4Available && (
          <MemoryBanner
            tone="warning"
            title={t('settings.embedding.unavailableTitle')}
            message={t('settings.embedding.unavailableMessage')}
          />
        )}

        {/* ============ Action error banner ============ */}
        {actionError && (
          <MemoryBanner
            tone="error"
            message={actionError}
            action={
              <button
                type="button"
                onClick={() => setActionError(null)}
                className="text-[11px] font-medium underline underline-offset-2 whitespace-nowrap"
              >
                {t('common.close')}
              </button>
            }
          />
        )}

        {/* ============ Section A: Embedding model ============ */}
        <div ref={sectionARef} className="space-y-3">
          <label
            className={cn(
              'inline-flex items-center gap-2 rounded-sm border border-border/60 bg-card px-3 py-2 text-xs cursor-pointer',
              'hover:bg-muted/40 transition-colors',
              interactiveDisabled && 'cursor-not-allowed opacity-60',
            )}
          >
            <input
              type="checkbox"
              checked={embedding.enabled}
              onChange={(e) => handleEmbeddingToggle(e.target.checked)}
              disabled={interactiveDisabled}
              className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0 cursor-pointer"
            />
            <span className="font-medium text-foreground">
              {t('settings.embedding.enabled')}
            </span>
          </label>

          {!embedding.enabled && (
            <MemoryBanner
              tone="info"
              message={t('settings.embedding.disabledHint')}
            />
          )}

          {showNoModelWarning && (
            <MemoryBanner
              tone="warning"
              message={t('settings.embedding.modelNotSelected')}
            />
          )}

          {showStaleModelWarning && (
            <MemoryBanner
              tone="warning"
              message={t('settings.embedding.modelMissingFromSnapshot')}
            />
          )}

          <EmbeddingModelSelector
            value={embedding.model_key}
            onChange={handleModelChange}
            disabled={
              interactiveDisabled || !embedding.enabled
            }
            error={errors?.['embedding.model_key']}
          />
        </div>

        {/* ============ Section B: Retrieval mode ============ */}
        <RetrievalModeSelector
          value={retrieval.mode}
          onChange={handleModeChange}
          embedding={embedding}
          disabled={interactiveDisabled}
          error={errors?.['retrieval.mode']}
          onRequestEnable={scrollToSectionA}
        />

        {/* ============ Section C: Advanced tuning ============ */}
        <AdvancedTuningPanel
          value={localRetrieval}
          onChange={handleRetrievalChange}
          saveStatus={saveStatus}
          disabled={disabled}
        />

        {/* ============ Section D: Reindex ============ */}
        <ReindexPanel
          embedding={embedding}
          disabled={disabled}
          onRequestEnable={scrollToSectionA}
        />
      </div>
    </MemorySection>
  );
}
