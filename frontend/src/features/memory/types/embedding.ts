// src/features/memory/types/embedding.ts

/**
 * Phase 4 -- Semantic search and hybrid retrieval domain types.
 *
 * This module is the TypeScript mirror of the backend's Phase 4
 * surface, transcribed directly from
 * `peldrun/memory/domain/settings.py` (the settings subtree) and
 * from the wire contract of `GET /api/config/embedding-models`
 * and `POST /api/memory/reindex`. Nothing here is renamed or
 * reshaped -- snake_case field names match the JSON payload
 * exactly (same policy as `settings.ts` and `ai.ts`).
 *
 * Scope of this file
 * ------------------
 *
 *   * `RetrievalMode`               -- the four retrieval strategies.
 *   * `MemoryEmbeddingSettings`     -- embedding sub-tree of
 *                                      `MemorySettings`.
 *   * `MemoryRetrievalSettings`     -- retrieval sub-tree of
 *                                      `MemorySettings`.
 *   * `EmbeddingModel`              -- one selectable model from
 *                                      the config catalog.
 *   * `EmbeddingStats`              -- the read-only coverage
 *                                      snapshot from
 *                                      `GET /api/memory/embedding-stats`.
 *   * `ReindexRequest`              -- body accepted by
 *                                      `POST /api/memory/reindex`.
 *   * Defaults + numeric bounds.
 *
 * What is NOT in this file
 * ------------------------
 *
 * Response envelopes (`EmbeddingModelsResponse`,
 * `ReindexResponse`, `ReindexErrorResponse`,
 * `EmbeddingStatsResponse`) live in `responses.ts`. This
 * follows the module's stated convention: domain types describe
 * SHAPES, envelopes describe TRANSPORT. See the JSDoc at the
 * top of `responses.ts` for the rationale.
 *
 * Relationship to `ai.ts`
 * -----------------------
 *
 * Phase 3's `ai.ts` and Phase 4's `embedding.ts` are distinct
 * subsystems that share the `MemorySettings` root:
 *
 *   * `ai.*`        -- LLM extraction pipeline (Phase 3).
 *   * `embedding.*` -- vector index + semantic retrieval (Phase 4).
 *
 * Enabling one does NOT require enabling the other. Keeping
 * them in separate files means a reviewer working on retrieval
 * does not have to skim past extraction config, and vice versa.
 */

// ---------------------------------------------------------------------------
// Retrieval mode
// ---------------------------------------------------------------------------

/**
 * The four retrieval strategies the backend's
 * `MemoryRetrievalRouter` supports.
 *
 * Semantics (mirrors `peldrun/memory/retrieval/router.py`):
 *
 *   * `lexical`   -- FTS5 BM25 only. No embeddings needed. Fast
 *                    (~10 ms), free, precise on exact keywords.
 *                    This is the Phase 1 default and the safe
 *                    fallback for every other mode.
 *
 *   * `vector`    -- cosine similarity over the sqlite-vec
 *                    table. Requires `embedding.enabled === true`
 *                    AND a valid `embedding.model_key`. Cannot
 *                    find keyword-only matches, but understands
 *                    paraphrases and cross-lingual queries.
 *                    Falls back to `lexical` server-side when
 *                    embeddings are missing (INFO log only --
 *                    the fallback is not surfaced to the client
 *                    because it is a legitimate degradation,
 *                    not an error).
 *
 *   * `hybrid`    -- runs BOTH lexical and vector queries, then
 *                    fuses the ranked lists (see `vector_alpha`
 *                    and `rrf_k` in `MemoryRetrievalSettings`).
 *                    Best quality overall; roughly 2x the cost
 *                    of `lexical` because every query requires
 *                    one embedding API call.
 *
 *   * `automatic` -- the router decides per-query: queries with
 *                    fewer than 3 tokens go to `lexical` (BM25
 *                    wins on short exact-term matches), longer
 *                    queries go to `hybrid`. Balances cost
 *                    against quality with no user configuration.
 *
 * The UI MUST NOT block saving a semantic mode when embeddings
 * are not enabled. Doing so would prevent the user from
 * pre-configuring before flipping the embedding switch. Instead,
 * the settings page shows a warning banner explaining that the
 * backend will fall back to `lexical` until embeddings are
 * enabled.
 */
export type RetrievalMode =
  | 'lexical'
  | 'vector'
  | 'hybrid'
  | 'automatic';

