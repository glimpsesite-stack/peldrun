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
 * Currently exposes only `getMemoryModels()` -- the single
 * endpoint the memory settings page needs. Future additions
 * (e.g. a `getModelBudgets()` for the AI cost ceiling) belong
 * here, not in `memoryClient`.
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
};
