// src/features/memory/hooks/useAIEnabled.ts

/**
 * Derive the AI-extraction availability gates from the current
 * AI settings snapshot.
 *
 * Sibling of `useMemoryMode` (which gates on the GLOBAL memory
 * `mode`). The two hooks are orthogonal:
 *
 *   * `useMemoryMode` answers "can this deployment accept ANY
 *     memory writes?" -- the answer depends on `mode` only.
 *
 *   * `useAIEnabled` answers "is the AI EXTRACTION subsystem
 *     turned on and configured?" -- the answer depends on
 *     `ai.enabled` AND `ai.extraction_enabled` AND the presence
 *     of a model selection.
 *
 * A Phase-3 UI gate on the automation panel composes BOTH:
 *
 *     const mode = useMemoryMode();
 *     const ai = useAIEnabled();
 *     const canRunBatch =
 *       mode.canWrite && ai.isAvailable;
 *
 * Why two booleans (`isEnabled` vs `isAvailable`)
 * ----------------------------------------------
 *
 * `MemoryAISettings` has two top-level flags -- `enabled` and
 * `extraction_enabled` -- plus a nested model selection
 * (`extraction.provider_id` / `extraction.model_id`). The
 * backend requires ALL THREE to be true before it will create
 * an extraction job:
 *
 *   * `enabled`:            "I want AI extraction as a
 *                            feature"
 *   * `extraction_enabled`: "I want the extraction task
 *                            specifically" (vs. only the future
 *                            `consolidation_enabled`)
 *   * `extraction.model_id`: "I have picked a model"
 *
 * `isEnabled` maps to `enabled && extraction_enabled`.
 * `isAvailable` additionally requires a non-empty `model_id`
 * in `ai.extraction`.
 *
 * The distinction matters for the UI: an operator who has
 * enabled extraction but not picked a model needs an actionable
 * prompt ("pick a model first"), while an operator who has
 * turned the feature off needs no prompt at all (the panel is
 * simply hidden).
 *
 * Fails closed
 * ------------
 *
 * Before the AI-settings query resolves -- and on any error --
 * `isEnabled` and `isAvailable` are `false`. Same pattern as
 * `useMemoryMode`: no accidental exposure of automation UI
 * during the initial paint, no flash of "you can run batches"
 * that then disappears.
 */

'use client';

import {
  useAISettings,
  useMemorySettings,
} from '../api/queries';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AIEnabledGates {
  /**
   * `true` when the AI subsystem's two top-level toggles are
   * both on (`ai.enabled && ai.extraction_enabled`).
   *
   * Independent of whether a model is configured -- see
   * `isAvailable` for the stronger gate.
   */
  isEnabled: boolean;

  /**
   * `true` when extraction is enabled AND a model has been
   * picked. This is the gate that determines whether a batch
   * or manual extraction call will actually succeed; the
   * backend will reject `POST /automation/batch` with 503
   * when the model is missing.
   */
  isAvailable: boolean;

  /**
   * `true` while the AI-settings query has not resolved yet.
   * Components that would gate on `isAvailable` should treat
   * this as "do not show the automation UI yet" rather than
   * assuming the subsystem is off -- see the module docstring
   * on "fails closed".
   */
  isLoading: boolean;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useAIEnabled(): AIEnabledGates {
  // Primary source: dedicated Phase 3 endpoint (lighter payload).
  const phase3Query = useAISettings();

  // Fallback: Phase 1 endpoint (`/api/memory/settings`) -- the SAME
  // source the Settings page uses. If Phase 3 fails silently (a
  // transient cache miss, an unregistered route in an older build,
  // or a network blip), we still get the correct answer from the
  // endpoint the user's Save button actually writes to.
  //
  // Both endpoints expose `MemorySettings.ai` at the same logical
  // position; `useMemorySettings` returns the full `MemorySettings`
  // object, so `data.ai` is the AI subtree directly.
  const phase1Query = useMemorySettings();

  const ai = phase3Query.data?.ai ?? phase1Query.data?.ai;

  const enabled = Boolean(ai?.enabled);
  const extractionEnabled = Boolean(ai?.extraction_enabled);

  // Only ``model_id`` is required. ``provider_id`` may be null
  // when the operator picked a model from a dropdown whose
  // entries carry no explicit provider association (LM Studio
  // and Ollama models are the common case).
  //
  // The backend's extraction worker infers the provider from
  // the model vault at dispatch time (via
  // ``MemoryService._resolve_model_provider``). The 4
  // completed jobs on /memory/jobs are direct evidence that
  // this inference works: those jobs were enqueued by
  // ``AFTER_TURN`` with ``provider_id=null`` and
  // ``model_id="nvidia-nemotron-3-nano-4b"``, and the worker
  // resolved the provider successfully.
  //
  // Treating ``provider_id=null`` as "not configured" here
  // would gate the Batch UI and the Extract button OFF while
  // the backend is perfectly capable of running the job --
  // a false negative that blocks a working feature.
  const hasModel = Boolean(ai?.extraction?.model_id);

  return {
    isEnabled: enabled && extractionEnabled,
    isAvailable: enabled && extractionEnabled && hasModel,
    // Only report "loading" while BOTH sources are still pending;
    // if either has resolved (even to null), we've made a decision.
    isLoading: phase3Query.isLoading && phase1Query.isLoading,
  };
}