/**
 * All valid `RetrievalMode` values, in display order.
 *
 * Consumed by `RetrievalModeSelector` so the component does not
 * duplicate the union at its callsite -- adding a fifth mode
 * later is a one-line change here, and the component iterates
 * over it automatically.
 */
export const ALL_RETRIEVAL_MODES: readonly RetrievalMode[] = [
  'lexical',
  'vector',
  'hybrid',
  'automatic',
] as const;

// ---------------------------------------------------------------------------
// Embedding settings sub-tree
// ---------------------------------------------------------------------------

/**
 * Semantic search configuration.
 *
 * Mirrors `peldrun.memory.domain.settings.MemoryEmbeddingSettings`
 * exactly. Deliberately minimal: the backend derives dimensions,
 * batch size, per-provider credentials, and pricing from the
 * selected `model_key` and the provider registry, so most of the
 * configuration lives OUTSIDE `MemorySettings`. The UI only
 * needs to expose the master switch and the model picker.
 *
 * Field semantics:
 *
 *   * `enabled`    -- master switch. `false` disables embedding
 *                     GENERATION (the worker stops draining
 *                     `memory_embedding_jobs`) and forces the
 *                     retrieval router to fall back to
 *                     `lexical` regardless of
 *                     `retrieval.mode`. Default: `false`.
 *
 *                     Disabling does NOT delete existing
 *                     embeddings -- they remain in
 *                     `memory_embeddings` and
 *                     `memory_vectors`. Re-enabling restores
 *                     the previous state immediately.
 *
 *   * `model_key`  -- composite key `"<provider_id>::<model_id>"`
 *                     (e.g. `"openai::text-embedding-3-small"`).
 *                     MUST match a `key` in the
 *                     `GET /api/config/embedding-models`
 *                     response -- the backend validates this on
 *                     every reindex request and rejects with
 *                     `model_key_not_in_snapshot` otherwise.
 *
 *                     `null` is a legitimate intermediate state:
 *                     `enabled=true && model_key=null` means
 *                     "semantic search is requested but not yet
 *                     wired up". The UI shows a prominent
 *                     warning and the worker refuses to process
 *                     jobs until a model is selected.
 *
 * Wire invariants (enforced by Pydantic on the backend):
 *
 *   * `model_key` is either `null` or a non-empty string
 *     containing `::`.
 *   * A bare model id (no `::` prefix) is rejected.
 *
 * Model-change semantics (informs the reindex UX):
 *
 *   * Changing `model_key` does NOT delete existing
 *     embeddings -- they remain in `memory_embeddings` keyed
 *     by the OLD `model_id`. This preserves an audit trail.
 *   * `memory_vectors` (the sqlite-vec virtual table) IS
 *     rebuilt on the next reindex -- vectors from two different
 *     models cannot coexist in one vec0 table.
 *   * A reindex is therefore REQUIRED after any `model_key`
 *     change. The UI MUST surface this (Section D's warning).
 */
export interface MemoryEmbeddingSettings {
  /** Master switch for semantic search. Default: `false`. */
  enabled: boolean;
  /** Composite `provider::model` key, or `null` when unselected. */
  model_key: string | null;
}

/**
 * Safe defaults for `MemoryEmbeddingSettings`.
 *
 * Matches the backend's default so the settings form renders
 * identically on a fresh install without a "configured but not
 * actually configured" mismatch.
 */
export const DEFAULT_MEMORY_EMBEDDING_SETTINGS: MemoryEmbeddingSettings = {
  enabled: false,
  model_key: null,
};

// ---------------------------------------------------------------------------
// Retrieval settings sub-tree
// ---------------------------------------------------------------------------

