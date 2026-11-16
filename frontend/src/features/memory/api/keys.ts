// src/features/memory/api/keys.ts

/**
 * TanStack Query key factory for the PELDRUN Memory feature.
 *
 * Why a factory, not inline arrays
 * --------------------------------
 *
 * Raw key arrays (`['memory', 'entries', 'list', ...]`) scattered
 * across queries.ts and mutations.ts create three classes of bugs:
 *
 *   1. **Typos.** `['memory', 'entry', 'list']` (missing 's')
 *      silently fails to invalidate -- the query keeps serving
 *      stale data with no error anywhere.
 *
 *   2. **Shape drift.** One call site caches with
 *      `{limit, offset}` and another invalidates with
 *      `{offset, limit}` -- same intent, different hashes,
 *      cache miss. A factory centralizes the shape.
 *
 *   3. **Over/under-invalidation.** Invalidating
 *      `['memory', 'entries']` (too broad) nukes unrelated
 *      detail queries; invalidating
 *      `['memory', 'entries', 'list', {specific params}]`
 *      (too narrow) misses sibling pages.
 *
 * The factory pattern solves all three: one declaration, one
 * shape, hierarchical prefixes for partial invalidation.
 *
 * Cache-key hierarchy
 * -------------------
 *
 * Every key returned here starts with `'memory'` and follows the
 * shape:
 *
 *     ['memory', <namespace>, <subnamespace?>..., <args?>...]
 *
 * The hierarchy is DESIGNED for TanStack Query's prefix matching.
 * `queryClient.invalidateQueries({ queryKey: prefix })` matches
 * every cached key that STARTS WITH `prefix`. So:
 *
 *     memoryKeys.all                    // ['memory']
 *         invalidates EVERYTHING under memory
 *     memoryKeys.entries.all()          // ['memory', 'entries']
 *         invalidates both list and detail queries
 *     memoryKeys.entries.lists()        // ['memory', 'entries', 'list']
 *         invalidates only list queries (any params)
 *     memoryKeys.entries.list(params)   // ['memory', 'entries', 'list', params]
 *         invalidates exactly ONE cached list
 *
 * Invalidation policy (from the spec, section 4.4)
 * ------------------------------------------------
 *
 *   Mutation                 | Invalidate
 *   -------------------------|----------------------------------------
 *   PATCH settings           | settings.global(), settings.project(*),
 *                            |   usage.all() (budget changed)
 *   PUT project override     | settings.project(id), usage.all()
 *   DELETE project override  | settings.project(id), usage.all()
 *   POST create              | entries.lists(), projects.list(),
 *                            |   projects.stats(<scope_id>)
 *   PATCH update             | entries.lists(), entries.detail(id, scope)
 *   DELETE                   | entries.lists(), entries.detail(id, scope),
 *                            |   projects.stats(<scope_id>)
 *   POST pin                 | entries.lists(), entries.detail(id, scope)
 *                            |   (optimistic; see mutations.ts)
 *   POST promote             | entries.lists() (BOTH source and target
 *                            |   scopes -- one prefix, two invalidations
 *                            |   if scopes differ)
 *   POST bulk-archive        | entries.lists(), projects.list(),
 *                            |   projects.stats(id)
 *   POST bulk-purge          | entries.lists(), entries.details(),
 *                            |   projects.list(), projects.stats(id)
 *   POST import apply        | entries.lists(), projects.list()
 *
 * mutations.ts implements exactly this table; deviations should
 * be documented in the specific mutation's hook.
 *
 * Rules for consumers
 * -------------------
 *
 *   * **Never write a raw key array** outside this file. If you
 *     find yourself typing `['memory', ...]` in a `.ts` file
 *     other than this one, stop -- add a builder here instead.
 *     The spec's acceptance criterion for F1.2 is literally
 *     "grep does not find duplicate `['memory'`", enforced by
 *     this rule.
 *
 *   * **Pass params objects, not positional args.** Future
 *     fields (e.g. a new filter) extend the params interface
 *     without touching every call site.
 *
 *   * **Use `null` (not `undefined`) for "no value".** TanStack
 *     Query hashes via JSON.stringify; `{a: undefined}` and
 *     `{}` hash the same, but `{a: null}` and `{}` do NOT. The
 *     params interfaces below require `| null`, and the
 *     normalizers coerce `undefined` to `null` defensively, so
 *     cache misses from missing-vs-null confusion are
 *     impossible.
 */

