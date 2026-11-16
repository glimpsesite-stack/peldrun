// src/features/memory/components/AdvancedTuningPanel.tsx

/**
 * Collapsible panel exposing the four retrieval-fusion tuning
 * knobs (`vector_alpha`, `min_lexical_score`,
 * `min_vector_similarity`, `rrf_k`) plus the disabled
 * `rerank_enabled` placeholder.
 *
 * Design contract
 * ---------------
 *
 *   * **Collapsible, closed by default.** Defaults are correct
 *     for ~90% of users; exposing the knobs unconditionally
 *     would be visual noise. Uses native `<details>` /
 *     `<summary>` for accessibility (keyboard toggle, screen-
 *     reader "expanded/collapsed" state) with no JS state.
 *
 *   * **Presentational.** The panel does NOT own the debounce
 *     timer or the mutation. The parent (`EmbeddingsSection`)
 *     owns both and passes:
 *
 *       - `value`      -- current `retrieval` sub-tree
 *       - `onChange`   -- called IMMEDIATELY on every edit
 *                         (parent debounces 500ms before
 *                         firing the mutation)
 *       - `saveStatus` -- 'idle' | 'saving' | 'saved' | 'error'
 *                         from the parent's mutation state
 *
 *     This separation keeps the save lifecycle in ONE place
 *     (the parent) and lets sibling panels share the same
 *     mutation without coordinating debounce timers.
 *
 *   * **Save-status indicator lives in the `<summary>`.**
 *     Always visible (even when the panel is collapsed), so a
 *     background save does not need the user to have the panel
 *     open to notice it succeeded or failed.
 *
 *   * **`rrf_k` is a number input, not a slider.** Per the
 *     Phase 4 plan (§5.4), it is an integer in `[1, 100]` --
 *     a slider would force users to drag for precise entry on
 *     a narrow range. Every other field is a 0.0..1.0 slider.
 *
 *   * **`rerank_enabled` is always disabled.** It is a Phase 5
 *     stub -- the backend ignores the field today. Rendering
 *     it as an interactive toggle would create a misleading
 *     "it's on but nothing happened" experience. A small
 *     "Coming in Phase 5" badge + tooltip explains the state.
 *
 * The parent's `saveStatus` prop is the ONLY mechanism the
 * panel uses to display save state. The panel does NOT show
 * toasts -- those belong to the parent (which decides which
 * changes deserve a toast vs. a silent indicator).
 */

'use client';

import { useTranslation } from 'react-i18next';
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  Loader2,
} from 'lucide-react';

import type { MemoryRetrievalSettings } from '../types/embedding';
import { MemoryRetrievalBounds } from '../types/embedding';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Save status
// ---------------------------------------------------------------------------

/**
 * The four states of the save cycle, as observed from the
 * parent's perspective.
 *
 * Transition rules (owned by the parent):
 *
 *   * `idle`   -- no unsaved changes; last save succeeded (or
 *                 no save has run yet).
 *   * `saving` -- debounce timer active OR mutation in flight.
 *                 The parent enters this state on the FIRST
 *                 onChange and stays in it until the mutation
 *                 settles.
 *   * `saved`  -- mutation returned success. The parent holds
 *                 this state for ~2 seconds, then returns to
 *                 `idle` (implemented via setTimeout, not
 *                 shown here).
 *   * `error`  -- mutation threw. The parent stays in this
 *                 state until the next edit (which restarts
 *                 the cycle) or a manual retry.
 */
export type AdvancedTuningSaveStatus =
  | 'idle'
  | 'saving'
  | 'saved'
  | 'error';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface AdvancedTuningPanelProps {
  /** Current retrieval sub-tree (from parent's local state). */
  value: MemoryRetrievalSettings;

  /**
   * Called IMMEDIATELY on every slider change, number-input
   * change, or (disabled, but still fireable) toggle change.
   *
   * The parent is responsible for debouncing this before
   * firing the mutation -- the panel does NOT debounce.
   * Debouncing at the parent means a single mutation call
   * covers a user who drags the slider across the full range
   * in one gesture.
   */
  onChange: (next: MemoryRetrievalSettings) => void;

  /**
   * Save-cycle status from the parent's mutation state.
   * Rendered in the `<summary>` row. `'idle'` renders nothing
   * (so the panel header stays minimal when there is no
   * pending work).
   */
  saveStatus?: AdvancedTuningSaveStatus;

  /** When true, disables every input inside the panel. */
  disabled?: boolean;
}

