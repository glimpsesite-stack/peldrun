// src/features/memory/types/ai.ts

/**
 * AI extraction subsystem types (Phase 3).
 *
 * This module is the TypeScript mirror of the backend's
 * `MemoryAISettings` and `MemoryAIExtractionSettings` Pydantic
 * models (see `peldrun/memory/domain/settings.py`). Every field
 * is snake_case to match the wire payload produced by the
 * backend's `model_dump(mode="json")` -- there is no
 * transformation layer.
 *
 * Scope of this file
 * ------------------
 *
 *   * `MemoryAISettings`         -- top-level AI configuration.
 *   * `MemoryAIExtractionSettings` -- per-task LLM configuration
 *                                    (provider, model, limits).
 *   * `MemoryEmbeddingSettings`  -- Phase 4 (semantic search).
 *   * `MemoryRetrievalSettings`  -- Phase 4 (hybrid retrieval).
 *   * Job / Batch types          -- Phase 3 (jobs UI).
 *   * `DEFAULT_MEMORY_AI_SETTINGS` -- safe defaults.
 *   * `MemoryAIBounds`           -- Zod bounds source.
 *
 * All string literals are ASCII. Translations live in
 * `public/locales/{ar,en}/memory.json` under `settings.ai.*`.
 */

// ---------------------------------------------------------------------------
// Extraction trigger
// ---------------------------------------------------------------------------

/**
 * Which trigger pattern fires the extractor.
 *
 *   * `after_turn`    -- fires on every completed turn (batched
 *                       by default via `after_turn_batching`).
 *   * `after_session` -- fires only when `session.json.status`
 *                       becomes 'completed'.
 *   * `both`          -- fires on either.
 *
 * The actual routing decisions are made by `TriggerRouter`
 * (`omweb/memory/extraction/trigger_router.py`). This type
 * exists so the UI can render a selector without duplicating
 * the union at every call site.
 */
export type ExtractionTrigger =
  | 'after_turn'
  | 'after_session'
  | 'both';

/**
 * Array of all valid `ExtractionTrigger` values, in display
 * order. Used by `ExtractionTriggerSelector` to iterate.
 */
export const ALL_EXTRACTION_TRIGGERS: readonly ExtractionTrigger[] = [
  'after_turn',
  'after_session',
  'both',
] as const;

// ---------------------------------------------------------------------------
// Per-task model config
// ---------------------------------------------------------------------------

/**
 * LLM configuration for one extraction task.
 *
 * Distinct from `MemoryAIExtractionSettings` only in name --
 * this is the shape used inside `MemoryAISettings.extraction`
 * and `MemoryAISettings.consolidation`. Provider credentials
 * (API key, base_url) live in `models_vault.json`, keyed by
 * `provider_id`; they are NOT stored here.
 */
export interface MemoryTaskModel {
  /** Provider id (e.g. 'openai', 'anthropic', 'lmstudio'). */
  provider_id: string | null;
  /** Model id understood by the provider (e.g. 'gpt-4o-mini'). */
  model_id: string | null;
  /** Hard cap on completion tokens per call. */
  max_output_tokens: number;
  /** Wall-clock timeout for the entire call, in seconds. */
  timeout_seconds: number;
}

// ---------------------------------------------------------------------------
// Top-level AI settings
// ---------------------------------------------------------------------------

/**
 * AI extraction subsystem configuration.
 *
 * Full gating contract: the extraction pipeline initializes
 * only when ALL of the following are true:
 *
 *   1. `enabled === true`
 *   2. `extraction_enabled === true`
 *   3. The current memory `mode` permits writes
 *      (`mode.can_write` -- extraction writes memories and
 *      would fail on every persistence attempt in READ_ONLY or
 *      DISABLED mode).
 *
 * When any condition fails, the extraction pipeline is
 * entirely absent: no outbox, no worker, no jobs. The rest of
 * the memory subsystem operates exactly as Phase 0.
 *
 * See `omweb/memory/manager/memory_service.py`'s
 * `_initialize_extraction` for the runtime enforcement of this
 * contract.
 */
export interface MemoryAISettings {
  /** Master switch for the AI extraction subsystem. */
  enabled: boolean;

  /** Second opt-in flag. Both `enabled` and this must be `true`. */
  extraction_enabled: boolean;

  /**
   * Consolidation subsystem (merging similar memories).
   * Phase 5. Off by default; the consolidation jobs run in the
   * same worker as extraction.
   */
  consolidation_enabled: boolean;

  /**
   * Which trigger pattern fires the extractor. See
   * `ExtractionTrigger` for the exact semantics.
   */
  extraction_trigger: ExtractionTrigger;

  /** LLM config for the extraction task. */
  extraction: MemoryTaskModel;