import type {
  MemoryStatusFilter,
  MemoryType,
  UsageBreakdownDimension,
  UsageGranularity,
  UsagePeriod,
} from '../types';

// Phase 3 additions -- same rationale as the response imports in
// `client.ts`: grouped so the diff between Phase-1 and Phase-3
// surface is obvious.
import type { MemoryBatchStatus } from '../types/batch';
import type {
  ExtractionTrigger,
  MemoryJobStatus,
} from '../types/jobs';

// ---------------------------------------------------------------------------
// Parameter types
// ---------------------------------------------------------------------------
//
// Every query that takes parameters declares a named interface
// here, so call sites cannot drift. The interfaces enforce that
// "no value" is spelled `null`, never omitted -- see the
// cache-hashing note in the module JSDoc.

/**
 * Parameters for the entry-list query.
 *
 * All non-optional fields MUST be supplied on every call, even
 * when the user has not set a filter. Use `null` for "no
 * filter". This eliminates the `undefined`-vs-missing hash
 * divergence that would otherwise cause stale reads when a
 * filter is cleared.
 */
export interface EntryListParams {
  /**
   * The single scope to list, in wire form (`"<type>:<id>"`).
   * E.g. `"project:acme"`, `"user:default"`.
   *
   * Note: the list endpoint requires an EXPLICIT single scope;
   * multi-scope retrieval is only available via `/search`.
   */
  scope: string;

  /** Filter by lifecycle status. Matches the `status` query param. */
  status: MemoryStatusFilter;

  /** Filter by memory type, or `null` for all types. */
  memory_type: MemoryType | null;

  /** Filter by pin state, or `null` for both. */
  is_pinned: boolean | null;

  /** Page size. The backend caps this at 200; the UI uses 50. */
  limit: number;

  /** Pagination offset. The "load more" button increments this. */
  offset: number;
}

/**
 * Parameters for the usage summary query.
 *
 * `chat_id` and `project_id` are OPTIONAL filters on the wire
 * (the backend accepts their absence as "no filter"). Here they
 * are typed `string | null` to preserve the null-vs-undefined
 * hashing rule -- pass `null` when you do not want to filter.
 */
export interface UsageSummaryParams {
  period: UsagePeriod;
  chat_id: string | null;
  project_id: string | null;
}

/**
 * Parameters for the usage breakdown query.
 *
 * `by` is the dimension to slice tokens by. `source` is an
 * accepted backend alias for `scope`; the client SHOULD use the
 * canonical form (`scope`) when querying, and the response will
 * echo the normalized value.
 */
export interface UsageBreakdownParams {
  by: UsageBreakdownDimension;
  period: UsagePeriod;
  chat_id: string | null;
  project_id: string | null;
}

/**
 * Parameters for the usage timeseries query.
 *
 * `period` and `granularity` are NOT cross-validated by the
 * backend -- `{period: 'day', granularity: 'month'}` returns a
 * single-point series. Valid, if unusual.
 */
export interface UsageTimeseriesParams {
  period: UsagePeriod;
  granularity: UsageGranularity;
}

// ---------------------------------------------------------------------------
// Phase 3 (Automation) parameter types
// ---------------------------------------------------------------------------
//
// Same null-vs-undefined hashing rule as the Phase-1 params: every
// filter field is `T | null` (never optional) so that clearing a
// filter and omitting it produce byte-identical cache keys. The
// normalizers below enforce the same invariant.

/**
 * Parameters for the jobs list query.
 *
 * Mirrors `GET /api/memory/automation/jobs`'s query parameters,
 * plus the pagination fields the query key factory hashes.
 *
 * `status` / `trigger_type` / `chat_id` are ALL filter slots --
 * `null` means "no filter on this field", not "match null".
 */
export interface JobsListParams {
  status: MemoryJobStatus | null;
  chat_id: string | null;
  trigger_type: ExtractionTrigger | null;
  limit: number;
  offset: number;
}

/**
 * Parameters for the batches list query.
 *
 * `status: null` means "all batches, any status" (the UI's
 * default view). A non-null value restricts to that status.
 */