/**
 * Retrieval strategy and fusion tuning.
 *
 * Mirrors `peldrun.memory.domain.settings.MemoryRetrievalSettings`.
 *
 * Every default below matches the backend's Pydantic default --
 * sending the object as-is is a no-op patch, which keeps the
 * settings form's "nothing changed" short-circuit correct.
 *
 * Field semantics:
 *
 *   * `mode`                  -- see `RetrievalMode`.
 *                                Default: `"lexical"`. A
 *                                semantic mode while
 *                                `embedding.enabled=false` is
 *                                allowed at the type level; the
 *                                backend falls back silently.
 *
 *   * `vector_alpha`          -- [0.0, 1.0] hybrid fusion weight.
 *                                `0.0` = lexical-dominant,
 *                                `1.0` = vector-dominant,
 *                                `0.5` = balanced.
 *
 *                                IMPORTANT: `0.5` is a special
 *                                value -- it selects RRF
 *                                (reciprocal rank fusion), which
 *                                is scale-free and ignores raw
 *                                scores. Every OTHER value
 *                                selects weighted fusion, which
 *                                uses `1 - vector_alpha` as the
 *                                lexical weight. Do NOT surface
 *                                this distinction in the UI --
 *                                it is a backend implementation
 *                                detail. The UI presents
 *                                `vector_alpha` as a single
 *                                "keyword vs semantic balance"
 *                                slider with the midpoint
 *                                emphasized.
 *
 *   * `min_lexical_score`     -- [0.0, 1.0] post-rank filter on
 *                                normalized BM25 scores. Lower
 *                                values admit more noise; higher
 *                                values admit only strong
 *                                matches. Default `0.2` accepts
 *                                moderately-strong matches,
 *                                which is what users expect from
 *                                a "similar memories" preview.
 *
 *   * `min_vector_similarity` -- [0.0, 1.0] post-rank filter on
 *                                cosine similarity. Default `0.5`
 *                                is the conventional cutoff for
 *                                "related but not identical"
 *                                in embedding space. Raising
 *                                this to ~0.75 is the standard
 *                                tactic for high-precision use
 *                                cases (e.g. duplicate
 *                                detection).
 *
 *   * `rrf_k`                 -- [1, 100] RRF smoothing. Only
 *                                consulted when
 *                                `vector_alpha === 0.5`. Higher
 *                                values flatten the fusion
 *                                (consensus-driven); lower
 *                                values sharpen it (top-result-
 *                                driven). Default `60` is the
 *                                value from the original RRF
 *                                paper and balances both
 *                                extremes well.
 *
 *   * `rerank_enabled`        -- Phase 5A. When `true` AND an
 *                                extraction model is configured
 *                                in `settings.ai.extraction`, the
 *                                backend's
 *                                `CrossEncoderReranker.rerank`
 *                                re-orders fused top-K candidates
 *                                by LLM-scored query relevance.
 *                                Default `false` preserves Phase 4
 *                                behavior exactly (no LLM call, no
 *                                added latency, `search_mode`
 *                                unchanged). Any reranker failure
 *                                (timeout, provider down, parse
 *                                error) silently degrades to RRF
 *                                ordering -- the search endpoint
 *                                NEVER returns an error because of
 *                                the reranker.
 *
 *   * `rerank_max_candidates` -- [1, 100] upper bound on candidates
 *                                sent to the reranker LLM per
 *                                search. Candidates beyond this cap
 *                                retain their RRF position and are
 *                                appended AFTER the reranked set.
 *                                Default `20` balances latency
 *                                against recall.
 *
 *   * `rerank_timeout_seconds`-- [1.0, 30.0] per-batch wall-clock
 *                                timeout for one reranker LLM
 *                                call, applied via
 *                                `asyncio.wait_for`. A timeout
 *                                degrades to RRF ordering. Default
 *                                `3.0` is healthy latency +
 *                                headroom; values above ~10s are
 *                                discouraged because the user-
 *                                facing search request is
 *                                blocked for that duration.
 *
 *   * `rerank_batch_size`     -- [1, 20] candidates per reranker
 *                                LLM call. Smaller batches parse
 *                                more reliably. The reference
 *                                spec (§6.3) suggests setting
 *                                equal to `rerank_max_candidates`
 *                                for single-call behavior, which
 *                                is why the default `20` matches
 *                                `rerank_max_candidates`'s default.
 */
export interface MemoryRetrievalSettings {
  /** See `RetrievalMode`. Default: `"lexical"`. */
  mode: RetrievalMode;
  /** Hybrid fusion weight [0, 1]. Default: `0.5`. */
  vector_alpha: number;
  /** Normalized BM25 score floor [0, 1]. Default: `0.2`. */
  min_lexical_score: number;
  /** Cosine similarity floor [0, 1]. Default: `0.5`. */
  min_vector_similarity: number;
  /** RRF smoothing parameter [1, 100]. Default: `60`. */
  rrf_k: number;
  /** Phase 5A LLM reranker master switch. Default: `false`. */
  rerank_enabled: boolean;
  /** Reranker LLM candidate cap per search [1, 100]. Default: `20`. */
  rerank_max_candidates: number;
  /** Per-batch reranker LLM timeout [1.0, 30.0] seconds. Default: `3.0`. */
  rerank_timeout_seconds: number;
  /** Reranker LLM candidates per call [1, 20]. Default: `20`. */
  rerank_batch_size: number;
}