// ---------------------------------------------------------------------------
// Save-status indicator
// ---------------------------------------------------------------------------

/**
 * The small "Saving..." / "Saved" / "Failed" indicator that
 * sits in the panel summary.
 *
 * Uses lucide icons for visual consistency with the rest of
 * the memory feature:
 *   * `Loader2` (spin) -> saving
 *   * `CheckCircle2`   -> saved
 *   * `AlertCircle`    -> error
 *   * `null`           -> idle (nothing rendered)
 *
 * Renders nothing for `'idle'` -- a permanent "Idle" label
 * would add visual noise to every state where nothing is
 * happening. The absence of an indicator IS the idle state.
 */
function SaveStatusIndicator({
  status,
}: {
  status: AdvancedTuningSaveStatus;
}) {
  const { t } = useTranslation('memory');

  if (status === 'idle') return null;

  if (status === 'saving') {
    return (
      <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
        <Loader2
          size={10}
          className="animate-spin"
          aria-hidden="true"
        />
        <span>{t('settings.retrieval.advanced.saving')}</span>
      </span>
    );
  }

  if (status === 'saved') {
    return (
      <span
        className="flex items-center gap-1 text-[10px] text-emerald-600 dark:text-emerald-400"
        role="status"
      >
        <CheckCircle2 size={10} aria-hidden="true" />
        <span>{t('settings.retrieval.advanced.saved')}</span>
      </span>
    );
  }

  return (
    <span
      className="flex items-center gap-1 text-[10px] text-rose-600 dark:text-rose-400"
      role="alert"
    >
      <AlertCircle size={10} aria-hidden="true" />
      <span>{t('settings.retrieval.advanced.saveFailed')}</span>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Slider field
// ---------------------------------------------------------------------------

/**
 * One labeled slider with a numeric value display on the right
 * and a help text below.
 *
 * Deliberately lighter than `MemoryField` -- the panel already
 * lives inside a card, and wrapping each slider in another
 * card would create a three-level card-in-card-in-card nest.
 * The visual treatment here is a compact inline row that
 * matches the "no borders between rows" pattern of the panel.
 */
interface SliderFieldProps {
  label: string;
  help: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  formatValue?: (value: number) => string;
}

function SliderField({
  label,
  help,
  value,
  min,
  max,
  step,
  onChange,
  disabled = false,
  formatValue = (v) => String(v),
}: SliderFieldProps) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <label className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          {label}
        </label>
        <span className="text-[11px] font-mono text-foreground shrink-0 tabular-nums">
          {formatValue(value)}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        disabled={disabled}
        aria-label={label}
        title={help}
        className={cn(
          'w-full accent-primary cursor-pointer',
          disabled && 'cursor-not-allowed opacity-60',
        )}
      />
      <p className="text-[10px] leading-snug text-muted-foreground">
        {help}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function AdvancedTuningPanel({
  value,
  onChange,
  saveStatus = 'idle',
  disabled = false,
}: AdvancedTuningPanelProps) {
  const { t } = useTranslation('memory');

  /**
   * Update a single field of `MemoryRetrievalSettings` while
   * preserving every other field. Generic over the field key
   * so the call sites stay type-safe (`update('rrf_k', 42)`
   * type-checks; `update('foo', ...)` does not).
   */
  const update = <K extends keyof MemoryRetrievalSettings>(
    key: K,
    next: MemoryRetrievalSettings[K],
  ) => {
    onChange({ ...value, [key]: next });
  };

  return (
    <details
      className={cn(
        'group rounded-sm border border-border/60 bg-card',
        'transition-colors hover:border-muted/30',
      )}
    >
      {/* ---- Summary (always visible, even when collapsed) ---- */}
      <summary
        className={cn(
          'flex items-center justify-between gap-2 cursor-pointer p-3',
          // `list-none` + the webkit variant remove the native
          // disclosure triangle -- we render our own ChevronRight.
          'list-none select-none [&::-webkit-details-marker]:hidden',
        )}
      >
        <div className="flex items-center gap-1.5">
          <ChevronRight
            size={12}
            aria-hidden="true"
            className="text-muted-foreground transition-transform group-open:rotate-90"
          />
          <span className="text-[11px] font-semibold uppercase tracking-wider text-foreground">
            {t('settings.retrieval.advanced.title')}
          </span>
        </div>
        <SaveStatusIndicator status={saveStatus} />
      </summary>

      {/* ---- Content (visible only when expanded) ---- */}
      <div className="border-t border-border/40 px-3 pb-3 pt-3 space-y-3">
        {/* --- vector_alpha --- */}
        <SliderField
          label={t('settings.retrieval.advanced.vectorAlpha')}
          help={t('settings.retrieval.advanced.vectorAlphaHelp')}
          value={value.vector_alpha}
          min={MemoryRetrievalBounds.vector_alpha.min}
          max={MemoryRetrievalBounds.vector_alpha.max}
          step={0.05}
          onChange={(v) => update('vector_alpha', v)}
          disabled={disabled}
          formatValue={(v) => v.toFixed(2)}
        />

        {/* --- min_lexical_score --- */}
        <SliderField
          label={t('settings.retrieval.advanced.minLexicalScore')}
          help={t('settings.retrieval.advanced.minLexicalScoreHelp')}
          value={value.min_lexical_score}
          min={MemoryRetrievalBounds.min_lexical_score.min}
          max={MemoryRetrievalBounds.min_lexical_score.max}
          step={0.05}
          onChange={(v) => update('min_lexical_score', v)}
          disabled={disabled}
          formatValue={(v) => v.toFixed(2)}
        />

        {/* --- min_vector_similarity --- */}
        <SliderField
          label={t('settings.retrieval.advanced.minVectorSimilarity')}
          help={t('settings.retrieval.advanced.minVectorSimilarityHelp')}
          value={value.min_vector_similarity}
          min={MemoryRetrievalBounds.min_vector_similarity.min}
          max={MemoryRetrievalBounds.min_vector_similarity.max}
          step={0.05}
          onChange={(v) => update('min_vector_similarity', v)}
          disabled={disabled}
          formatValue={(v) => v.toFixed(2)}
        />

        {/* --- rrf_k (number input, per plan §5.4) --- */}
        <div className="space-y-1">
          <div className="flex items-center justify-between gap-2">
            <label
              htmlFor="advanced-tuning-rrf-k"
              className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
            >
              {t('settings.retrieval.advanced.rrfK')}
            </label>
            <input
              id="advanced-tuning-rrf-k"
              type="number"
              min={MemoryRetrievalBounds.rrf_k.min}
              max={MemoryRetrievalBounds.rrf_k.max}
              step={1}
              value={value.rrf_k}
              onChange={(e) => {
                const n = Number(e.target.value);
                // Guard: `Number('')` is `0`, `Number('abc')` is
                // `NaN`. We only commit finite numbers; an empty
                // or malformed input reverts visually on blur via
                // the controlled `value` prop.
                if (Number.isFinite(n)) update('rrf_k', n);
              }}
              disabled={disabled}
              className={cn(
                'w-20 rounded-sm border border-border/60 bg-transparent',
                'px-2 py-1 text-[11px] font-mono text-foreground text-right tabular-nums',
                'focus:outline-none focus:ring-1 focus:ring-primary focus:border-primary',
                'disabled:cursor-not-allowed disabled:opacity-60',
              )}
            />
          </div>
          <p className="text-[10px] leading-snug text-muted-foreground">
            {t('settings.retrieval.advanced.rrfKHelp')}
          </p>
        </div>

        {/* --- rerank_enabled (Phase 5 stub, always disabled) --- */}
        <div className="space-y-1 pt-2 border-t border-border/40">
          <label
            className="inline-flex items-center gap-2 text-[11px] cursor-not-allowed opacity-60"
            title={t('settings.retrieval.advanced.rerankComingSoon')}
          >
            <input
              type="checkbox"
              checked={value.rerank_enabled}
              disabled
              className="h-3.5 w-3.5 rounded border-border cursor-not-allowed"
            />
            <span className="font-medium text-foreground">
              {t('settings.retrieval.advanced.rerank')}
            </span>
            <span className="ml-1 rounded-sm bg-muted/40 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
              {t('settings.retrieval.advanced.rerankComingSoon')}
            </span>
          </label>
          <p className="text-[10px] leading-snug text-muted-foreground">
            {t('settings.retrieval.advanced.rerankHelp')}
          </p>
        </div>
      </div>
    </details>
  );
}