  /** LLM config for the consolidation task. */
  consolidation: MemoryTaskModel;

  /**
   * Maximum messages sent to the LLM per extraction job.
   * A longer conversation is truncated to the LAST N messages
   * (most recent context wins). Bounds the per-job cost.
   */
  max_messages_per_job: number;

  /**
   * Maximum input tokens per extraction job. A second
   * cost bound applied AFTER `max_messages_per_job`. If the
   * truncated message set still exceeds this, the oldest
   * messages are dropped.
   */
  max_input_tokens: number;

  /**
   * Ceiling on candidate memories the LLM may propose per job.
   * Post-parse bound; the LLM may return more, but only the
   * highest-confidence N are accepted.
   */
  max_candidates_per_job: number;

  /**
   * Minimum LLM-reported confidence for a candidate to be
   * accepted without human review. Candidates below this
   * threshold are queued for Pending Review (Phase 5).
   * Range: 0.0 (accept all) to 1.0 (accept only max).
   */
  min_confidence: number;

  /**
   * Daily cost ceiling, in nano-USD. When total cost across
   * all extraction jobs in the current UTC day exceeds this
   * value, new jobs are paused until midnight. 50,000,000 nano
   * = $0.05/day (a sensible default for small deployments).
   */
  max_cost_nano_usd_per_day: number;

  /** Enable the batch retroactive extraction subsystem. */
  batch_enabled: boolean;

  /**
   * Maximum concurrent batch jobs. The current default (1)
   * serializes batch work to avoid SQLite lock contention.
   * Raising this is only safe with a non-SQLite provider.
   */
  batch_max_concurrent_jobs: number;

  /**
   * Auto-schedule a batch every N hours. `null` = manual
   * trigger only (the "Run batch now" button on
   * `/memory/batch`).
   */
  batch_auto_schedule_hours: number | null;

  /**
   * Heartbeat interval for the worker's session-snapshot
   * re-check. Lower values detect mid-flight session changes
   * faster but consume more IO. Range: 1..60 seconds.
   */
  snapshot_heartbeat_interval_seconds: number;

  /**
   * When `extraction_trigger === 'after_turn'`, batch all
   * pending turns into ONE job per notification instead of one
   * job per turn. Cheaper (1 LLM call instead of N) but
   * delays per-turn visibility.
   */
  after_turn_batching: boolean;
}

// ---------------------------------------------------------------------------
// AI settings overlay (partial form)
// ---------------------------------------------------------------------------

/**
 * A DEEP-partial form of `MemoryAISettings`.
 *
 * Every field is optional, AND the nested `extraction` and
 * `consolidation` objects are partial too. This is the shape
 * accepted by:
 *
 *   * `MemorySettingsPatch.ai`      (PATCH /settings body)
 *   * `MemoryProjectOverride.ai`    (PUT /projects/{id}/settings)
 *   * `computeEffectiveOverride`    (client-side diffing)
 *
 * Why not just `Partial<MemoryAISettings>`?
 * -----------------------------------------
 * `Partial<T>` only makes the TOP-LEVEL keys optional -- it
 * leaves `extraction: MemoryTaskModel` requiring ALL four of
 * its fields. That is stricter than what the UI actually
 * produces: the form may touch only `provider_id` and leave
 * `model_id`/`max_output_tokens`/`timeout_seconds` untouched.
 *
 * `computeEffectiveOverride` expands a partial overlay to a
 * FULL `MemoryAISettings` before transmission (via
 * `mergeMemoryAISettings`), so the wire payload always carries
 * every field. But the TYPE the caller hands to that helper
 * has to reflect the partial-ness, or TypeScript rejects the
 * call before it ever runs.
 *
 * Named separately (not inlined) so that all three consumer
 * types above stay in lockstep -- adding a field to
 * `MemoryAISettings` and forgetting to mirror it here is a
 * compile error at the `computeEffectiveOverride` callsite.
 */
export interface MemoryAISettingsOverlay {
  enabled?: boolean;
  extraction_enabled?: boolean;
  consolidation_enabled?: boolean;
  extraction_trigger?: ExtractionTrigger;
  extraction?: Partial<MemoryTaskModel>;
  consolidation?: Partial<MemoryTaskModel>;
  max_messages_per_job?: number;
  max_input_tokens?: number;
  max_candidates_per_job?: number;
  min_confidence?: number;
  max_cost_nano_usd_per_day?: number;
  batch_enabled?: boolean;
  batch_max_concurrent_jobs?: number;
  batch_auto_schedule_hours?: number | null;
  snapshot_heartbeat_interval_seconds?: number;
  after_turn_batching?: boolean;
}

// ---------------------------------------------------------------------------
// Embedding settings (Phase 4)
// ---------------------------------------------------------------------------