/**
 * Safe defaults for `MemoryRetrievalSettings`.
 *
 * Mirrors the backend's defaults byte-for-byte so the settings
 * form's dirty-check (`isNoOpPatch`) correctly reports "no
 * changes" on a fresh page load.
 */
export const DEFAULT_MEMORY_RETRIEVAL_SETTINGS: MemoryRetrievalSettings = {
  mode: 'lexical',
  vector_alpha: 0.5,
  min_lexical_score: 0.2,
  min_vector_similarity: 0.5,
  rrf_k: 60,
  rerank_enabled: false,
  // Phase 5A reranker defaults -- mirror the backend's Pydantic
  // Field(...) defaults so a fresh settings form renders
  // identically on both sides.
  rerank_max_candidates: 20,
  rerank_timeout_seconds: 3.0,
  rerank_batch_size: 20,
};

// ---------------------------------------------------------------------------
// Embedding model catalog
// ---------------------------------------------------------------------------

/**
 * How a model entry was discovered.
 *
 *   * `manual`        -- the user explicitly tagged this model
 *                        as an embedding model via the "Model
 *                        Info" modal on the Models settings
 *                        page. Highest trust level; always
 *                        selectable.
 *
 *   * `provider_live` -- auto-detected by probing the provider's
 *                        `/v1/models` (or equivalent) endpoint
 *                        at scan time. The provider currently
 *                        advertises this model. Trustworthy
 *                        but may lag if the user removed the
 *                        model on the provider's side without
 *                        re-scanning.
 *
 *   * `unknown`       -- the backend's `_read_models_vault`
 *                        helper read the entry but could not
 *                        classify its source (e.g. a legacy
 *                        entry written before the `source`
 *                        field existed). The UI should render
 *                        these with a subtle "unknown source"
 *                        badge rather than hiding them.
 *
 * The settings UI displays `[Manual]` / `[Auto]` badges for the
 * first two sources and does NOT distinguish `unknown` visually
 * -- the absence of a badge already conveys "default entry".
 */
export type EmbeddingModelSource =
  | 'manual'
  | 'provider_live'
  | 'unknown';

/**
 * One selectable embedding model in the config catalog.
 *
 * Returned as an element of `EmbeddingModelsResponse.models`.
 * Consumers should treat every field as read-only -- the
 * settings page only uses `key` (for storage) and `display_name`
 * + `provider_name` (for rendering).
 *
 * Nullability notes -- IMPORTANT
 * ------------------------------
 *
 * `provider_id` and `provider_name` may BOTH be `null` because
 * of a known bug in `config_rtr.py`'s `_read_models_vault`
 * (it reads from `STORAGE_ROOT` while the current deployment
 * stores the vault at `backend/data/models_vault.json`). Until
 * that bug is fixed server-side, the frontend MUST:
 *
 *   1. Render such models without crashing (no non-null
 *      assertion on `provider_name`).
 *   2. Group them under an "Unknown provider" heading in the
 *      dropdown.
 *   3. NOT attempt to filter them out -- they may be the only
 *      models available on some deployments.
 *
 * The `display_name` field is always present and always
 * human-readable -- use it as the primary label and derive the
 * secondary label (provider) defensively.
 */
export interface EmbeddingModel {
  /**
   * The composite key stored in `embedding.model_key`.
   * Format: `"<provider_id_or_unknown>::<model_id>"`. The exact
   * string is what the backend re-validates on reindex, so the
   * client MUST round-trip it verbatim -- never split or
   * reconstruct it.
   */
  key: string;

  /** The model identifier understood by the provider. */
  model_id: string;

  /** Provider id (e.g. `"openai"`, `"lmstudio"`), or `null`. */
  provider_id: string | null;

  /** Human-readable provider label (e.g. `"OpenAI"`), or `null`. */
  provider_name: string | null;

  /** Primary label for the dropdown. Always non-empty. */
  display_name: string;

  /**
   * Model type string from the provider's catalog. Always
   * `"embeddings"` for entries in this response -- the field
   * exists so the type system mirrors the wire shape, not
   * because the UI needs to branch on it.
   */
  type: string;

  /** How the entry was discovered -- see `EmbeddingModelSource`. */
  source: EmbeddingModelSource;
}