export interface BatchesListParams {
  status: MemoryBatchStatus | null;
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// Normalizers
// ---------------------------------------------------------------------------
//
// Each `normalize*` function returns a new object with exactly
// the fields declared in the params interface, in the interface's
// declared order, with any `undefined` coerced to `null`.
//
// Why normalize at all, if the interfaces already force explicit
// `null`?
//
//   1. DEFENSE IN DEPTH. A caller who has `strict: false` in
//      tsconfig, or who constructs params via `JSON.parse`, can
//      still slip `undefined` past the type system. The
//      normalizer catches it.
//
//   2. STABLE FIELD SET. TanStack hashes `{a: 1}` and
//      `{a: 1, b: undefined}` differently (JSON drops
//      `undefined`). The normalizer guarantees the field set is
//      always complete.
//
//   3. FUTURE-PROOFING. If a new field is added to an interface
//      but a call site forgot to pass it, the normalizer's
//      `?? null` fills it in rather than producing a
//      structurally different key.

function normalizeEntryListParams(p: EntryListParams): EntryListParams {
  return {
    scope: p.scope,
    status: p.status,
    memory_type: p.memory_type ?? null,
    is_pinned: p.is_pinned ?? null,
    limit: p.limit,
    offset: p.offset,
  };
}

function normalizeUsageSummaryParams(
  p: UsageSummaryParams,
): UsageSummaryParams {
  return {
    period: p.period,
    chat_id: p.chat_id ?? null,
    project_id: p.project_id ?? null,
  };
}

function normalizeUsageBreakdownParams(
  p: UsageBreakdownParams,
): UsageBreakdownParams {
  return {
    by: p.by,
    period: p.period,
    chat_id: p.chat_id ?? null,
    project_id: p.project_id ?? null,
  };
}

function normalizeUsageTimeseriesParams(
  p: UsageTimeseriesParams,
): UsageTimeseriesParams {
  return {
    period: p.period,
    granularity: p.granularity,
  };
}

function normalizeJobsListParams(p: JobsListParams): JobsListParams {
  return {
    status: p.status ?? null,
    chat_id: p.chat_id ?? null,
    trigger_type: p.trigger_type ?? null,
    limit: p.limit,
    offset: p.offset,
  };
}

function normalizeBatchesListParams(
  p: BatchesListParams,
): BatchesListParams {
  return {
    status: p.status ?? null,
    limit: p.limit,
    offset: p.offset,
  };
}

// ---------------------------------------------------------------------------
// The key factory
// ---------------------------------------------------------------------------

/**
 * Canonical query-key factory for every memory query.
 *
 * Every function returns a `readonly` tuple -- TanStack Query's
 * `QueryKey` type is `readonly unknown[]`, so readonly tuples
 * satisfy it without a cast. Immutability also prevents the
 * classic "someone mutated the cached key array" bug.
 *
 * IMPORTANT: the entire object is `as const`. This makes:
 *
 *   * `memoryKeys.all` a literal `readonly ['memory']` -- usable
 *     as the root prefix for `invalidateQueries`.
 *   * the property graph deeply readonly -- so
 *     `memoryKeys.settings.global = ...` is a compile error.
 *
 * Each function ALSO has its own `as const` on its return
 * expression (required -- the outer `as const` does not narrow
 * function return types).
 */
export const memoryKeys = {
  /**
   * Root prefix. Invalidating this clears the ENTIRE memory
   * cache. Use only when you want a full reset (e.g. on logout,
   * or after the "disable memory" transition).
   */
  all: ['memory'] as const,

  // -------------------------------------------------------------------------
  // Settings
  // -------------------------------------------------------------------------

  settings: {
    /**
     * Prefix for ALL settings queries (global + every project).
     * Invalidating this resets both, which is correct after a
     * mode change (mode is a global setting but affects every
     * project's effective view).
     */
    all: () => [...memoryKeys.all, 'settings'] as const,

    /**
     * The global settings snapshot.
     *
     * Only ONE cache entry exists under this key -- the endpoint
     * takes no parameters. So `settings.global()` is both the
     * builder AND the invalidation prefix.
     */
    global: () => [...memoryKeys.settings.all(), 'global'] as const,

    /**
     * One project's (effective, override) settings pair.
     *
     * Invalidating `settings.project(id)` does NOT invalidate
     * sibling projects -- each project's override is
     * independent.
     */
    project: (projectId: string) =>
      [...memoryKeys.settings.all(), 'project', projectId] as const,
  },

  // -------------------------------------------------------------------------
  // Entries
  // -------------------------------------------------------------------------

  entries: {
    /**
     * Prefix for ALL entry queries (lists and details).
     *
     * Invalidating this is appropriate after bulk operations
     * that affect both list membership AND detail freshness --
     * e.g. bulk-purge, which can orphan every detail query.
     */
    all: () => [...memoryKeys.all, 'entries'] as const,

    /**
     * Prefix for ALL entry-list queries, regardless of params.
     *
     * Invalidating this catches every cached page. Use after
     * any write that can affect list membership (create,
     * update, delete, pin, import).
     */
    lists: () => [...memoryKeys.entries.all(), 'list'] as const,

    /**
     * One specific list (scope + filters + page).
     *
     * Normalizes params before embedding them, so two calls
     * with the same semantic params always produce the same
     * hash -- even if one caller omitted an `undefined` field
     * and the other passed `null`.
     */
    list: (params: EntryListParams) =>
      [
        ...memoryKeys.entries.lists(),
        normalizeEntryListParams(params),
      ] as const,

    /**
     * Prefix for ALL entry-detail queries, across all scopes.
     *
     * Invalidating this catches every detail view. Rarely
     * needed -- prefer `detail(id, scope)` for precision, or
     * rely on `lists()` invalidation which is usually enough.
     * Provided for the bulk-purge case where the affected ids
     * are not enumerable.
     */
    details: () => [...memoryKeys.entries.all(), 'detail'] as const,

    /**
     * One specific entry detail.
     *
     * The `scope` parameter is part of the key (not just the
     * id) because the backend's detail endpoint requires the
     * scope as a query parameter, and the same id in different
     * scopes is a distinct logical entity (even though
     * `memory_id` is globally unique in practice, the API
     * treats scope as part of the addressing scheme).
     */
    detail: (memoryId: string, scope: string) =>
      [...memoryKeys.entries.details(), memoryId, scope] as const,
  },

  // -------------------------------------------------------------------------
  // Projects
  // -------------------------------------------------------------------------

  projects: {
    /**
     * Prefix for ALL project-aggregation queries.
     */
    all: () => [...memoryKeys.all, 'projects'] as const,

    /**
     * The projects list (per-project counts).
     *
     * Only ONE cache entry exists -- the endpoint takes no
     * parameters.
     */
    list: () => [...memoryKeys.projects.all(), 'list'] as const,

    /**
     * One project's detailed statistics.
     *
     * Invalidating `projects.stats(id)` is appropriate after
     * any write that changes the project's entry counts or
     * distributions (create, delete, bulk-archive,
     * bulk-purge).
     */
    stats: (projectId: string) =>
      [...memoryKeys.projects.all(), 'stats', projectId] as const,
  },

  // -------------------------------------------------------------------------
  // Usage
  // -------------------------------------------------------------------------

  usage: {
    /**
     * Prefix for ALL usage queries.
     *
     * Invalidating `usage.all()` resets summary, breakdown, and
     * timeseries together. Appropriate after any settings
     * change that affects the token budget (because
     * `budget_utilization` and `drop_rate` in the summary
     * depend on the current budget).
     */
    all: () => [...memoryKeys.all, 'usage'] as const,

    /**
     * The usage summary for a period + optional filters.
     *
     * `period` and filters are part of the key: the summary
     * for "month, project:acme" is DIFFERENT data from
     * "month, all projects". Two entries coexist in cache.
     */
    summary: (params: UsageSummaryParams) =>
      [
        ...memoryKeys.usage.all(),
        'summary',
        normalizeUsageSummaryParams(params),
      ] as const,

    /**
     * The usage breakdown for a dimension + period + filters.
     *
     * The `by` dimension is part of the key -- switching from
     * "by scope" to "by project" is a DIFFERENT query, not a
     * refetch of the same one.
     */
    breakdown: (params: UsageBreakdownParams) =>
      [
        ...memoryKeys.usage.all(),
        'breakdown',
        normalizeUsageBreakdownParams(params),
      ] as const,

    /**
     * The usage timeseries for a period + granularity.
     *
     * No chat/project filters on this endpoint -- the backend
     * does not support them on `/usage/timeseries`.
     */
    timeseries: (params: UsageTimeseriesParams) =>
      [
        ...memoryKeys.usage.all(),
        'timeseries',
        normalizeUsageTimeseriesParams(params),
      ] as const,
  },

  // -------------------------------------------------------------------------
  // Import adapters
  // -------------------------------------------------------------------------

  importAdapters: {
    /**
     * The registered import adapters list.
     *
     * Fetched once when the import wizard opens. The list is
     * effectively static for the lifetime of the process --
     * adapters are registered at startup and do not change at
     * runtime in the current backend. So the query can use a
     * very long `staleTime` (see queries.ts). Included as a
     * query key anyway (rather than as a constant) so that the
     * wizard can render a loading state on first mount and a
     * cache hit on subsequent ones without any conditional
     * logic.
     */
    all: () => [...memoryKeys.all, 'import', 'adapters'] as const,
  },

  // -------------------------------------------------------------------------
  // Embedding (Phase 4)
  // -------------------------------------------------------------------------
  //
  // The Phase 4 embedding surface under `/api/memory/*` is
  // deliberately NARROW:
  //
  //   * `stats` -- read-only coverage snapshot from
  //                `GET /api/memory/embedding-stats`.
  //
  //   * reindex  -- `POST /api/memory/reindex` is a MUTATION.
  //                 Mutations do not own query keys -- they
  //                 invalidate the queries whose data they
  //                 affect. The reindex mutation invalidates
  //                 `embedding.stats()` and `settings.all()`
  //                 (see mutations.ts).
  //
  // The jobs endpoint used to monitor in-flight embedding work
  // (`GET /api/memory/automation/jobs`) already lives under
  // `automation.jobs` -- no new key is needed; reindex jobs are
  // regular MemoryJob rows with `job_type: 'embedding'`.
  //
  // The model catalog (`GET /api/config/embedding-models`) is
  // NOT here -- see `configKeys` at the bottom of this file.
  // Its root is `'config'` (not `'memory'`), so invalidating
  // `memoryKeys.all` does NOT clear it, which is correct: the
  // catalog is provider-scoped and shared across subsystems.

  embedding: {
    /**
     * Prefix for every Phase 4 embedding query under
     * `/api/memory/*`.
     *
     * Invalidating this resets the stats snapshot but leaves
     * the model catalog (`configKeys.all`) and the jobs list
     * (`memoryKeys.automation.jobs.all()`) untouched --
     * exactly the scope a `POST /api/memory/reindex` failure
     * should touch.
     */
    all: () => [...memoryKeys.all, 'embedding'] as const,

    /**
     * The single coverage snapshot
     * (`GET /api/memory/embedding-stats`).
     *
     * No parameters -- the endpoint returns the state under
     * the CURRENT `embedding.model_key`. Switching models
     * invalidates the cache via a `settings.all()`
     * invalidation (from the settings PATCH mutation), so two
     * coexisting "current model" snapshots can never exist
     * in cache.
     *
     * OPTIONAL ENDPOINT (plan §9): some deployments may return
     * 404. The consumer hook (`useEmbeddingStats`) treats a
     * 404 as a graceful degradation (returns `null` data, no
     * error banner) rather than a user-facing failure. The
     * key still exists so the mutation can invalidate it
     * regardless of whether the endpoint actually responds
     * -- an invalidation of a non-existent cache entry is a
     * no-op, not an error.
     */
    stats: () => [...memoryKeys.embedding.all(), 'stats'] as const,
  },

  // -------------------------------------------------------------------------
  // Automation (Phase 3)
  // -------------------------------------------------------------------------
  //
  // The Phase 3 surface lives ENTIRELY under a single `automation`
  // namespace, mirroring the backend's mount point
  // (`/api/memory/automation/*`). This isolation matters for two
  // reasons:
  //
  //   1. **Invalidation containment.** Invalidating
  //      `memoryKeys.automation.all()` never touches any Phase-1
  //      cache (settings / entries / projects / usage / imports /
  //      exports). A future "reset the automation UI" action can
  //      be implemented as a single prefix invalidation without
  //      risking collateral damage.
  //
  //   2. **Readability.** A grep for `automation.` in a
  //      component immediately identifies it as Phase-3 code --
  //      useful when triaging which subsystem owns a bug report.
  //
  // Sub-namespace layout mirrors the four backend sub-routers
  // (`memory_automation.py`, `memory_jobs.py`,
  // `memory_extraction.py`, `memory_batch.py`), plus a shared
  // `extract` alias for the two manual-extraction endpoints
  // (which do not have their own top-level URL segment --
  // they live under `/automation/extract/*`).

  automation: {
    /**
     * Prefix for EVERY Phase 3 query. Invalidating this clears
     * the entire automation cache in one call -- appropriate on
     * logout, or after the "disable AI extraction" transition
     * (which makes every automation query return 503).
     */
    all: () => [...memoryKeys.all, 'automation'] as const,

    // ---------------------------------------------------------------------
    // Automation settings
    // ---------------------------------------------------------------------

    settings: {
      /** Prefix for every automation-settings query. */
      all: () => [...memoryKeys.automation.all(), 'settings'] as const,

      /**
       * The single AI-settings snapshot.
       *
       * No parameters -- the endpoint returns the current
       * `MemoryAISettings` regardless of scope. So `get()` is
       * both the builder AND the invalidation prefix; there is
       * no way to cache two "current AI settings" entries.
       */
      get: () => [...memoryKeys.automation.settings.all(), 'get'] as const,
    },

    // ---------------------------------------------------------------------
    // Extraction jobs
    // ---------------------------------------------------------------------

    jobs: {
      /** Prefix for every job query (lists + details). */
      all: () => [...memoryKeys.automation.all(), 'jobs'] as const,

      /** Prefix for every job list (any params). */
      lists: () => [...memoryKeys.automation.jobs.all(), 'list'] as const,

      /**
       * One specific jobs list (filters + page).
       *
       * Normalized before hashing, so `{status: null, ...}` and
       * `{status: undefined, ...}` produce the same key.
       * Different filter combinations are distinct cache
       * entries -- toggling the status filter on the jobs page
       * fetches a new page rather than refetching the previous
       * one.
       */
      list: (params: JobsListParams) =>
        [
          ...memoryKeys.automation.jobs.lists(),
          normalizeJobsListParams(params),
        ] as const,

      /** Prefix for every job detail query. */
      details: () => [...memoryKeys.automation.jobs.all(), 'detail'] as const,

      /**
       * One specific job detail.
       *
       * `jobId` is the ONLY parameter -- the backend has no
       * scope filter for jobs (the table is a global audit
       * trail), so two clients asking for the same id get the
       * same row.
       */
      detail: (jobId: string) =>
        [...memoryKeys.automation.jobs.details(), jobId] as const,
    },

    // ---------------------------------------------------------------------
    // Batch orchestration
    // ---------------------------------------------------------------------

    batch: {
      /** Prefix for every batch query. */
      all: () => [...memoryKeys.automation.all(), 'batch'] as const,

      /** Prefix for every batch list (any params). */
      lists: () => [...memoryKeys.automation.batch.all(), 'list'] as const,

      /** One specific batches list (status filter + page). */
      list: (params: BatchesListParams) =>
        [
          ...memoryKeys.automation.batch.lists(),
          normalizeBatchesListParams(params),
        ] as const,

      /** Prefix for every batch detail query. */
      details: () => [...memoryKeys.automation.batch.all(), 'detail'] as const,

      /** One specific batch detail (with live progress). */
      detail: (batchId: string) =>
        [...memoryKeys.automation.batch.details(), batchId] as const,

      /**
       * The single cost estimate for a fresh batch.
       *
       * No parameters -- the estimate is a snapshot of the
       * current workspace state. Every "Start Batch" dialog
       * opening wants a fresh value, so consumers should use
       * `staleTime: 0` (see `MEMORY_STALE_TIMES.batchEstimate`
       * in `queries.ts`). The key is namespaced separately
       * from details so a routine "refresh the currently-open
       * batch" invalidation does not force a new estimate.
       */
      estimate: () => [...memoryKeys.automation.batch.all(), 'estimate'] as const,
    },
  },
} as const;

// ---------------------------------------------------------------------------
// Type helpers
// ---------------------------------------------------------------------------

/**
 * The type of a "fully-built" memory query key.
 *
 * Useful when declaring function parameters or React state that
 * holds a query key. Do NOT use this to construct keys -- use
 * the factory above.
 *
 * Note: this type only captures the KEY structure, not the
 * params that went into it. For type-safe invalidation with
 * exact params, call the specific factory function.
 */
export type MemoryQueryKey = readonly ['memory', ...readonly unknown[]];

/**
 * Detects whether a query key belongs to the memory feature.
 *
 * Used by generic utilities (e.g. a devtools filter, a logout
 * handler that wants to clear only the memory cache) that
 * receive keys from outside this module.
 *
 * NOTE: TanStack Query's `invalidateQueries({ queryKey: prefix })`
 * already does prefix matching internally -- this guard is for
 * callers who receive a single key from somewhere and need to
 * decide whether it is theirs to touch.
 */
export function isMemoryQueryKey(
  key: readonly unknown[] | undefined,
): key is MemoryQueryKey {
  return Array.isArray(key) && key.length > 0 && key[0] === 'memory';
}

// ---------------------------------------------------------------------------
// Config query keys
// ---------------------------------------------------------------------------
//
// A SEPARATE factory (root `'config'`, not `'memory'`) for the
// `/api/config/*` endpoints the memory feature consumes.
//
// Why a separate factory and not a `memoryKeys.config` sub-tree
// ------------------------------------------------------------------
// `/api/config/*` responses are PROVIDER-SCOPED, not memory-
// scoped. They are shared with other frontend features
// (chat, generation, model settings) that also read from
// `/api/config/*`. Putting them under `'memory'` would cause:
//
//   1. Over-invalidation. `queryClient.invalidateQueries({
//      queryKey: memoryKeys.all })` (e.g. on logout, or after
//      "disable memory") would clear the model catalog used
//      by the chat UI -- a cross-subsystem surprise.
//
//   2. Cache-key collisions if a future consumer reads the
//      same endpoint from a different subsystem and the two
//      call sites disagree on the key path.
//
// A separate `'config'` root means invalidating one subsystem
// leaves the other's cache intact, which matches the actual
// data dependency graph.
//
// MIGRATION NOTE (Phase 3 legacy)
// -------------------------------
// `MemoryModelSelector.tsx` (Phase 3) currently uses a raw
// `['config', 'memory-models']` array inline at its useQuery
// call site. That predates this factory. The keys are
// STRUCTURALLY IDENTICAL -- `configKeys.memoryModels()` would
// return the same tuple -- so existing Phase 3 caching
// behavior is preserved byte-for-byte. A future cleanup can
// migrate `MemoryModelSelector` to use `configKeys.memoryModels()`
// without changing runtime behavior. Not done here to avoid
// touching a working Phase 3 component.

export const configKeys = {
  /**
   * Root prefix for every `/api/config/*` query the memory
   * feature reads. Matches the raw `['config', ...]` array
   * `MemoryModelSelector` already uses for its
   * `memory-models` key.
   */
  all: ['config'] as const,

  /**
   * The user's marked "memory models" catalog
   * (`GET /api/config/memory-models`).
   *
   * Structurally equal to the inline `['config', 'memory-models']`
   * used by `MemoryModelSelector` -- preserved as a builder
   * for future migration of that call site without a
   * cache-behavior change.
   */
  memoryModels: () =>
    [...configKeys.all, 'memory-models'] as const,

  /**
   * The embedding-models catalog
   * (`GET /api/config/embedding-models`).
   *
   * Consumed by `useEmbeddingModels` (queries.ts), which
   * delegates to `configClient.getEmbeddingModels()`.
   *
   * Invalidated by:
   *   * The models-settings page when the user marks or
   *     unmarks an embedding model (via its own mutation,
   *     not ours -- the memory feature just picks up the
   *     refreshed cache).
   *   * NOT by `POST /api/memory/reindex` -- the catalog
   *     does not change when a reindex runs.
   */
  embeddingModels: () =>
    [...configKeys.all, 'embedding-models'] as const,
} as const;

/**
 * The type of a "fully-built" config query key.
 *
 * Sibling of `MemoryQueryKey` for the `/api/config/*` surface.
 */
export type ConfigQueryKey = readonly ['config', ...readonly unknown[]];

/**
 * Detects whether a query key belongs to the config feature.
 *
 * Mirror of `isMemoryQueryKey` for the `'config'` root.
 */
export function isConfigQueryKey(
  key: readonly unknown[] | undefined,
): key is ConfigQueryKey {
  return Array.isArray(key) && key.length > 0 && key[0] === 'config';
}