/**
 * Semantic search configuration (Phase 4).
 *
 * Stubbed here so `MemorySettings.embedding` can be typed now,
 * even though the backend does not yet consume it. When Phase 4
 * lands, `_PATCHABLE_FIELDS` will accept these fields and the
 * retrieval pipeline will honor them.
 */
export interface MemoryEmbeddingSettings {
  /** Enable vector-based retrieval. */
  enabled: boolean;
  /** Embedding provider id (e.g. 'openai', 'google'). */
  provider_id: string | null;
  /** Embedding model id (e.g. 'text-embedding-3-small'). */
  model_id: string | null;
  /** Vector dimensions (model-dependent; read-only). */
  dimensions: number;
  /** Batch size for embedding generation. */
  batch_size: number;
  /** Daily cost ceiling for embedding jobs. */
  max_cost_nano_usd_per_day: number;
  /** Model version (bumped when the provider rotates a model). */
  version: string;
}

// ---------------------------------------------------------------------------
// Retrieval settings (Phase 4)
// ---------------------------------------------------------------------------

/**
 * Retrieval strategy configuration (Phase 4).
 *
 * The four `mode` values:
 *   * `lexical`    -- FTS5 only (Phase 1 behavior; default).
 *   * `vector`     -- vector similarity only.
 *   * `hybrid`     -- both, fused by `vector_alpha`.
 *   * `automatic`  -- heuristic: short query -> lexical,
 *                     long query -> hybrid.
 */
export interface MemoryRetrievalSettings {
  mode: 'lexical' | 'vector' | 'hybrid' | 'automatic';
  /** Weight of lexical score in hybrid fusion. Range 0..1. */
  vector_alpha: number;
  /** Minimum normalized lexical score to include. */
  min_lexical_score: number;
  /** Minimum cosine similarity for vector hits. */
  min_vector_similarity: number;
  /** Enable cross-encoder reranking (Phase 5). */
  rerank_enabled: boolean;
  /** RRF k parameter for reciprocal rank fusion. */
  rrf_k: number;
}

// ---------------------------------------------------------------------------
// Job types (Phase 3)
// ---------------------------------------------------------------------------

/**
 * Lifecycle status of an extraction job.
 *
 * Transitions:
 *   pending -> running -> completed
 *                      -> failed -> retry_pending -> running
 *                                 -> failed (max attempts)
 *                      -> cancelled (user action)
 */
export type JobStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'retry_pending';

/**
 * Type of work a job performs.
 *   * `extraction`    -- LLM extracts candidate memories.
 *   * `embedding`     -- embed memory content for vector search.
 *   * `consolidation` -- merge similar memories.
 */
export type JobType = 'extraction' | 'embedding' | 'consolidation';

/**
 * Per-candidate decision recorded on a job.
 *
 *   * `add`            -- new memory created.
 *   * `skip`           -- duplicate of an existing memory.
 *   * `merge`          -- merged into an existing memory.
 *   * `reject`         -- LLM declined (insufficient evidence).
 *   * `conflict`       -- contradicts an existing memory
 *                        (queued for review).
 *   * `low_confidence` -- below `min_confidence` (queued for
 *                        review).
 */
export type JobDecisionAction =
  | 'add'
  | 'skip'
  | 'merge'
  | 'reject'
  | 'conflict'
  | 'low_confidence';

/**
 * One decision recorded during a job.
 *
 * `candidate_id` is the LLM-assigned id for the proposed
 * memory (stable within the job's response only). `matched_memory_id`
 * is set when `action` is `skip`/`merge`/`conflict` (points to
 * the existing memory the candidate matched).
 */
export interface JobDecision {
  candidate_id: string;
  action: JobDecisionAction;
  reason: string;
  confidence: number;
  matched_memory_id?: string;
}

/**
 * A single extraction job (or embedding/consolidation job).
 *
 * The UI consumes this shape in two places:
 *   * `MemoryJobsList`  -- list view (fields: status, trigger,
 *                          chat_id, candidates_count, error_code).
 *   * `JobDetailPage`   -- detail view (all fields, especially
 *                          `decisions`).
 */
export interface MemoryJob {
  job_id: string;
  job_type: JobType;
  chat_id: string;
  project_id: string | null;

  trigger_type: ExtractionTrigger;
  start_turn_index: number;
  end_turn_index: number;

  status: JobStatus;
  attempts: number;
  max_attempts: number;

  created_at: number;
  started_at: number | null;
  completed_at: number | null;

  candidates_count: number;
  created_memories_count: number;
  cost_nano_usd: number;

  error_code: string | null;
  error_message: string | null;

  decisions: JobDecision[];

  model_provider_id: string;
  model_id: string;
}

