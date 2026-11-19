// src/features/memory/components/AgenticSettingsForm.tsx

/**
 * Phase 6 settings: agentic retrieval + knowledge graph.
 *
 * Covers the nine Phase 6 fields on `MemoryRetrievalSettings` AND
 * the four Phase 6 fields on `MemoryAISettings` that the design
 * brief omitted. Both halves are required for the feature to do
 * anything:
 *
 *   * `retrieval.agentic_enabled` + `retrieval.graph_search_enabled`
 *     control READING (search).
 *   * `ai.graph_indexing_enabled` controls POPULATING the graph.
 *   * `ai.agentic.*` is the model that both the indexer (entity
 *     extraction) and the agentic pipeline (rewrite + synthesis)
 *     call.
 *
 * Without the second half the user flips a switch and nothing
 * happens: search finds an empty graph, and agentic returns
 * `agentic+fallback` forever. That failure is silent on the backend
 * (it degrades instead of erroring), so the settings UI is the only
 * place it can be explained.
 *
 * Save model
 * ----------
 * Draft -> per-field diff -> explicit Save, exactly like
 * `MemoryRetrievalSettingsForm`. NOT the "PATCH on every toggle
 * change / debounce 500ms" model the design brief proposed: a second
 * mutation model on the same page would fight the re-hydration
 * effect (each PATCH returns new settings, which resets the draft
 * and discards the user's unsaved numeric edits).
 *
 * Patch shape
 * -----------
 * Only CHANGED leaf fields are sent:
 *
 *     { retrieval: { agentic_max_hops: 4 },
 *       ai: <FULL MemoryAISettings> }
 *
 * `ai` is sent whole -- the backend's settings manager applies the
 * patch with a shallow `dict.update`, so a partial `ai` object would
 * reset every omitted field to its Pydantic default (see
 * `MemorySettingsPatch.ai` in `types/settings.ts`). `mergeMemoryAISettings`
 * expands the delta before transmission.
 */

'use client';

import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  BrainCircuit,
  Loader2,
  Network,
  RotateCcw,
  Save,
  X as XIcon,
} from 'lucide-react';

import type { MemoryAISettings, MemoryTaskModel } from '../types/ai';
import type { MemoryRetrievalSettings } from '../types/embedding';
import { DEFAULT_MEMORY_RETRIEVAL_SETTINGS } from '../types/embedding';
import { DEFAULT_MEMORY_AI_SETTINGS } from '../types/ai';
import {
  mergeMemoryAISettings,
  type MemorySettingsPatch,
} from '../types/settings';

import { useMemorySettings, useAgenticStatus } from '../api/queries';
import { usePatchSettings } from '../api/mutations';
import { MemorySection } from './MemorySection';
import { MemoryField, MEMORY_FIELD_INPUT_CLASS } from './MemoryField';
import { MemoryBanner } from './MemoryBanner';
import { MemoryModelSelector } from './MemoryModelSelector';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Bounds -- mirror the backend's Pydantic validators exactly
// ---------------------------------------------------------------------------

const BOUNDS = {
  agentic_max_hops: { min: 1, max: 5, step: 1 },
  agentic_timeout_seconds: { min: 5, max: 120, step: 5 },
  agentic_confidence_threshold: { min: 0, max: 1, step: 0.05 },
  graph_search_max_depth: { min: 0, max: 3, step: 1 },
  graph_search_max_relations: { min: 1, max: 100, step: 1 },
  graph_search_min_query_entities: { min: 1, max: 5, step: 1 },
  graph_search_memories_per_entity: { min: 1, max: 100, step: 1 },
  graph_backfill_interval_seconds: { min: 60, max: 86_400, step: 60 },
  graph_backfill_batch_size: { min: 1, max: 200, step: 1 },
} as const;

// ---------------------------------------------------------------------------
// Draft
// ---------------------------------------------------------------------------