/**
 * Convenience map from provider id to the list of model ids that
 * provider exposes.
 *
 * Mirrors the `by_provider` field of
 * `EmbeddingModelsResponse`. Keys are provider ids (or the
 * literal `"unknown"` when `provider_id` was `null` on every
 * entry -- the backend synthesizes this key). Values are arrays
 * of model ids (NOT full `EmbeddingModel` objects -- resolve
 * them via `models`).
 *
 * Kept as a separate type so consuming code that only needs
 * "which providers are available?" does not have to walk the
 * full `models` array.
 */
export type EmbeddingModelsByProvider = Record<string, string[]>;

// ---------------------------------------------------------------------------
// Embedding stats (read-only coverage snapshot)
// ---------------------------------------------------------------------------

/**
 * Coverage snapshot returned by `GET /api/memory/embedding-stats`.
 *
 * Powers the reindex panel's "12 of 15 memories embedded"
 * summary and the diagnostics panel's vector-health section.
 *
 * Every field is read-only; there is no PATCH endpoint for
 * stats (unlike settings, which is writable).
 *
 * Field-by-field (all nullable fields are `null` when the
 * corresponding subsystem has never been active):
 *
 *   * `model_key`         -- the CURRENTLY configured composite
 *                            key, or `null` when unset.
 *
 *   * `model_id`          -- the parsed model id (from
 *                            `model_key`), or `null`.
 *
 *   * `total_memories`    -- count of ACTIVE memories (excludes
 *                            archived / superseded). This is the
 *                            denominator for coverage: "12 of N".
 *
 *   * `with_embeddings`   -- count of memories that have at
 *                            least one embedding under the
 *                            CURRENT `model_key`. Memories
 *                            embedded under a PREVIOUS model are
 *                            NOT counted here (they are counted
 *                            under the old model's row and are
 *                            invisible to this snapshot).
 *
 *   * `pending_reindex`   -- count of jobs in
 *                            `memory_embedding_jobs` with status
 *                            `pending` or `running`. When > 0,
 *                            the reindex panel shows a progress
 *                            spinner and polls
 *                            `GET /api/memory/automation/jobs`
 *                            every 2 seconds.
 *
 *   * `last_reindex_at`   -- Unix epoch (seconds) of the last
 *                            `POST /api/memory/reindex` call
 *                            that enqueued at least one job, or
 *                            `null` if no reindex has ever run.
 *
 *   * `vec0_dimension`    -- the dimensionality of the sqlite-vec
 *                            table under the CURRENT model, or
 *                            `null` if the table has not been
 *                            created yet. A mismatch against the
 *                            model's expected dimension is a
 *                            server-side bug -- the UI should
 *                            NOT attempt to correct it.
 *
 *   * `vec0_row_count`    -- raw row count in the sqlite-vec
 *                            table. EXPECTED to equal
 *                            `with_embeddings` on a healthy
 *                            system; a persistent mismatch
 *                            usually means a reindex was
 *                            interrupted mid-transaction. The
 *                            diagnostics panel surfaces this as
 *                            a warning when the two disagree.
 */
export interface EmbeddingStats {
  model_key: string | null;
  model_id: string | null;
  total_memories: number;
  with_embeddings: number;
  pending_reindex: number;
  last_reindex_at: number | null;
  vec0_dimension: number | null;
  vec0_row_count: number;
}

// ---------------------------------------------------------------------------
// Reindex request body
// ---------------------------------------------------------------------------

/**
 * Body accepted by `POST /api/memory/reindex`.
 *
 * Both fields are optional -- sending `{}` (an empty body)
 * reindexes EVERY active memory under the currently selected
 * model. This is the common case from the settings page's
 * "Rebuild Embedding Index" button.
 *
 * Field semantics:
 *
 *   * `memory_ids`     -- when supplied, restrict the reindex
 *                        to the listed ids. `null` and omitted
 *                        are equivalent (reindex all).
 *                        Constraint: at most 1000 ids per
 *                        request (enforced by the backend;
 *                        exceeding returns HTTP 422).
 *                        Constraint: each id must be a
 *                        non-empty string; an empty string or a
 *                        number returns HTTP 422.
 *
 *   * `content_version`-- the schema version of the embedded
 *                        content. The backend uses this to
 *                        reject mismatched rows at retrieval
 *                        time without a full reindex. Default
 *                        `1` matches the current pipeline.
 *                        Clients should NOT override this --
 *                        the field exists for forward
 *                        compatibility, not user control.
 *
 * IMPORTANT: the reindex endpoint is IDEMPOTENT for jobs that
 * are already pending or running under the same
 * `(memory_id, model_key, content_version)` tuple. Sending the
 * same request twice does NOT create duplicate jobs -- the
 * second call returns the SAME counts with `enqueued` reduced
 * by however many were already pending, and `skipped` increased
 * by the same amount.
 */