// ---------------------------------------------------------------------------
// Batch types (Phase 3)
// ---------------------------------------------------------------------------

/**
 * Lifecycle status of a batch extraction run.
 *
 *   * `running`            -- actively processing sessions.
 *   * `paused`             -- operator paused; resumes on request.
 *   * `completed`          -- all sessions processed.
 *   * `cancelled`          -- operator cancelled; remaining
 *                            sessions NOT processed.
 *   * `cost_limit_reached` -- daily cost ceiling hit; remaining
 *                            sessions deferred to tomorrow.
 */
export type BatchStatus =
  | 'running'
  | 'paused'
  | 'completed'
  | 'cancelled'
  | 'cost_limit_reached';

/**
 * Snapshot of the job currently being processed by a batch.
 *
 * `current_turn` and `total_turns` are shown as "turn 3/10" in
 * the progress panel; both are `null` between jobs.
 */
export interface CurrentJobInfo {
  job_id: string;
  chat_id: string;
  status: JobStatus;
  current_turn: number | null;
  total_turns: number | null;
}

/**
 * Full progress snapshot of a batch run.
 *
 * Polled every 2 seconds by `useBatchProgress` while the batch
 * is running. The `cost_nano_usd_so_far` and
 * `estimated_remaining_cost_nano_usd` fields are computed
 * server-side from the ledger; the UI just displays them.
 */
export interface BatchProgress {
  batch_id: string;
  started_at: number;
  status: BatchStatus;

  total_sessions: number;
  completed_sessions: number;
  failed_sessions: number;
  skipped_sessions: number;

  current_job: CurrentJobInfo | null;

  cost_nano_usd_so_far: number;
  estimated_remaining_cost_nano_usd: number;

  pause_requested: boolean;
  cancel_requested: boolean;
}

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

/**
 * Safe defaults for a fresh `MemoryAISettings`.
 *
 * All feature flags default to `false`: a new deployment must
 * explicitly opt in to extraction. `extraction_trigger` defaults
 * to `after_session` (the plan's recommended default -- cheaper
 * than `after_turn`, more accurate than `after_turn` without
 * batching).
 */
export const DEFAULT_MEMORY_AI_SETTINGS: MemoryAISettings = {
  enabled: false,
  extraction_enabled: false,
  consolidation_enabled: false,
  extraction_trigger: 'after_session',
  extraction: {
    // Aligned with the backend's MemoryAIExtractionSettings
    // defaults (peldrun/memory/domain/settings.py) so the UI
    // fallback (form.ai ?? DEFAULT_MEMORY_AI_SETTINGS) matches
    // what the backend produces on first boot. Keeping these
    // in sync prevents a "configured" model from appearing as
    // "not configured" in the form on a fresh install.
    provider_id: 'openai',
    model_id: 'gpt-4o-mini',
    max_output_tokens: 1200,
    timeout_seconds: 45,
  },
  consolidation: {
    provider_id: 'openai',
    model_id: 'gpt-4o-mini',
    max_output_tokens: 1200,
    timeout_seconds: 45,
  },
  max_messages_per_job: 40,
  max_input_tokens: 12_000,
  max_candidates_per_job: 20,
  min_confidence: 0.7,
  max_cost_nano_usd_per_day: 50_000_000,
  batch_enabled: true,
  batch_max_concurrent_jobs: 1,
  batch_auto_schedule_hours: null,
  snapshot_heartbeat_interval_seconds: 5,
  after_turn_batching: true,
};

// ---------------------------------------------------------------------------
// Numeric bounds
// ---------------------------------------------------------------------------

/**
 * Numeric bounds for every AI setting field.
 *
 * Consumed by `lib/validators.ts` to build Zod schemas. Every
 * bound here MUST match the corresponding Pydantic `Field(ge=...)`
 * on the backend -- a mismatch means the client thinks a value
 * is valid that the server rejects (or vice versa).
 *
 * The `max_output_tokens` and `timeout_seconds` bounds are
 * per-task (apply to both `extraction` and `consolidation`).
 */
export const MemoryAIBounds = {
  max_messages_per_job: { min: 1, max: 200 },
  max_input_tokens: { min: 512, max: 128_000 },
  max_candidates_per_job: { min: 1, max: 100 },
  min_confidence: { min: 0, max: 1 },
  max_cost_nano_usd_per_day: { min: 0, max: 10_000_000_000 },
  batch_max_concurrent_jobs: { min: 1, max: 4 },
  batch_auto_schedule_hours: { min: 1, max: 168 },
  max_output_tokens: { min: 128, max: 32_000 },
  timeout_seconds: { min: 5, max: 180 },
  snapshot_heartbeat_interval_seconds: { min: 1, max: 60 },
} as const;
