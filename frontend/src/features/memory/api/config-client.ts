// src/features/memory/api/config-client.ts

/**
 * HTTP client for the `/api/config/*` endpoints.
 *
 * Why a SEPARATE client from `memoryClient`
 * -----------------------------------------
 * `memoryClient` (in `client.ts`) is scoped to `/api/memory/*`:
 * its module docstring explicitly says so, and every method
 * prefixes `MEMORY_API_BASE`. Adding `/api/config/*` methods to
 * it would violate that boundary and conflate two backend
 * subsystems (memory vs. config) in one transport layer.
 *
 * The memory settings page needs exactly ONE config endpoint:
 * `/api/config/memory-models` -- the list of models the user has
 * marked as memory models in Settings -> Models. Keeping the
 * transport here makes the boundary explicit and lets future
 * config endpoints land in this file without touching the memory
 * client.
 *
 * Error handling
 * --------------
 * Unlike `memoryFetch`, this client does NOT throw `MemoryApiError`
 * on failure. `/api/config/*` is an existing surface used by the
 * model-settings UI; it returns `{ok: false, error: ...}` on
 * failure rather than HTTP errors. The only exceptions we raise
 * are network-level (fetch throws). The caller (React Query)
 * surfaces them as `error` state, and the UI renders a banner.
 */

// ---------------------------------------------------------------------------
// Base URL
// ---------------------------------------------------------------------------

/**
 * Base URL for `/api/config/*`. Same-origin in the standard
 * Next.js setup. No env override is exposed today -- the config
 * router is always mounted at `/api/config` by `main.py`.
 */
const CONFIG_API_BASE = '/api/config';

// ---------------------------------------------------------------------------
// Domain type imports (Phase 4)
// ---------------------------------------------------------------------------
//
// The embedding catalog response mirrors `EmbeddingModel` from
// `../types/embedding.ts` one-to-one (same field names, same
// nullability). Unlike `MemoryModelOption` -- which SPLITS the
// composite key into `providerId` + `modelId` because the AI
// settings store them as separate fields -- embedding settings
// store the composite `key` VERBATIM in `embedding.model_key`,
// so no split is needed. Reusing `EmbeddingModel` directly
// avoids a redundant parallel type.
import type {
  EmbeddingModel,
  EmbeddingModelsByProvider,
} from '../types/embedding';

// ---------------------------------------------------------------------------
// Response shape
// ---------------------------------------------------------------------------

/**
 * The wire shape of `GET /api/config/memory-models`.
 *
 * `models` is the raw list of metadata keys, in the exact form
 * `models_metadata.json` uses. It may be empty (no model marked
 * as memory) or contain composite keys
 * (`"openai::gpt-4o-mini"`, `"provider::connection::model"`).
 */
/**
 * The wire shape of `GET /api/config/memory-models`.
 *
 * Each model is an OBJECT (not a bare string) so the client can
 * populate `MemoryModelOption` without a second fetch.
 */
interface MemoryModelsResponse {
  ok: boolean;
  models: Array<{
    key: string;
    provider_id: string | null;
    model_id: string;
    is_embedding: boolean;
    context_window: number | null;
    max_output_tokens: number | null;
  }>;
}

/**
 * The wire shape of `GET /api/config/embedding-models`.
 *
 * Mirrors the backend's response from `config_rtr.py`'s
 * `get_embedding_models` endpoint. The `models` array uses
 * `EmbeddingModel` VERBATIM (snake_case fields matching the
 * JSON payload) because no transformation is needed -- the
 * embedding settings store the composite `key` directly, so
 * there is nothing to split.
 *
 * `by_provider` is a convenience grouping the backend
 * provides (`{ "openai": ["text-embedding-3-small", ...] }`).
 * The Phase 4 `EmbeddingModelSelector` does NOT read it --
 * it groups by `provider_name` from each `models` entry, which
 * gives it the human-readable provider label ("OpenAI") rather
 * than just the id. `by_provider` is preserved in the type for
 * fidelity to the wire; future consumers may use it.
 *
 * `ok` is BOOLEAN (not literal `true`) because `/api/config/*`
 * returns `{ok: false, error, message}` on failure with HTTP
 * 200. `configFetch` only throws on HTTP errors, so a 200
 * with `ok: false` reaches here -- `getEmbeddingModels` treats
 * that as an empty catalog (see the method's JSDoc).
 *
 * KNOWN BACKEND BUG (documented for the UI):
 * ---------------------------------------------------------
 * `provider_id` and `provider_name` may BOTH be `null` on some
 * entries due to an old bug in `_read_models_vault` (it reads
 * from `STORAGE_ROOT` while some deployments store the vault
 * at `backend/data/`). The Phase 4 selector MUST:
 *
 *   1. Render such models without crashing (no non-null
 *      assertion on `provider_name`).
 *   2. Group them under an "Unknown provider" heading.
 *   3. NOT filter them out -- they may be the only models
 *      available.
 */
interface EmbeddingModelsResponse {
  ok: boolean;
  models: EmbeddingModel[];
  by_provider: EmbeddingModelsByProvider;
}

/**
 * A parsed model option, ready for the dropdown.
 *
 * `key` is the raw metadata key (used as the `<option value>`).
 * `providerId` and `modelId` are the split parts -- the memory
 * settings page stores them as SEPARATE fields
 * (`ai.extraction.provider_id` + `ai.extraction.model_id`), so
 * the split has to happen here, at the transport boundary, not
 * at every call site.
 *
 * `providerId` is `null` for plain keys (`"gpt-4o-mini"`), which
 * means "no provider prefix was recorded when the user marked
 * this model". In that case `modelId` still carries the model
 * name; the backend's gateway will resolve the provider from
 * the vault by trying each configured provider.
 */