/**
 * Everything this form edits, in one flat object.
 *
 * `retrieval` and `ai` are kept as FULL copies (not deltas) so the
 * inputs always render a concrete value and the per-field diff has a
 * stable baseline to compare against.
 */
interface AgenticDraft {
  retrieval: MemoryRetrievalSettings;
  agentic: MemoryTaskModel;
  graph_indexing_enabled: boolean;
  graph_backfill_interval_seconds: number;
  graph_backfill_batch_size: number;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function AgenticSettingsForm() {
  const { t } = useTranslation('memory');
  const settingsQuery = useMemorySettings();
  const patchSettings = usePatchSettings();
  const agenticStatus = useAgenticStatus();

  const server = settingsQuery.data;
  const serverRetrieval = server?.retrieval;
  const serverAi = server?.ai;

  const [draft, setDraft] = useState<AgenticDraft | null>(null);
  const [saveSucceeded, setSaveSucceeded] = useState(false);

  // ---- Hydrate from server -------------------------------------
  useEffect(() => {
    if (!serverRetrieval || !serverAi) return;
    if (draft !== null && !saveSucceeded) return;
    setDraft({
      // Merge over defaults so a backend that predates Phase 6 (and
      // therefore omits the new keys) still renders concrete values
      // instead of `undefined` in every input.
      retrieval: {
        ...DEFAULT_MEMORY_RETRIEVAL_SETTINGS,
        ...serverRetrieval,
      },
      agentic: {
        ...DEFAULT_MEMORY_AI_SETTINGS.agentic,
        ...(serverAi.agentic ?? {}),
      },
      graph_indexing_enabled:
        serverAi.graph_indexing_enabled ??
        DEFAULT_MEMORY_AI_SETTINGS.graph_indexing_enabled,
      graph_backfill_interval_seconds:
        serverAi.graph_backfill_interval_seconds ??
        DEFAULT_MEMORY_AI_SETTINGS.graph_backfill_interval_seconds,
      graph_backfill_batch_size:
        serverAi.graph_backfill_batch_size ??
        DEFAULT_MEMORY_AI_SETTINGS.graph_backfill_batch_size,
    });
    setSaveSucceeded(false);
  }, [serverRetrieval, serverAi, draft, saveSucceeded]);

  // ---- Patch: changed leaf fields only -------------------------
  const patch = useMemo<MemorySettingsPatch | null>(() => {
    if (!draft || !serverRetrieval || !serverAi) return null;

    // --- retrieval half: per-field diff -------------------------
    const retrievalDelta: Partial<MemoryRetrievalSettings> = {};
    for (const key of RETRIEVAL_KEYS) {
      if (draft.retrieval[key] !== serverRetrieval[key]) {
        // TypeScript cannot narrow a union key here; the assignment
        // is sound because `key` indexes the same interface on both
        // sides and the values came from that interface.
        (retrievalDelta as Record<string, unknown>)[key] =
          draft.retrieval[key];
      }
    }

    // --- ai half: build the full object -------------------------
    const agenticDelta: Partial<MemoryTaskModel> = {};
    for (const key of AGENTIC_KEYS) {
      if (draft.agentic[key] !== serverAi.agentic?.[key]) {
        (agenticDelta as Record<string, unknown>)[key] =
          draft.agentic[key];
      }
    }
    const graphChanged =
      draft.graph_indexing_enabled !== serverAi.graph_indexing_enabled ||
      draft.graph_backfill_interval_seconds !==
        serverAi.graph_backfill_interval_seconds ||
      draft.graph_backfill_batch_size !==
        serverAi.graph_backfill_batch_size;

    const aiChanged =
      Object.keys(agenticDelta).length > 0 || graphChanged;

    const result: MemorySettingsPatch = {};
    if (Object.keys(retrievalDelta).length > 0) {
      result.retrieval = retrievalDelta;
    }
    if (aiChanged) {
      // FULL object on the wire -- see the module JSDoc.
      result.ai = mergeMemoryAISettings(serverAi, {
        agentic: agenticDelta,
        graph_indexing_enabled: draft.graph_indexing_enabled,
        graph_backfill_interval_seconds:
          draft.graph_backfill_interval_seconds,
        graph_backfill_batch_size: draft.graph_backfill_batch_size,
      });
    }

    return Object.keys(result).length > 0 ? result : null;
  }, [draft, serverRetrieval, serverAi]);

  const isDirty = patch !== null;
  const isSaving = patchSettings.isPending;

  // ---- Actions ------------------------------------------------
  async function handleSave() {
    if (!patch) return;
    try {
      await patchSettings.mutateAsync(patch);
      setSaveSucceeded(true);
    } catch {
      // Surfaced via patchSettings.error below.
    }
  }

  function handleReset() {
    if (!serverRetrieval || !serverAi) return;
    setDraft({
      retrieval: deepClone(serverRetrieval),
      agentic: {
        ...DEFAULT_MEMORY_AI_SETTINGS.agentic,
        ...(serverAi.agentic ?? {}),
      },
      graph_indexing_enabled: serverAi.graph_indexing_enabled,
      graph_backfill_interval_seconds:
        serverAi.graph_backfill_interval_seconds,
      graph_backfill_batch_size: serverAi.graph_backfill_batch_size,
    });
  }

  function updateRetrieval<K extends keyof MemoryRetrievalSettings>(
    key: K,
    value: MemoryRetrievalSettings[K],
  ) {
    setDraft((prev) =>
      prev ? { ...prev, retrieval: { ...prev.retrieval, [key]: value } } : prev,
    );
  }

  // ---- Loading ------------------------------------------------
  if (!draft || !serverRetrieval || !serverAi) {
    return (
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 size={12} className="animate-spin" />
          <span className="text-[11px]">
            {t('settings.agentic.loading', 'Loading agentic settings...')}
          </span>
        </div>
      </div>
    );
  }

  // ---- Derived status ----------------------------------------
  const agenticOn = draft.retrieval.agentic_enabled;
  const graphOn = draft.retrieval.graph_search_enabled;
  const indexingOn = draft.graph_indexing_enabled;

  // The agentic MODEL is the one prerequisite the user can satisfy
  // here; `ai.enabled` lives on the Automation tab and is reported
  // read-only so the message is actionable.
  const aiEnabled = serverAi.enabled;
  const modelConfigured = Boolean(draft.agentic.model_id);
  const agenticReady = agenticOn && aiEnabled && modelConfigured;

  // Graph search reads what indexing writes. Both can be on with an
  // empty graph, which is valid (the backfill will fill it) -- so
  // this is a hint, not an error.
  const graphWillBeEmpty = graphOn && !indexingOn;

  return (
    <div className="space-y-4">
      {/* ================= Agentic search ================= */}
      <MemorySection
        icon={BrainCircuit}
        title={t('settings.agentic.title', 'Agentic Search')}
        subtitle={t(
          'settings.agentic.subtitle',
          'Answer complex questions with multi-hop reasoning instead of a memory list',
        )}
        accent="purple"
      >
        <div className="space-y-3">
          <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border/60 p-3">
            <input
              type="checkbox"
              checked={agenticOn}
              onChange={(e) =>
                updateRetrieval('agentic_enabled', e.target.checked)
              }
              className="mt-0.5 h-3.5 w-3.5 cursor-pointer rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
            />
            <span className="min-w-0">
              <span className="block text-xs font-medium text-foreground">
                {t(
                  'settings.agentic.enable',
                  'Enable agentic multi-hop search',
                )}
              </span>
              <span className="mt-0.5 block text-[10px] leading-snug text-muted-foreground">
                {t(
                  'settings.agentic.enableHelp',
                  'Decomposes the question, searches 2-3 hops, and synthesizes a cited answer. Several seconds and several LLM calls per question, so it is never used for the normal chat turn.',
                )}
              </span>
            </span>
          </label>

          {agenticOn && (
            <div className="space-y-3">
              {/* Model -- the prerequisite that actually gates the feature. */}
              <MemoryModelSelector
                label={t('settings.agentic.model', 'Agentic model')}
                help={t(
                  'settings.agentic.modelHelp',
                  'Used for query rewriting, gap analysis, and evidence synthesis. Falls back to the extraction model, then to the first model marked as a memory model.',
                )}
                providerId={draft.agentic.provider_id}
                modelId={draft.agentic.model_id}
                onChange={(providerId, modelId) =>
                  setDraft((prev) =>
                    prev
                      ? {
                          ...prev,
                          agentic: {
                            ...prev.agentic,
                            provider_id: providerId,
                            model_id: modelId,
                          },
                        }
                      : prev,
                  )
                }
              />

              {!aiEnabled && (
                <MemoryBanner
                  tone="warning"
                  message={t(
                    'settings.agentic.aiDisabled',
                    'AI is disabled. Agentic search cannot run until you enable "AI enabled" on the Automation tab.',
                  )}
                />
              )}
              {aiEnabled && !modelConfigured && (
                <MemoryBanner
                  tone="warning"
                  message={t(
                    'settings.agentic.noModel',
                    'No agentic model selected. Without a model every agentic request falls back to standard retrieval.',
                  )}
                />
              )}
              {aiEnabled && modelConfigured && !agenticReady && (
                <MemoryBanner
                  tone="info"
                  message={t(
                    'settings.agentic.saveToActivate',
                    'Save to activate. The backend only builds the agentic pipeline after these settings are stored.',
                  )}
                />
              )}

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <MemoryField
                  label={t('settings.agentic.maxHops', 'Max hops')}
                  help={t(
                    'settings.agentic.maxHopsHelp',
                    'Retrieval rounds. Each hop costs one search per sub-query plus one LLM call.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.retrieval.agentic_max_hops}
                    min={BOUNDS.agentic_max_hops.min}
                    max={BOUNDS.agentic_max_hops.max}
                    step={BOUNDS.agentic_max_hops.step}
                    onChange={(e) =>
                      updateRetrieval(
                        'agentic_max_hops',
                        Number(e.target.value),
                      )
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>

                <MemoryField
                  label={t(
                    'settings.agentic.timeout',
                    'Timeout (seconds)',
                  )}
                  help={t(
                    'settings.agentic.timeoutHelp',
                    'Hard ceiling for one agentic run. On timeout the evidence gathered so far is returned.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.retrieval.agentic_timeout_seconds}
                    min={BOUNDS.agentic_timeout_seconds.min}
                    max={BOUNDS.agentic_timeout_seconds.max}
                    step={BOUNDS.agentic_timeout_seconds.step}
                    onChange={(e) =>
                      updateRetrieval(
                        'agentic_timeout_seconds',
                        Number(e.target.value),
                      )
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>

                <MemoryField
                  label={t(
                    'settings.agentic.confidenceThreshold',
                    'Confidence threshold',
                  )}
                  help={t(
                    'settings.agentic.confidenceThresholdHelp',
                    'Answers below this are LABELLED uncertain, not discarded.',
                  )}
                >
                  <div className="flex items-center gap-2">
                    <input
                      type="range"
                      value={draft.retrieval.agentic_confidence_threshold}
                      min={BOUNDS.agentic_confidence_threshold.min}
                      max={BOUNDS.agentic_confidence_threshold.max}
                      step={BOUNDS.agentic_confidence_threshold.step}
                      onChange={(e) =>
                        updateRetrieval(
                          'agentic_confidence_threshold',
                          Number(e.target.value),
                        )
                      }
                      className="flex-1 accent-primary"
                    />
                    <span className="min-w-[2.75rem] font-mono text-[11px] tabular-nums text-end">
                      {Math.round(
                        draft.retrieval.agentic_confidence_threshold * 100,
                      )}
                      %
                    </span>
                  </div>
                </MemoryField>
              </div>
            </div>
          )}
        </div>
      </MemorySection>

      {/* ================= Knowledge graph ================= */}
      <MemorySection
        icon={Network}
        title={t('settings.graph.title', 'Knowledge Graph')}
        subtitle={t(
          'settings.graph.subtitle',
          'Find memories linked through entities — "Ahmed" → "AcmeCorp" → "Peldrun"',
        )}
        accent="cyan"
      >
        <div className="space-y-3">
          <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border/60 p-3">
            <input
              type="checkbox"
              checked={graphOn}
              onChange={(e) =>
                updateRetrieval('graph_search_enabled', e.target.checked)
              }
              className="mt-0.5 h-3.5 w-3.5 cursor-pointer rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
            />
            <span className="min-w-0">
              <span className="block text-xs font-medium text-foreground">
                {t(
                  'settings.graph.enableSearch',
                  'Use the knowledge graph in search',
                )}
              </span>
              <span className="mt-0.5 block text-[10px] leading-snug text-muted-foreground">
                {t(
                  'settings.graph.enableSearchHelp',
                  'Adds a third ranked source to normal search. Independent of agentic search — this works on its own.',
                )}
              </span>
            </span>
          </label>

          {graphOn && (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <MemoryField
                label={t('settings.graph.maxDepth', 'Max depth')}
                help={t(
                  'settings.graph.maxDepthHelp',
                  '0 = direct links only; 1 = neighbours; 2 = two hops.',
                )}
              >
                <input
                  type="number"
                  value={draft.retrieval.graph_search_max_depth}
                  min={BOUNDS.graph_search_max_depth.min}
                  max={BOUNDS.graph_search_max_depth.max}
                  step={BOUNDS.graph_search_max_depth.step}
                  onChange={(e) =>
                    updateRetrieval(
                      'graph_search_max_depth',
                      Number(e.target.value),
                    )
                  }
                  className={MEMORY_FIELD_INPUT_CLASS}
                />
              </MemoryField>

              <MemoryField
                label={t('settings.graph.maxRelations', 'Max relations')}
                help={t(
                  'settings.graph.maxRelationsHelp',
                  'Per entity. Stops one hub node from consuming the budget.',
                )}
              >
                <input
                  type="number"
                  value={draft.retrieval.graph_search_max_relations}
                  min={BOUNDS.graph_search_max_relations.min}
                  max={BOUNDS.graph_search_max_relations.max}
                  step={BOUNDS.graph_search_max_relations.step}
                  onChange={(e) =>
                    updateRetrieval(
                      'graph_search_max_relations',
                      Number(e.target.value),
                    )
                  }
                  className={MEMORY_FIELD_INPUT_CLASS}
                />
              </MemoryField>

              <MemoryField
                label={t('settings.graph.minEntities', 'Min entities')}
                help={t(
                  'settings.graph.minEntitiesHelp',
                  'Known entities a query must mention before the graph participates.',
                )}
              >
                <input
                  type="number"
                  value={draft.retrieval.graph_search_min_query_entities}
                  min={BOUNDS.graph_search_min_query_entities.min}
                  max={BOUNDS.graph_search_min_query_entities.max}
                  step={BOUNDS.graph_search_min_query_entities.step}
                  onChange={(e) =>
                    updateRetrieval(
                      'graph_search_min_query_entities',
                      Number(e.target.value),
                    )
                  }
                  className={MEMORY_FIELD_INPUT_CLASS}
                />
              </MemoryField>

              <MemoryField
                label={t('settings.graph.perEntity', 'Memories per entity')}
                help={t(
                  'settings.graph.perEntityHelp',
                  'Cap on memories hydrated for one entity.',
                )}
              >
                <input
                  type="number"
                  value={draft.retrieval.graph_search_memories_per_entity}
                  min={BOUNDS.graph_search_memories_per_entity.min}
                  max={BOUNDS.graph_search_memories_per_entity.max}
                  step={BOUNDS.graph_search_memories_per_entity.step}
                  onChange={(e) =>
                    updateRetrieval(
                      'graph_search_memories_per_entity',
                      Number(e.target.value),
                    )
                  }
                  className={MEMORY_FIELD_INPUT_CLASS}
                />
              </MemoryField>
            </div>
          )}

          {/* ---------- Indexing (the half the brief omitted) ---------- */}
          <div className="rounded-md border border-border/60 p-3">
            <label className="flex cursor-pointer items-start gap-2">
              <input
                type="checkbox"
                checked={indexingOn}
                onChange={(e) =>
                  setDraft((prev) =>
                    prev
                      ? {
                          ...prev,
                          graph_indexing_enabled: e.target.checked,
                        }
                      : prev,
                  )
                }
                className="mt-0.5 h-3.5 w-3.5 cursor-pointer rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
              />
              <span className="min-w-0">
                <span className="block text-xs font-medium text-foreground">
                  {t(
                    'settings.graph.enableIndexing',
                    'Build the graph from new memories',
                  )}
                </span>
                <span className="mt-0.5 block text-[10px] leading-snug text-muted-foreground">
                  {t(
                    'settings.graph.enableIndexingHelp',
                    'Extracts entities in the background after each write. Nothing above can find anything until this has run at least once.',
                  )}
                </span>
              </span>
            </label>

            {indexingOn && (
              <div className="mt-2 grid grid-cols-2 gap-2">
                <MemoryField
                  label={t('settings.graph.backfillInterval', 'Backfill every (s)')}
                  help={t(
                    'settings.graph.backfillIntervalHelp',
                    'Also indexes memories written outside the pipeline (the save tool, imports).',
                  )}
                >
                  <input
                    type="number"
                    value={draft.graph_backfill_interval_seconds}
                    min={BOUNDS.graph_backfill_interval_seconds.min}
                    max={BOUNDS.graph_backfill_interval_seconds.max}
                    step={BOUNDS.graph_backfill_interval_seconds.step}
                    onChange={(e) =>
                      setDraft((prev) =>
                        prev
                          ? {
                              ...prev,
                              graph_backfill_interval_seconds: Number(
                                e.target.value,
                              ),
                            }
                          : prev,
                      )
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>

                <MemoryField
                  label={t('settings.graph.backfillBatch', 'Backfill batch size')}
                  help={t(
                    'settings.graph.backfillBatchHelp',
                    'Memories indexed per tick. Bounds the cost of draining a large backlog.',
                  )}
                >
                  <input
                    type="number"
                    value={draft.graph_backfill_batch_size}
                    min={BOUNDS.graph_backfill_batch_size.min}
                    max={BOUNDS.graph_backfill_batch_size.max}
                    step={BOUNDS.graph_backfill_batch_size.step}
                    onChange={(e) =>
                      setDraft((prev) =>
                        prev
                          ? {
                              ...prev,
                              graph_backfill_batch_size: Number(
                                e.target.value,
                              ),
                            }
                          : prev,
                      )
                    }
                    className={MEMORY_FIELD_INPUT_CLASS}
                  />
                </MemoryField>
              </div>
            )}

            {graphWillBeEmpty && (
              <div className="mt-2">
                <MemoryBanner
                  tone="warning"
                  message={t(
                    'settings.graph.emptyGraph',
                    'Graph search is on but graph building is off (or has not run yet). Searches will find nothing until the graph has been populated.',
                  )}
                />
              </div>
            )}
          </div>

          {/* ---------- Live availability ----------
              Three DIFFERENT causes, three different messages. The
              first version collapsed them into "no model resolved",
              which was actively misleading when `ai.enabled` was
              off -- the user had saved a model and was told to save
              a model again. `ai_enabled` / `model_resolved` come
              from the status endpoint so the copy can be exact. */}
          {agenticStatus.data && !agenticStatus.data.available && (
            <MemoryBanner
              tone="info"
              message={
                !agenticStatus.data.enabled
                  ? t(
                      'settings.agentic.statusDisabled',
                      'Agentic search is currently off on the server.',
                    )
                  : !agenticStatus.data.ai_enabled
                    ? t(
                        'settings.agentic.statusAiOff',
                        'Agentic search needs the AI subsystem. Turn on "AI enabled" on the Automation tab, then save here again.',
                      )
                    : agenticStatus.data.model_resolved
                      ? t(
                          'settings.agentic.statusNotBuilt',
                          'Settings are valid but the agentic pipeline is not built yet on the server. Saving again rebuilds it; if it persists, check the server log for "agentic".',
                        )
                      : t(
                          'settings.agentic.statusUnconfigured',
                          'No agentic model resolved. Save a model above, or turn on "AI enabled" on the Automation tab.',
                        )
              }
            />
          )}
        </div>
      </MemorySection>

      {/* ---------- Error surface ---------- */}
      {patchSettings.error && !isSaving && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/50 bg-destructive/5 px-3 py-2 text-[11px] text-destructive">
          <XIcon size={14} className="mt-0.5 shrink-0" />
          <div>
            <strong className="font-semibold">
              {t('settings.agentic.saveFailed', 'Save failed')}
            </strong>
            <p className="mt-0.5">{patchSettings.error.message}</p>
          </div>
        </div>
      )}

      {/* ---------- Sticky save bar ---------- */}
      <div className="sticky bottom-4 z-10 flex items-center justify-between gap-2 rounded-md border border-border bg-card/95 px-3 py-2 shadow-lg backdrop-blur">
        <span className="text-[10px] text-muted-foreground">
          {isDirty
            ? t('settings.agentic.dirtyHint', 'You have unsaved changes')
            : t('settings.agentic.cleanHint', 'No unsaved changes')}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleReset}
            disabled={!isDirty || isSaving}
            className={cn(
              'inline-flex h-7 items-center gap-1.5 rounded-sm border border-border bg-card px-2.5 text-[11px] font-medium text-foreground transition-colors',
              'hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            <RotateCcw size={11} />
            {t('settings.agentic.reset', 'Reset')}
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={!isDirty || isSaving}
            className={cn(
              'inline-flex h-7 items-center gap-1.5 rounded-sm bg-primary px-2.5 text-[11px] font-medium text-primary-foreground transition-colors',
              'hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            {isSaving ? (
              <>
                <Loader2 size={11} className="animate-spin" />
                {t('settings.agentic.saving', 'Saving...')}
              </>
            ) : (
              <>
                <Save size={11} />
                {t('settings.agentic.save', 'Save changes')}
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * The `MemoryRetrievalSettings` keys this form owns.
 *
 * Listed EXPLICITLY rather than derived from `Object.keys` so the
 * diff can never accidentally include a Phase 4/5 field the other
 * form owns -- two forms patching the same key would produce
 * last-write-wins surprises.
 */
const RETRIEVAL_KEYS = [
  'agentic_enabled',
  'agentic_max_hops',
  'agentic_timeout_seconds',
  'agentic_confidence_threshold',
  'graph_search_enabled',
  'graph_search_max_depth',
  'graph_search_max_relations',
  'graph_search_min_query_entities',
  'graph_search_memories_per_entity',
] as const satisfies readonly (keyof MemoryRetrievalSettings)[];

/** The `MemoryTaskModel` keys the agentic model picker edits. */
const AGENTIC_KEYS = [
  'provider_id',
  'model_id',
] as const satisfies readonly (keyof MemoryTaskModel)[];

function deepClone<T>(value: T): T {
  if (value === null || typeof value !== 'object') return value;
  if (Array.isArray(value)) return value.map(deepClone) as T;
  const out: Record<string, unknown> = {};
  for (const [key, inner] of Object.entries(value as Record<string, unknown>)) {
    out[key] = deepClone(inner);
  }
  return out as T;
}