export interface ReindexRequest {
  memory_ids?: string[] | null;
  content_version?: number;
}

// ---------------------------------------------------------------------------
// Numeric bounds
// ---------------------------------------------------------------------------

/**
 * Numeric bounds for the embedding settings subtree.
 *
 * Currently empty -- `MemoryEmbeddingSettings` has no numeric
 * fields (both fields are `boolean | string | null`). The
 * constant exists so the validators module can import it
 * uniformly with the other bounds objects, and so a future
 * numeric field (e.g. a rate limit) lands in a well-known
 * place.
 */
export const MemoryEmbeddingBounds = {} as const;

/**
 * Numeric bounds for the retrieval settings subtree.
 *
 * Consumed by `lib/validators.ts` to build Zod schemas. Every
 * range here MUST match the corresponding Pydantic
 * `Field(ge=..., le=...)` on the backend. A mismatch means the
 * client accepts a value the server rejects (or vice versa).
 *
 * `rerank_enabled` and `mode` are not represented here -- they
 * are categorical (`boolean` and `RetrievalMode` respectively),
 * and are validated by their type rather than by a range.
 */
export const MemoryRetrievalBounds = {
  vector_alpha: { min: 0.0, max: 1.0 },
  min_lexical_score: { min: 0.0, max: 1.0 },
  min_vector_similarity: { min: 0.0, max: 1.0 },
  rrf_k: { min: 1, max: 100 },
  // Phase 5A reranker bounds -- must match the backend's
  // Pydantic `Field(ge=..., le=...)` declarations exactly.
  rerank_max_candidates: { min: 1, max: 100 },
  rerank_timeout_seconds: { min: 1.0, max: 30.0 },
  rerank_batch_size: { min: 1, max: 20 },
} as const;

// ---------------------------------------------------------------------------
// Helper predicates
// ---------------------------------------------------------------------------

/**
 * `true` iff `mode` is a semantic mode that requires embeddings
 * to be useful.
 *
 * Used by the settings page to:
 *   1. Render the "vector / hybrid / automatic need embeddings"
 *      warning next to those modes.
 *   2. Decide whether to show a reindex button in the Reindex
 *      panel (a purely lexical deployment does not need one).
 *
 * `lexical` is deliberately NOT considered semantic -- a user
 * who leaves `mode` at `lexical` but enables embeddings is
 * pre-configuring, not requesting semantic search.
 */
export function isSemanticMode(mode: RetrievalMode): boolean {
  return mode === 'vector' || mode === 'hybrid' || mode === 'automatic';
}

/**
 * `true` iff the embedding subsystem is fully wired up and
 * ready to process reindex requests.
 *
 * The condition is `enabled === true` AND `model_key !== null`.
 * This mirrors the backend's own readiness gate for the
 * `POST /api/memory/reindex` endpoint, which returns HTTP 503
 * with a specific error code when EITHER condition fails:
 *
 *   * `embedding_not_enabled`   -- `enabled === false`.
 *   * `model_key_not_set`       -- `enabled === true` but
 *                                  `model_key === null`.
 *
 * The UI uses this predicate to decide whether the "Rebuild
 * Embedding Index" button is enabled. It does NOT replace
 * server-side validation -- the 503 cases are still handled
 * (see the error mapping in `ReindexErrorResponse`), because a
 * race (settings changed between page load and button click)
 * is always possible.
 */
export function isEmbeddingConfigured(
  embedding: MemoryEmbeddingSettings,
): boolean {
  return embedding.enabled === true && embedding.model_key !== null;
}

/**
 * `true` iff `key` is a syntactically valid composite
 * `provider::model` key.
 *
 * The check is deliberately loose (non-empty string containing
 * `::`). The authoritative validation lives on the backend,
 * which matches the key against the live snapshot from
 * `GET /api/config/embedding-models`. This helper exists only
 * to catch obvious client-side mistakes (e.g. a bare model id
 * slipped through a copy-paste) before hitting the network.
 */
export function isValidModelKey(key: string | null): key is string {
  return typeof key === 'string' && key.length > 0 && key.includes('::');
}