export interface MemoryModelOption {
  key: string;
  providerId: string | null;
  modelId: string;
  /**
   * `true` when the metadata entry carries `is_embedding: true`.
   * Extraction and consolidation MUST NOT use embedding models
   * -- they can only serve `/chat/completions`.
   */
  isEmbedding: boolean;
  /**
   * Model's context window in tokens, resolved from
   * `models_budget.json` (authoritative) with a fallback to
   * `models_metadata.json` (`max_context_length`). `null` when
   * neither source has a value -- the UI then leaves the input
   * fields untouched rather than pre-filling with a guess.
   */
  contextWindow: number | null;
  /**
   * Model's hard cap on completion tokens, resolved the same
   * way. Used to pre-fill `MemoryTaskModel.max_output_tokens`
   * on selection. `null` when unknown.
   */
  maxOutputTokens: number | null;
}

// ---------------------------------------------------------------------------
// Fetch wrapper
// ---------------------------------------------------------------------------

async function configFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json');
  }

  const response = await fetch(`${CONFIG_API_BASE}${path}`, {
    ...options,
    headers,
  });

  // AbortError must propagate verbatim so React Query treats
  // the cancellation as a cancellation, not as an error.
  // (fetch throws the AbortError itself before this point; this
  // check catches the case where a caller passes a pre-aborted
  // signal -- defensive only.)
  if (response.status === 499) {
    throw new DOMException('Aborted', 'AbortError');
  }

  if (!response.ok) {
    throw new Error(
      `Config API ${path} returned HTTP ${response.status}.`,
    );
  }

  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Public client
// ---------------------------------------------------------------------------

/**
 * The typed config API client.
 *
 * Exposes two methods today:
 *
 *   * `getMemoryModels()`    -- Phase 3, chat models for the
 *                                extraction / consolidation
 *                                tasks. Filtered to exclude
 *                                embedding models.
 *   * `getEmbeddingModels()` -- Phase 4, embedding models for
 *                                semantic search. The full
 *                                catalog, including provider
 *                                information so the selector can
 *                                group by provider.
 *
 * Future config endpoints (e.g. a `getModelBudgets()` for the
 * AI cost ceiling) belong here, not in `memoryClient` -- the
 * memory client is scoped to `/api/memory/*` and must not
 * grow config endpoints.
 */
export const configClient = {
  /**
   * `GET /api/config/memory-models`.
   *
   * Returns the models the user has marked as memory models in
   * Settings -> Models, parsed into `MemoryModelOption[]`.
   *
   * Empty list is a VALID response, meaning "the user has not
   * marked any model". The UI must render a "no memory models"
   * warning with a link to Settings -> Models in that case --
   * NOT a broken dropdown. This is the design contract the
   * backend endpoint documents.
   */
  async getMemoryModels(
    signal?: AbortSignal,
  ): Promise<MemoryModelOption[]> {
    const data = await configFetch<MemoryModelsResponse>(
      '/memory-models',
      { signal },
    );

    const raw = Array.isArray(data.models) ? data.models : [];
    return raw.map((m) => ({
      key: m.key,
      providerId: m.provider_id,
      modelId: m.model_id,
      isEmbedding: Boolean(m.is_embedding),
      contextWindow:
        typeof m.context_window === 'number' ? m.context_window : null,
      maxOutputTokens:
        typeof m.max_output_tokens === 'number'
          ? m.max_output_tokens
          : null,
    }));
  },

  /**
   * `GET /api/config/embedding-models`.
   *
   * Returns the models the user has marked as embedding models
   * (either explicitly in Settings -> Models, or auto-detected
   * from the provider's `/v1/models` scan), parsed into
   * `EmbeddingModel[]`.
   *
   * The `models` array is returned VERBATIM (no split, no
   * camelCase normalization). The composite `key` is stored in
   * `MemorySettings.embedding.model_key` on selection, and
   * every field is already the shape the Phase 4 selector
   * needs. Unlike `getMemoryModels`, no transformation is
   * warranted -- there is no `provider_id` + `model_id` split
   * to do.
   *
   * Response contract:
   *
   *   * `{ok: true, models: [...], by_provider: {...}}` -- the
   *     normal case. `models` is an array (possibly empty).
   *
   *   * `{ok: false, error: '...', message: '...'}` -- the
   *     backend reports a catalog-scan failure. `models` is
   *     absent. We return `[]` (the caller's empty state
   *     triggers a "no models available" banner with a link
   *     to Settings -> Models).
   *
   *   * HTTP 4xx/5xx -- `configFetch` throws a plain `Error`.
   *     React Query surfaces it as `error` state; the
   *     selector renders an error banner with a retry link.
   *
   * The `by_provider` field is NOT returned to the caller
   * because the Phase 4 selector groups client-side by
   * `provider_name` (which gives it the human-readable label
   * "OpenAI" instead of just the id "openai"). If a future
   * consumer needs `by_provider`, this method can grow a
   * sibling that returns the full response -- do NOT change
   * this method's return type, since callers already assume
   * an array (matching `getMemoryModels`).
   */
  async getEmbeddingModels(
    signal?: AbortSignal,
  ): Promise<EmbeddingModel[]> {
    const data = await configFetch<EmbeddingModelsResponse>(
      '/embedding-models',
      { signal },
    );

    // Defensive `Array.isArray` -- matches `getMemoryModels`.
    // Handles both the `{ok: false}` case (where `models` is
    // absent) and the theoretically-malformed case (where the
    // backend sends a non-array). The UI's empty state handles
    // both.
    const raw = Array.isArray(data.models) ? data.models : [];
    return raw;
  },
};
