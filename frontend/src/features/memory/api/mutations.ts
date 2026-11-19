// src/features/memory/api/mutations.ts

/**
 * React Query mutation hooks for the PELDRUN Memory feature.
 *
 * Every write the UI performs goes through exactly one hook
 * here. The mapping mirrors `queries.ts`: one hook, one
 * `memoryClient` method, one explicit invalidation policy.
 *
 * Invalidation policy
 * -------------------
 *
 * Each mutation declares WHICH cache keys it invalidates, drawn
 * directly from the spec's section 4.4 table. The invalidation
 * happens in `onSuccess`, AFTER the backend confirms the write.
 * It is fire-and-forget (no `await`): the mutation's pending
 * state should reflect the network request, not the refetch of
 * dependent queries. Callers who need "mutation fully settled
 * including refetches" can `await` the mutation's `mutateAsync`
 * and then `await queryClient.invalidateQueries(...)`
 * themselves.
 *
 * Optimistic updates
 * ------------------
 *
 * ONLY `usePinEntry` performs an optimistic cache update. Every
 * other mutation (create, update, delete, promote, bulk-archive,
 * bulk-purge, settings changes, import apply) waits for the
 * backend response before touching the cache. This is
 * deliberate:
 *
 *   * Pin is REVERSIBLE (unpin undoes it) and low-risk (worst
 *     case: a slightly stale pin indicator for a fraction of a
 *     second). The UX win (instant feedback on a common action)
 *     outweighs the risk.
 *
 *   * Delete, bulk-purge, and promote are IRREVERSIBLE or
 *     COPY-SEMANTICS. An optimistic UI that shows "deleted"
 *     before the backend confirms risks showing success for an
 *     operation that actually failed -- the user has walked
 *     away from a dialog believing the entry is gone, but it
 *     is not. The spec's rule (§6.1 rule 3) formalizes this.
 *
 *   * Create and update are technically reversible but the
 *     window between the optimistic state and the backend's
 *     authoritative version is large enough (server-generated
 *     timestamps, `content_hash`, computed `scope`) that
 *     optimistic rendering would show a visually different
 *     entry than the one that lands. Not worth it.
 *
 * Variable types
 * --------------
 *
 * Every mutation's variables type is EXTRACTED from the
 * corresponding `memoryClient` method with
 * `Parameters<typeof ...>[N]`. This means the mutation cannot
 * drift from the client: changing the client's method
 * signature immediately breaks the mutation's variables type,
 * surfacing as a compile error at the call site.
 *
 * Import discriminant
 * -------------------
 *
 * Import preview and apply accept BOTH multipart and JSON
 * requests. Rather than exporting four separate hooks (preview-
 * file, preview-source, apply-file, apply-source), this module
 * exposes two hooks that take a discriminated union of the
 * request (`{ kind: 'file', request }` or
 * `{ kind: 'source', request }`). The discriminant prevents
 * the classic mistake of calling `apply` with a preview-only
 * request shape, and keeps the call sites readable.
 */

import {
  useMutation,
  useQueryClient,
  type UseMutationResult,
} from '@tanstack/react-query';

import { memoryClient } from './client';
import { memoryKeys } from './keys';
import type { MemoryApiError } from '../types/errors';
import type {
  BulkArchiveResponse,
  BulkPurgeResponse,
  CreateMemoryResponse,
  DeleteMemoryResponse,
  ExportJsonResponse,
  GetMemoryResponse,
  ImportApplyResponse,
  ImportPreviewResponse,
  ListMemoryResponse,
  PinMemoryResponse,
  ProjectSettingsDeleteResponse,
  ProjectSettingsPutResponse,
  PromoteMemoryResponse,
  SearchMemoryResponse,
  SettingsUpdateResponse,
  UpdateMemoryResponse,
} from '../types/responses';
// Phase 6 agentic retrieval response envelope.
import type { AgenticSearchResponse } from '../types/agentic';
import type {
  ExportDownloadResult,
  ExportQueryParams,
  ImportJsonRequest,
  ImportMultipartRequest,
} from '../types/import-export';

// ---------------------------------------------------------------------------
// Phase 3 (Automation) imports
// ---------------------------------------------------------------------------
//
// Grouped together and placed AFTER the Phase-1 imports so the
// diff between the two surfaces is unambiguous when reading the
// file. `MemoryAISettingsOverlay` is the deep-partial patch
// shape; the response types are the 10 envelopes the automation
// mutations produce.

import type { MemoryAISettingsOverlay } from '../types/ai';
import type {
  AISettingsUpdateResponse,
  BatchCancelResponse,
  BatchCreateResponse,
  BatchPauseResponse,
  BatchResumeResponse,
  ExtractFromSessionResponse,
  ExtractRangeResponse,
  JobCancelResponse,
  JobRetryResponse,
  TestModelResponse,
  ReindexResponse,
} from '../types/responses';

// Phase 4 -- reindex request body type. Grouped separately so
// the Phase 4 surface (single mutation, single type) is
// obvious. `ReindexResponse` lives in the block above because
// it is a response envelope (import locality with siblings).
import type { ReindexRequest } from '../types/embedding';

// ---------------------------------------------------------------------------
// Shared hook-options base
// ---------------------------------------------------------------------------

/**
 * Common lifecycle callbacks every mutation hook accepts.
 *
 * `onSuccess` and `onError` here are called AFTER the built-in
 * invalidation logic has been scheduled. They do NOT override
 * invalidation -- a caller cannot break the cache coherence by
 * forgetting to invalidate. The built-in invalidation is
 * unconditional; the callbacks are for user-facing feedback
 * (toasts, navigation).
 */
interface BaseMutationOptions<TData, TVariables> {
  onSuccess?: (data: TData, variables: TVariables) => void | Promise<void>;
  onError?: (error: MemoryApiError, variables: TVariables) => void | Promise<void>;
  onSettled?: (
    data: TData | undefined,
    error: MemoryApiError | null,
    variables: TVariables,
  ) => void | Promise<void>;
}

// ---------------------------------------------------------------------------
// Invalidation helpers
// ---------------------------------------------------------------------------

/**
 * Invalidate every cache entry that a SETTINGS change could
 * affect.
 *
 * Settings drive the retrieval pipeline's token budget, so a
 * change to `token_budget` (or `mode`, which changes whether
 * usage is logged at all) invalidates the usage queries. The
 * per-project settings map is invalidated too because
 * `effective` for every project depends on the base settings.
 */
function invalidateSettingsCascade(
  queryClient: ReturnType<typeof useQueryClient>,
): void {
  queryClient.invalidateQueries({ queryKey: memoryKeys.settings.global() });
  queryClient.invalidateQueries({ queryKey: memoryKeys.settings.all() });
  queryClient.invalidateQueries({ queryKey: memoryKeys.usage.all() });
  // Phase 4: the embedding-stats snapshot is SCOPED to the
  // current `embedding.model_key`. Changing that key (via
  // PATCH /settings {embedding: {model_key: "..."}}) means
  // the previous "with_embeddings" / "vec0_dimension" numbers
  // no longer describe the active model. Invalidating here
  // (rather than in a hypothetical embedding-specific cascade)
  // keeps the coherence rule in ONE place -- every settings
  // change already goes through this function.
  //
  // The extra refetch is essentially free: the endpoint is
  // small, and `invalidateQueries` only refetches queries
  // that are CURRENTLY OBSERVED. If the ReindexPanel is not
  // mounted, no network call happens.
  queryClient.invalidateQueries({ queryKey: memoryKeys.embedding.stats() });
  // Phase 6: the agentic availability probe is a projection of
  // settings (`retrieval.agentic_enabled` + a resolvable model), so
  // any settings PATCH can change it. Invalidating here keeps the
  // composer toggle honest: turning agentic ON in Settings makes the
  // toggle usable on the next render without a page reload.
  queryClient.invalidateQueries({ queryKey: memoryKeys.agentic.all() });
}

/**
 * Invalidate caches that depend on entry membership in a scope.
 *
 * Used by create, update, delete, pin, and promote. The
 * `scopeId` parameter (when known) narrows the `projects.stats`
 * invalidation to the affected project -- invalidating ALL
 * project stats on every write would be wasteful. Pass `null`
 * when the scope is not a project (e.g. a user-scope write).
 */
function invalidateEntryCascade(
  queryClient: ReturnType<typeof useQueryClient>,
  scopeId: string | null,
): void {
  queryClient.invalidateQueries({ queryKey: memoryKeys.entries.lists() });
  queryClient.invalidateQueries({ queryKey: memoryKeys.projects.list() });
  if (scopeId) {
    queryClient.invalidateQueries({
      queryKey: memoryKeys.projects.stats(scopeId),
    });
  }
}

// ---------------------------------------------------------------------------
// Settings mutations
// ---------------------------------------------------------------------------

/**
 * Variables for `usePatchSettings`.
 *
 * Mirrors the first parameter of `memoryClient.settings.patch`.
 * (The second parameter is the abort-signal bag, which mutations
 * do not use.)
 */
type PatchSettingsVariables = Parameters<
  typeof memoryClient.settings.patch
>[0];

/**
 * `PATCH /api/memory/settings` -- apply a partial update.
 *
 * Callers MUST guard against no-op patches (using `isNoOpPatch`
 * from `types/settings.ts`) -- the backend rejects empty
 * patches with `400 empty_update`, and a value-changed-then-
 * changed-back scenario is a common source of confusing error
 * toasts. The mutation does not guard internally because it
 * cannot know the "current" settings without a cache read,
 * which the caller already has from `useMemorySettings()`.
 *
 * On success, invalidates the entire settings cascade (global
 * snapshot, every project's effective view, all usage queries)
 * because a global settings change affects all of them.
 */
export function usePatchSettings(
  options: BaseMutationOptions<SettingsUpdateResponse, PatchSettingsVariables> = {},
): UseMutationResult<
  SettingsUpdateResponse,
  MemoryApiError,
  PatchSettingsVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    SettingsUpdateResponse,
    MemoryApiError,
    PatchSettingsVariables
  >({
    mutationFn: (patch) => memoryClient.settings.patch(patch),
    onSuccess: (data, variables) => {
      invalidateSettingsCascade(queryClient);
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * Variables for `usePutProjectOverride`.
 */
type PutProjectOverrideVariables = {
  projectId: string;
  override: Parameters<typeof memoryClient.settings.putProject>[1];
};

/**
 * `PUT /api/memory/settings/projects/{id}` -- replace override.
 *
 * HTTP PUT semantics: the body REPLACES the project's override
 * slot wholesale. To merge with an existing override, the caller
 * must GET first, merge client-side, and PUT back. There is no
 * PATCH for project settings.
 *
 * On success, invalidates the project's settings AND the usage
 * queries (the effective budget for that project has changed).
 * Does NOT invalidate other projects -- their overrides are
 * independent.
 */
export function usePutProjectOverride(
  options: BaseMutationOptions<
    ProjectSettingsPutResponse,
    PutProjectOverrideVariables
  > = {},
): UseMutationResult<
  ProjectSettingsPutResponse,
  MemoryApiError,
  PutProjectOverrideVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    ProjectSettingsPutResponse,
    MemoryApiError,
    PutProjectOverrideVariables
  >({
    mutationFn: ({ projectId, override }) =>
      memoryClient.settings.putProject(projectId, override),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.settings.project(variables.projectId),
      });
      queryClient.invalidateQueries({ queryKey: memoryKeys.usage.all() });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * Variables for `useDeleteProjectOverride`.
 */
type DeleteProjectOverrideVariables = { projectId: string };

/**
 * `DELETE /api/memory/settings/projects/{id}` -- remove override.
 *
 * Idempotent. On success, invalidates the project's settings
 * (its `effective` reverts to global) and the usage queries.
 */
export function useDeleteProjectOverride(
  options: BaseMutationOptions<
    ProjectSettingsDeleteResponse,
    DeleteProjectOverrideVariables
  > = {},
): UseMutationResult<
  ProjectSettingsDeleteResponse,
  MemoryApiError,
  DeleteProjectOverrideVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    ProjectSettingsDeleteResponse,
    MemoryApiError,
    DeleteProjectOverrideVariables
  >({
    mutationFn: ({ projectId }) =>
      memoryClient.settings.deleteProject(projectId),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.settings.project(variables.projectId),
      });
      queryClient.invalidateQueries({ queryKey: memoryKeys.usage.all() });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Entry mutations
// ---------------------------------------------------------------------------

/**
 * Variables for `useCreateEntry`.
 */
type CreateEntryVariables = Parameters<
  typeof memoryClient.entries.create
>[0];

/**
 * `POST /api/memory` -- create a new entry.
 *
 * The write goes directly through `provider.create`, NOT through
 * the write pipeline (dedupe/supersede logic). So the response
 * is always a freshly-inserted entry -- the caller does NOT need
 * to handle `skipped`/`superseded`/`reactivated` outcomes on
 * this path. Those exist only on the agent tool path and the
 * import path.
 *
 * On success, invalidates the entry lists, the projects list
 * (a new project may now exist if this was the first entry in
 * its scope), and -- when the scope is a project -- that
 * project's stats.
 */
export function useCreateEntry(
  options: BaseMutationOptions<CreateMemoryResponse, CreateEntryVariables> = {},
): UseMutationResult<
  CreateMemoryResponse,
  MemoryApiError,
  CreateEntryVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    CreateMemoryResponse,
    MemoryApiError,
    CreateEntryVariables
  >({
    mutationFn: (body) => memoryClient.entries.create(body),
    onSuccess: (data, variables) => {
      invalidateEntryCascade(
        queryClient,
        variables.scope_type === 'project' ? variables.scope_id ?? null : null,
      );
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * Variables for `useUpdateEntry`.
 *
 * Split into `params` (which entry) and `patch` (what to change),
 * mirroring the client's two-parameter signature.
 */
type UpdateEntryVariables = {
  params: Parameters<typeof memoryClient.entries.update>[0];
  patch: Parameters<typeof memoryClient.entries.update>[1];
};

/**
 * `PATCH /api/memory/{id}` -- partial update.
 *
 * The body MUST contain at least one field (backend rejects
 * empty patches). Each field fully replaces its counterpart --
 * e.g. `tags` replaces the tags array, not appends to it.
 *
 * The entry's scope is IMMUTABLE via this endpoint. Use
 * `usePromoteEntry` to copy into user scope.
 *
 * On success, invalidates the specific detail AND all lists
 * (the entry may have moved in/out of filtered results).
 */
export function useUpdateEntry(
  options: BaseMutationOptions<UpdateMemoryResponse, UpdateEntryVariables> = {},
): UseMutationResult<
  UpdateMemoryResponse,
  MemoryApiError,
  UpdateEntryVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    UpdateMemoryResponse,
    MemoryApiError,
    UpdateEntryVariables
  >({
    mutationFn: ({ params, patch }) =>
      memoryClient.entries.update(params, patch),
    onSuccess: (data, variables) => {
      const scope = `${variables.params.scope_type}:${variables.params.scope_id}`;
      queryClient.invalidateQueries({
        queryKey: memoryKeys.entries.detail(variables.params.memoryId, scope),
      });
      invalidateEntryCascade(
        queryClient,
        variables.params.scope_type === 'project'
          ? variables.params.scope_id
          : null,
      );
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * Variables for `useDeleteEntry`.
 */
type DeleteEntryVariables = Parameters<
  typeof memoryClient.entries.delete
>[0];

/**
 * `DELETE /api/memory/{id}` -- soft (default) or hard delete.
 *
 * Soft delete is REVERSIBLE (the entry becomes `archived` and
 * can be recovered via list with `status: 'all'`). Hard delete
 * is PERMANENT. The UI MUST gate `hard: true` behind a
 * type-to-confirm dialog (see `ConfirmDangerDialog`); the hook
 * does not enforce this because the confirmation belongs to the
 * component that owns the dialog state.
 *
 * On success, invalidates the specific detail, all lists, and
 * the project stats (if the scope is a project).
 */
export function useDeleteEntry(
  options: BaseMutationOptions<DeleteMemoryResponse, DeleteEntryVariables> = {},
): UseMutationResult<
  DeleteMemoryResponse,
  MemoryApiError,
  DeleteEntryVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    DeleteMemoryResponse,
    MemoryApiError,
    DeleteEntryVariables
  >({
    mutationFn: (params) => memoryClient.entries.delete(params),
    onSuccess: (data, variables) => {
      const scope = `${variables.scope_type}:${variables.scope_id}`;
      queryClient.invalidateQueries({
        queryKey: memoryKeys.entries.detail(variables.memoryId, scope),
      });
      invalidateEntryCascade(
        queryClient,
        variables.scope_type === 'project' ? variables.scope_id : null,
      );
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * Variables for `usePinEntry`.
 *
 * Flat shape (not split into `params` / `body` like the client
 * method) because the component owns these as a unit and the
 * optimistic update logic reads them all at once.
 */
type PinEntryVariables = {
  memoryId: string;
  scope_type: string;
  scope_id: string;
  pinned: boolean;
  importance?: number;
};

/**
 * The context object returned by `usePinEntry.onMutate` and
 * consumed by `onError` for rollback.
 *
 * Holds snapshots of every cache slice the optimistic update
 * touched, so the rollback restores the EXACT previous state
 * (not a re-derivation, which could race with concurrent
 * mutations).
 */
interface PinOptimisticContext {
  previousDetail: GetMemoryResponse | undefined;
  previousLists: Array<
    [readonly unknown[], ListMemoryResponse | undefined]
  >;
}

/**
 * `POST /api/memory/{id}/pin` -- set pin state (toggle-style).
 *
 * This is the ONLY mutation with an optimistic cache update
 * (see the module JSDoc for the reasoning). The optimistic
 * update touches:
 *
 *   * The specific detail cache (`entries.detail(id, scope)`).
 *   * EVERY cached list (`entries.lists()`) -- because the
 *     entry may appear in any list that matches its scope and
 *     filters, and we cannot know which without iterating.
 *
 * `onError` rolls back BOTH from the snapshots. `onSettled`
 * invalidates both to reconcile with the server's authoritative
 * state (which may differ slightly -- e.g. `updated_at` on the
 * returned entry, which the optimistic update cannot know).
 *
 * If the mutation fails and the caller's `onError` throws, the
 * rollback still happens FIRST (the built-in handler runs
 * before the user callback). This ordering is essential --
 * otherwise a throwing user callback would leave the cache in
 * the optimistic state indefinitely.
 */
export function usePinEntry(
  options: BaseMutationOptions<PinMemoryResponse, PinEntryVariables> = {},
): UseMutationResult<
  PinMemoryResponse,
  MemoryApiError,
  PinEntryVariables,
  PinOptimisticContext
> {
  const queryClient = useQueryClient();
  return useMutation<
    PinMemoryResponse,
    MemoryApiError,
    PinEntryVariables,
    PinOptimisticContext
  >({
    mutationFn: ({ memoryId, scope_type, scope_id, pinned, importance }) =>
      memoryClient.entries.pin(
        { memoryId, scope_type, scope_id },
        { pinned, importance },
      ),

    onMutate: async (variables): Promise<PinOptimisticContext> => {
      const scope = `${variables.scope_type}:${variables.scope_id}`;
      const detailKey = memoryKeys.entries.detail(variables.memoryId, scope);

      // Cancel any in-flight refetches for the affected queries
      // so they do not overwrite our optimistic state with stale
      // server data before the mutation settles.
      await queryClient.cancelQueries({ queryKey: detailKey });
      await queryClient.cancelQueries({
        queryKey: memoryKeys.entries.lists(),
      });

      // Snapshot both slices.
      const previousDetail =
        queryClient.getQueryData<GetMemoryResponse>(detailKey);
      const previousLists = queryClient.getQueriesData<ListMemoryResponse>({
        queryKey: memoryKeys.entries.lists(),
      });

      // Optimistic update: detail cache.
      if (previousDetail) {
        queryClient.setQueryData<GetMemoryResponse>(detailKey, {
          ...previousDetail,
          memory: {
            ...previousDetail.memory,
            is_pinned: variables.pinned,
            importance:
              variables.importance ?? previousDetail.memory.importance,
          },
        });
      }

      // Optimistic update: every list variant.
      queryClient.setQueriesData<ListMemoryResponse>(
        { queryKey: memoryKeys.entries.lists() },
        (old) => {
          if (!old) return old;
          return {
            ...old,
            memories: old.memories.map((m) =>
              m.memory_id === variables.memoryId
                ? {
                    ...m,
                    is_pinned: variables.pinned,
                    importance: variables.importance ?? m.importance,
                  }
                : m,
            ),
          };
        },
      );

      return { previousDetail, previousLists };
    },

    onError: (error, variables, context) => {
      // Rollback BEFORE calling the user's onError so a throwing
      // callback cannot leave the cache in the optimistic state.
      if (context) {
        const scope = `${variables.scope_type}:${variables.scope_id}`;
        const detailKey = memoryKeys.entries.detail(
          variables.memoryId,
          scope,
        );
        if (context.previousDetail) {
          queryClient.setQueryData(detailKey, context.previousDetail);
        } else {
          // No prior detail was cached -- remove whatever the
          // optimistic update may have created (defensive; the
          // optimistic branch above only updates when
          // `previousDetail` exists, so this is usually a no-op).
          queryClient.removeQueries({ queryKey: detailKey });
        }
        for (const [key, data] of context.previousLists) {
          queryClient.setQueryData(key, data);
        }
      }
      options.onError?.(error, variables);
    },

    onSettled: (data, error, variables) => {
      const scope = `${variables.scope_type}:${variables.scope_id}`;
      queryClient.invalidateQueries({
        queryKey: memoryKeys.entries.detail(variables.memoryId, scope),
      });
      queryClient.invalidateQueries({
        queryKey: memoryKeys.entries.lists(),
      });
      options.onSettled?.(data, error, variables);
    },
  });
}

/**
 * Variables for `usePromoteEntry`.
 */
type PromoteEntryVariables = {
  params: Parameters<typeof memoryClient.entries.promote>[0];
  body: Parameters<typeof memoryClient.entries.promote>[1];
};

/**
 * `POST /api/memory/{id}/promote` -- COPY into user scope.
 *
 * The source entry is UNTOUCHED; the response carries a NEW
 * entry. The UI MUST state this in the confirmation dialog --
 * users expect move semantics.
 *
 * On success, invalidates ALL entry lists (not just the source
 * and target scopes) because the promoted entry appears in the
 * user scope while remaining in its source scope, so any list
 * could be affected. This is slightly over-broad but correct
 * and cheap (list refetch is fast).
 */
export function usePromoteEntry(
  options: BaseMutationOptions<PromoteMemoryResponse, PromoteEntryVariables> = {},
): UseMutationResult<
  PromoteMemoryResponse,
  MemoryApiError,
  PromoteEntryVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    PromoteMemoryResponse,
    MemoryApiError,
    PromoteEntryVariables
  >({
    mutationFn: ({ params, body }) =>
      memoryClient.entries.promote(params, body),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.entries.lists(),
      });
      // The project whose entry was promoted may want to show
      // "already in user memory" indicators -- refresh its stats
      // so any derived counts update.
      if (variables.params.scope_type === 'project') {
        queryClient.invalidateQueries({
          queryKey: memoryKeys.projects.stats(variables.params.scope_id),
        });
      }
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Search (preview retrieval)
// ---------------------------------------------------------------------------

/**
 * Variables for `useSearchMemories`.
 */
type SearchMemoriesVariables = Parameters<
  typeof memoryClient.entries.search
>[0];

/**
 * `POST /api/memory/search` -- preview retrieval results.
 *
 * READ-ONLY, but exposed as a MUTATION because:
 *
 *   * The input is a live-typed query string, not a stable
 *     identifier. A query cache keyed by every keystroke would
 *     accumulate dozens of unused entries per session.
 *
 *   * The typical use (`SimilarMemoriesPanel` in the composer)
 *     debounces input and holds the result in local state --
 *     `useMutation`'s `mutate` / `data` shape fits this
 *     pattern; `useQuery`'s "re-render on cache change" does
 *     not.
 *
 *   * The panel wants a clear "searching..." state between
 *     keystroke and result. `useMutation.isPending` gives that
 *     for free without per-query-key cache management.
 *
 * Callers who need long-lived caching (e.g. a history of past
 * searches) should use `useQuery` directly with
 * `memoryKeys.entries.lists()` and a hand-built key.
 *
 * This mutation does NOT invalidate anything -- search is
 * read-only.
 */
export function useSearchMemories(
  options: BaseMutationOptions<
    SearchMemoryResponse,
    SearchMemoriesVariables
  > = {},
): UseMutationResult<
  SearchMemoryResponse,
  MemoryApiError,
  SearchMemoriesVariables
> {
  return useMutation<
    SearchMemoryResponse,
    MemoryApiError,
    SearchMemoriesVariables
  >({
    mutationFn: (body) => memoryClient.entries.search(body),
    onSuccess: options.onSuccess,
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Agentic search (Phase 6)
// ---------------------------------------------------------------------------

/**
 * Variables for `useAgenticSearch`.
 *
 * Extracted from the client method (not hand-written) so the
 * mutation cannot drift from `memoryClient.agentic.search`.
 */
export type AgenticSearchVariables = Parameters<
  typeof memoryClient.agentic.search
>[0];

/**
 * `POST /api/memory/search/agentic` -- answer a question from memory.
 *
 * A MUTATION for the same three reasons as `useSearchMemories`: the
 * input is a live question (not a stable key), the caller wants a
 * pending flag rather than cache re-renders, and the result is
 * consumed once.
 *
 * Differences from `useSearchMemories` that matter to callers:
 *
 *   * **It is slow.** Several LLM calls; expect seconds, not
 *     milliseconds. The UI should show a distinct
 *     "reasoning…" state, not the ordinary search spinner.
 *
 *   * **A failure is not an error.** The backend returns HTTP 200
 *     with `is_available: false` and the standard retrieval result
 *     under `fallback`. Only genuine transport/500 failures reject.
 *     So `onError` should be reserved for "the request itself
 *     broke", never for "agentic was unavailable".
 *
 *   * **It is cancellable.** Pass `options.onCancel`-friendly UI
 *     (or abort via the query client) if the user should be able to
 *     stop waiting; the backend's own timeout bounds the worst case.
 *
 * No cache invalidation: retrieval is read-only.
 */
export function useAgenticSearch(
  options: BaseMutationOptions<
    AgenticSearchResponse,
    AgenticSearchVariables
  > = {},
): UseMutationResult<
  AgenticSearchResponse,
  MemoryApiError,
  AgenticSearchVariables
> {
  return useMutation<
    AgenticSearchResponse,
    MemoryApiError,
    AgenticSearchVariables
  >({
    mutationFn: (body) => memoryClient.agentic.search(body),
    onSuccess: options.onSuccess,
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Project bulk operations
// ---------------------------------------------------------------------------

/**
 * Variables for `useBulkArchive` and `useBulkPurge`.
 */
type ProjectIdVariables = { projectId: string };

/**
 * `POST /api/memory/projects/{id}/bulk-archive` -- soft-archive
 * every active entry in the project.
 *
 * Idempotent. Requires `mode=read_write`; rejected with
 * `errorType: 'mode_read_only'` otherwise (the UI should gate
 * the button on the current mode).
 *
 * On success, invalidates the project's stats, the projects
 * list, and all entry lists (every list showing this project's
 * entries now needs a refetch to drop the archived ones).
 */
export function useBulkArchive(
  options: BaseMutationOptions<BulkArchiveResponse, ProjectIdVariables> = {},
): UseMutationResult<BulkArchiveResponse, MemoryApiError, ProjectIdVariables> {
  const queryClient = useQueryClient();
  return useMutation<BulkArchiveResponse, MemoryApiError, ProjectIdVariables>({
    mutationFn: ({ projectId }) => memoryClient.projects.bulkArchive(projectId),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.projects.stats(variables.projectId),
      });
      queryClient.invalidateQueries({ queryKey: memoryKeys.projects.list() });
      queryClient.invalidateQueries({ queryKey: memoryKeys.entries.lists() });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * `POST /api/memory/projects/{id}/bulk-purge?confirm=yes` --
 * PERMANENTLY delete every entry in the project.
 *
 * The most destructive operation in the API. The UI MUST gate
 * this behind a type-project-name-to-confirm dialog; the
 * `confirm=yes` query parameter (which the client sends) is a
 * low-friction guard against accidental direct calls, not a
 * substitute for user confirmation.
 *
 * On success, invalidates the project's stats, the projects
 * list, ALL entry lists, AND all entry details -- because every
 * cached detail for an entry in this project is now a 404. The
 * detail cache invalidation is what prevents a "phantom entry"
 * from briefly showing when the user navigates to a stale
 * detail URL.
 */
export function useBulkPurge(
  options: BaseMutationOptions<BulkPurgeResponse, ProjectIdVariables> = {},
): UseMutationResult<BulkPurgeResponse, MemoryApiError, ProjectIdVariables> {
  const queryClient = useQueryClient();
  return useMutation<BulkPurgeResponse, MemoryApiError, ProjectIdVariables>({
    mutationFn: ({ projectId }) => memoryClient.projects.bulkPurge(projectId),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.projects.stats(variables.projectId),
      });
      queryClient.invalidateQueries({ queryKey: memoryKeys.projects.list() });
      queryClient.invalidateQueries({ queryKey: memoryKeys.entries.lists() });
      queryClient.invalidateQueries({ queryKey: memoryKeys.entries.details() });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Import mutations
// ---------------------------------------------------------------------------

/**
 * The discriminated input for import preview/apply.
 *
 * `kind: 'file'` carries a `File` + target scope (multipart
 * upload). `kind: 'source'` carries an inline source (JSON
 * body). The discriminant is REQUIRED -- callers cannot
 * accidentally send a multipart request through the JSON code
 * path or vice versa.
 */
export type ImportMutationVariables =
  | { kind: 'file'; request: ImportMultipartRequest }
  | { kind: 'source'; request: ImportJsonRequest };

/**
 * Dispatch an import request to the correct client method
 * based on the discriminant.
 */
function dispatchImportPreview(
  variables: ImportMutationVariables,
): Promise<ImportPreviewResponse> {
  if (variables.kind === 'file') {
    return memoryClient.imports.previewFromFile(variables.request);
  }
  return memoryClient.imports.previewFromSource(variables.request);
}

function dispatchImportApply(
  variables: ImportMutationVariables,
): Promise<ImportApplyResponse> {
  if (variables.kind === 'file') {
    return memoryClient.imports.applyFromFile(variables.request);
  }
  return memoryClient.imports.applyFromSource(variables.request);
}

/**
 * `POST /api/memory/import/preview` -- parse without writing.
 *
 * Read-only. Returns the parsed candidates, `to_create` /
 * `to_skip` counts, and any warnings. The typical flow is
 * preview -> (user confirms) -> apply, but the backend does NOT
 * enforce this ordering -- a scripted caller can skip preview.
 * The wizard enforces it for the user.
 *
 * Does not invalidate anything (no writes occurred).
 */
export function useImportPreview(
  options: BaseMutationOptions<
    ImportPreviewResponse,
    ImportMutationVariables
  > = {},
): UseMutationResult<
  ImportPreviewResponse,
  MemoryApiError,
  ImportMutationVariables
> {
  return useMutation<
    ImportPreviewResponse,
    MemoryApiError,
    ImportMutationVariables
  >({
    mutationFn: dispatchImportPreview,
    onSuccess: options.onSuccess,
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * `POST /api/memory/import/apply` -- write every viable candidate.
 *
 * Best-effort: a single candidate's failure does not abort the
 * batch. The response reports per-candidate errors in
 * `result.errors`.
 *
 * On success, invalidates the entry lists and the projects list
 * (imported entries may create new project summaries). Does NOT
 * invalidate every project's stats -- imported entries could
 * affect many projects, and refetching all stats is wasteful;
 * the projects list invalidation will update the visible counts,
 * and a user who opens a specific project's detail page will
 * fetch its stats fresh anyway.
 */
export function useImportApply(
  options: BaseMutationOptions<ImportApplyResponse, ImportMutationVariables> = {},
): UseMutationResult<
  ImportApplyResponse,
  MemoryApiError,
  ImportMutationVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    ImportApplyResponse,
    MemoryApiError,
    ImportMutationVariables
  >({
    mutationFn: dispatchImportApply,
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({ queryKey: memoryKeys.entries.lists() });
      queryClient.invalidateQueries({ queryKey: memoryKeys.projects.list() });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Export mutations
// ---------------------------------------------------------------------------

/**
 * Variables for `useExportZip` and `useExportJson`.
 *
 * Mirrors `ExportQueryParams` but with `format` omitted -- the
 * hook determines it. This avoids the "caller passed
 * format='json' to useExportZip" mistake.
 */
type ExportVariables = Omit<ExportQueryParams, 'format'>;

/**
 * `GET /api/memory/export?format=zip` -- fetch the zip archive
 * as a `Blob` plus its metadata headers.
 *
 * Exposed as a MUTATION (not a query) because it triggers a
 * side-effect (a file download) and the result is a one-shot
 * `Blob` that should not be cached in React Query's memory
 * indefinitely. The wizard's success screen keeps the blob in
 * local state until the user navigates away.
 *
 * On success, does NOT invalidate anything -- export is
 * read-only.
 */
export function useExportZip(
  options: BaseMutationOptions<ExportDownloadResult, ExportVariables> = {},
): UseMutationResult<ExportDownloadResult, MemoryApiError, ExportVariables> {
  return useMutation<ExportDownloadResult, MemoryApiError, ExportVariables>({
    mutationFn: (params) =>
      memoryClient.exports.downloadZip({ ...params, format: 'zip' }),
    onSuccess: options.onSuccess,
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * `GET /api/memory/export?format=json` -- fetch the JSON
 * package as a typed body.
 *
 * Used by the "inspect before download" panel. The response
 * carries the raw `{manifest, memories}` package (NOT a
 * `status: "ok"` envelope -- see the `ExportJsonResponse`
 * documentation for that backend inconsistency).
 *
 * Exposed as a MUTATION for symmetry with `useExportZip` and
 * because the caller typically wants it once per "Inspect"
 * click, not on every render.
 */
export function useExportJson(
  options: BaseMutationOptions<ExportJsonResponse, ExportVariables> = {},
): UseMutationResult<ExportJsonResponse, MemoryApiError, ExportVariables> {
  return useMutation<ExportJsonResponse, MemoryApiError, ExportVariables>({
    mutationFn: (params) =>
      memoryClient.exports.downloadJson({ ...params, format: 'json' }),
    onSuccess: options.onSuccess,
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ===========================================================================
// Automation (Phase 3)
// ===========================================================================
//
// Invalidation policy (documented per-hook, summarized here):
//
//   * AI settings PATCH -> settings.get(), jobs.lists()
//     (enabling/disabling extraction tears down or rebuilds the
//     pipeline; the jobs list may have stale state-of-pipeline
//     from the user's POV).
//
//   * test-model -> NO invalidation (read-only probe).
//
//   * job retry / cancel -> jobs.lists(), jobs.detail(id).
//     The batch aggregate is ALSO invalidated when the job
//     belongs to a batch -- but the mutation does not know the
//     batch id at call time (the caller's job object does). The
//     caller who opens the batch panel after mutating a job
//     should invalidate `automation.batch.all()` themselves; the
//     hooks below invalidate only what they can prove.
//
//   * extract-from-session / extract-range -> jobs.lists().
//     No batch is touched (these are MANUAL jobs).
//
//   * batch create -> batch.lists(), batch.estimate(),
//     jobs.lists() (new jobs appear; the estimate is now stale).
//
//   * batch pause / resume / cancel -> batch.detail(id),
//     batch.lists(), jobs.lists() (job statuses changed).

// ---------------------------------------------------------------------------
// AI settings mutations
// ---------------------------------------------------------------------------

/**
 * Variables for `usePatchAISettings`.
 */
type PatchAISettingsVariables = MemoryAISettingsOverlay;

/**
 * `PATCH /api/memory/automation/settings` -- partial AI update.
 *
 * The backend deep-merges nested `extraction` / `consolidation`
 * objects, so callers may send a sparse patch. The route's
 * `batch_auto_schedule_hours` field is the ONLY nullable field
 * -- sending `null` clears it, omitting leaves it unchanged.
 *
 * On success, invalidates:
 *
 *   * `automation.settings.get()` -- the source of truth.
 *   * `automation.jobs.lists()` -- if the enable flags flipped,
 *     the extraction pipeline was torn down or rebuilt, which
 *     can invalidate in-flight job states.
 *
 * Does NOT invalidate batches: a batch row's historical
 * configuration snapshot (model, min_confidence) is independent
 * of the current AI settings, and the batch detail self-heals
 * on read.
 */
export function usePatchAISettings(
  options: BaseMutationOptions<
    AISettingsUpdateResponse,
    PatchAISettingsVariables
  > = {},
): UseMutationResult<
  AISettingsUpdateResponse,
  MemoryApiError,
  PatchAISettingsVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    AISettingsUpdateResponse,
    MemoryApiError,
    PatchAISettingsVariables
  >({
    mutationFn: (patch) => memoryClient.automation.settings.patch(patch),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.settings.get(),
      });
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.lists(),
      });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Test-model mutation
// ---------------------------------------------------------------------------

/**
 * Variables for `useTestModel`.
 */
type TestModelVariables = Parameters<
  typeof memoryClient.automation.settings.testModel
>[0];

/**
 * `POST /api/memory/automation/test-model` -- probe a model.
 *
 * **Response contract**: the mutation resolves with EITHER a
 * success envelope (`status: 'ok'`) OR an error envelope
 * (`status: 'error'`) -- both delivered via HTTP 200. The hook
 * does NOT throw on `status: 'error'`; the caller must narrow.
 *
 *     const result = await testModel.mutateAsync({...});
 *     if (result.status === 'ok') {
 *       showSuccess(result.latency_ms);
 *     } else {
 *       showError(result.message);
 *     }
 *
 * This is deliberate: a model-level failure (unreachable
 * provider, unparseable output) is a LEGITIMATE OUTCOME of the
 * probe -- not an infrastructure error. Callers need to display
 * it as "the model is misconfigured", not as "the API call
 * failed".
 *
 * No invalidation -- the probe does not change any state, only
 * the cost ledger (which is not cached in this feature).
 */
export function useTestModel(
  options: BaseMutationOptions<TestModelResponse, TestModelVariables> = {},
): UseMutationResult<TestModelResponse, MemoryApiError, TestModelVariables> {
  return useMutation<TestModelResponse, MemoryApiError, TestModelVariables>({
    mutationFn: (body) =>
      memoryClient.automation.settings.testModel(body),
    onSuccess: options.onSuccess,
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Job mutations
// ---------------------------------------------------------------------------

/**
 * Variables for `useRetryJob` and `useCancelJob`.
 */
type JobIdVariables = { jobId: string };

/**
 * `POST /api/memory/automation/jobs/{id}/retry` -- requeue a
 * terminal-failure job.
 *
 * The backend enforces `failed` / `cancelled` only; illegal
 * state transitions surface as `MemoryApiError` with
 * `errorType: 'invalid_state'` (HTTP 409). The UI should gate
 * the Retry button on `isRetryableJobStatus(job.status)` from
 * `types/jobs.ts` -- but the mutation still throws on the 409
 * path, so a stale button cannot silently fail.
 *
 * On success, invalidates `automation.jobs.detail(id)` (the
 * requeued job) and `automation.jobs.lists()` (any list
 * containing it).
 */
export function useRetryJob(
  options: BaseMutationOptions<JobRetryResponse, JobIdVariables> = {},
): UseMutationResult<JobRetryResponse, MemoryApiError, JobIdVariables> {
  const queryClient = useQueryClient();
  return useMutation<JobRetryResponse, MemoryApiError, JobIdVariables>({
    mutationFn: ({ jobId }) =>
      memoryClient.automation.jobs.retry(jobId),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.detail(variables.jobId),
      });
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.lists(),
      });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * `POST /api/memory/automation/jobs/{id}/cancel` -- cancel an
 * active job.
 *
 * Terminal states surface as `MemoryApiError` with
 * `errorType: 'invalid_state'` (HTTP 409). The UI should gate
 * the Cancel button on `isCancellableJobStatus(job.status)`.
 *
 * A RUNNING job CAN be cancelled: the worker notices on its
 * next heartbeat and skips the subsequent `complete()` call
 * (the outbox raises `JobNotFoundError`, which the worker
 * already handles as "the job disappeared; done").
 *
 * Same invalidation as `useRetryJob`.
 */
export function useCancelJob(
  options: BaseMutationOptions<JobCancelResponse, JobIdVariables> = {},
): UseMutationResult<JobCancelResponse, MemoryApiError, JobIdVariables> {
  const queryClient = useQueryClient();
  return useMutation<JobCancelResponse, MemoryApiError, JobIdVariables>({
    mutationFn: ({ jobId }) =>
      memoryClient.automation.jobs.cancel(jobId),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.detail(variables.jobId),
      });
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.lists(),
      });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Manual extraction mutations
// ---------------------------------------------------------------------------

/**
 * Variables for `useExtractFromSession`.
 */
type ExtractFromSessionVariables = Parameters<
  typeof memoryClient.automation.extract.fromSession
>[0];

/**
 * `POST /api/memory/automation/extract/from-session` -- enqueue
 * a MANUAL extraction of the whole session.
 *
 * The response is a discriminated union on `job === null`:
 *
 *   * `job: MemoryJob` -- a job was enqueued.
 *   * `job: null`      -- nothing to extract (200, with a
 *                          `message` explaining why).
 *
 * Neither case is an error. Both invalidate the jobs list --
 * even a no-op is worth re-fetching because it proves no job
 * was created (which is itself state the UI cares about).
 */
export function useExtractFromSession(
  options: BaseMutationOptions<
    ExtractFromSessionResponse,
    ExtractFromSessionVariables
  > = {},
): UseMutationResult<
  ExtractFromSessionResponse,
  MemoryApiError,
  ExtractFromSessionVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    ExtractFromSessionResponse,
    MemoryApiError,
    ExtractFromSessionVariables
  >({
    mutationFn: (body) =>
      memoryClient.automation.extract.fromSession(body),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.lists(),
      });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * Variables for `useExtractRange`.
 */
type ExtractRangeVariables = Parameters<
  typeof memoryClient.automation.extract.range
>[0];

/**
 * `POST /api/memory/automation/extract/range` -- enqueue a
 * MANUAL extraction of a specific turn range.
 *
 * Same `job: null` semantics and invalidation policy as
 * `useExtractFromSession`.
 */
export function useExtractRange(
  options: BaseMutationOptions<
    ExtractRangeResponse,
    ExtractRangeVariables
  > = {},
): UseMutationResult<
  ExtractRangeResponse,
  MemoryApiError,
  ExtractRangeVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    ExtractRangeResponse,
    MemoryApiError,
    ExtractRangeVariables
  >({
    mutationFn: (body) => memoryClient.automation.extract.range(body),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.lists(),
      });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ---------------------------------------------------------------------------
// Batch mutations
// ---------------------------------------------------------------------------

/**
 * Variables for `useCreateBatch`.
 */
type CreateBatchVariables = Parameters<
  typeof memoryClient.automation.batch.create
>[0];

/**
 * `POST /api/memory/automation/batch` -- create a batch.
 *
 * SYNCHRONOUS on the backend: the response returns only after
 * every job row has been inserted. A workspace with hundreds of
 * sessions can make this a multi-second POST; the UI should
 * show a spinner and disable the trigger button.
 *
 * On success, invalidates:
 *
 *   * `automation.batch.lists()` -- a new batch appears.
 *   * `automation.batch.estimate()` -- the estimate for the
 *     "next" batch is stale now.
 *   * `automation.jobs.lists()` -- new jobs were enqueued.
 */
export function useCreateBatch(
  options: BaseMutationOptions<
    BatchCreateResponse,
    CreateBatchVariables
  > = {},
): UseMutationResult<
  BatchCreateResponse,
  MemoryApiError,
  CreateBatchVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    BatchCreateResponse,
    MemoryApiError,
    CreateBatchVariables
  >({
    mutationFn: (body) => memoryClient.automation.batch.create(body),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.batch.lists(),
      });
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.batch.estimate(),
      });
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.lists(),
      });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * Variables for `usePauseBatch`, `useResumeBatch`, and
 * `useCancelBatch`.
 */
type BatchIdVariables = { batchId: string };

/**
 * Shared invalidation for the three batch mutating operations.
 *
 * All three change:
 *   * the batch's own status + timestamps (`batch.detail(id)`),
 *   * its position in the list (`batch.lists()`),
 *   * the statuses of many jobs (`jobs.lists()`).
 *
 * Centralized so the three hooks cannot drift.
 */
function invalidateBatchCascade(
  queryClient: ReturnType<typeof useQueryClient>,
  batchId: string,
): void {
  queryClient.invalidateQueries({
    queryKey: memoryKeys.automation.batch.detail(batchId),
  });
  queryClient.invalidateQueries({
    queryKey: memoryKeys.automation.batch.lists(),
  });
  queryClient.invalidateQueries({
    queryKey: memoryKeys.automation.jobs.lists(),
  });
}

/**
 * `POST /api/memory/automation/batch/{id}/pause` -- pause a
 * RUNNING batch.
 *
 * Pending / retry_pending jobs are cancelled with
 * `error_code='BATCH_PAUSED'`. Running jobs are left alone --
 * the current attempt finishes naturally, and no further jobs
 * are claimed until resume (or cancel).
 *
 * 409 if the batch is not currently `running`. The UI should
 * gate the Pause button on `isPausableBatchStatus(batch.status)`.
 */
export function usePauseBatch(
  options: BaseMutationOptions<
    BatchPauseResponse,
    BatchIdVariables
  > = {},
): UseMutationResult<BatchPauseResponse, MemoryApiError, BatchIdVariables> {
  const queryClient = useQueryClient();
  return useMutation<BatchPauseResponse, MemoryApiError, BatchIdVariables>({
    mutationFn: ({ batchId }) =>
      memoryClient.automation.batch.pause(batchId),
    onSuccess: (data, variables) => {
      invalidateBatchCascade(queryClient, variables.batchId);
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * `POST /api/memory/automation/batch/{id}/resume` -- resume a
 * PAUSED batch.
 *
 * Re-queues every job whose `error_code` is `BATCH_PAUSED`.
 * Jobs cancelled for other reasons (per-job cancel,
 * `SESSION_CHANGED` failures) stay cancelled.
 *
 * 409 if the batch is not currently `paused`. UI gate:
 * `isResumableBatchStatus(batch.status)`.
 */
export function useResumeBatch(
  options: BaseMutationOptions<
    BatchResumeResponse,
    BatchIdVariables
  > = {},
): UseMutationResult<BatchResumeResponse, MemoryApiError, BatchIdVariables> {
  const queryClient = useQueryClient();
  return useMutation<BatchResumeResponse, MemoryApiError, BatchIdVariables>({
    mutationFn: ({ batchId }) =>
      memoryClient.automation.batch.resume(batchId),
    onSuccess: (data, variables) => {
      invalidateBatchCascade(queryClient, variables.batchId);
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

/**
 * `POST /api/memory/automation/batch/{id}/cancel` -- cancel an
 * active batch.
 *
 * Cancels EVERY non-terminal job (pending, retry_pending,
 * running) with `error_code='BATCH_CANCELLED'`. There is no
 * graceful variant.
 *
 * 409 if the batch is already terminal. UI gate:
 * `isCancellableBatchStatus(batch.status)`.
 */
export function useCancelBatch(
  options: BaseMutationOptions<
    BatchCancelResponse,
    BatchIdVariables
  > = {},
): UseMutationResult<BatchCancelResponse, MemoryApiError, BatchIdVariables> {
  const queryClient = useQueryClient();
  return useMutation<BatchCancelResponse, MemoryApiError, BatchIdVariables>({
    mutationFn: ({ batchId }) =>
      memoryClient.automation.batch.cancel(batchId),
    onSuccess: (data, variables) => {
      invalidateBatchCascade(queryClient, variables.batchId);
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}

// ===========================================================================
// Embedding (Phase 4)
// ===========================================================================

/**
 * Variables for `useReindexEmbeddings`.
 *
 * Extracted from the client method's first parameter so the
 * hook cannot drift from the signature -- `ReindexRequest` (or
 * `undefined`, since the client has a `= {}` default).
 *
 * Callers may invoke `mutate()` with no argument (reindex every
 * active memory), or `mutate({ memory_ids: [...] })` to restrict
 * to specific memories. `memory_ids` has a backend-enforced max
 * of 1000 per request -- exceeding it returns HTTP 422 with
 * Pydantic validation errors, which surface as
 * `MemoryApiError` with `errorType: 'request_validation'` and
 * populated `fields`.
 */
type ReindexEmbeddingsVariables = Parameters<
  typeof memoryClient.embedding.reindex
>[0];

/**
 * `POST /api/memory/reindex` -- enqueue embedding jobs.
 *
 * Enqueues embedding jobs for the currently-selected
 * `embedding.model_key`. Idempotent for jobs already pending or
 * running under the same `(memory_id, model_key,
 * content_version)` tuple -- a repeated call shifts counts from
 * `enqueued` to `skipped` without creating duplicates.
 *
 * ERROR CONTRACT (all surface as `MemoryApiError` via
 * `classifyMemoryError`):
 *
 *   * `errorType: 'embedding_not_enabled'` (503)
 *     → `MemorySettings.embedding.enabled` is `false`. The UI
 *       should toast "Enable semantic search first" and focus
 *       the Section A toggle.
 *
 *   * `errorType: 'model_key_not_set'` (503)
 *     → `embedding.enabled === true` but `model_key === null`.
 *       Toast "Select an embedding model first" and focus the
 *       EmbeddingModelSelector.
 *
 *   * `errorType: 'model_key_not_in_snapshot'` (503)
 *     → the stored `model_key` no longer exists in the config
 *       catalog (provider rotated/removed the model). Toast
 *       "Selected model is no longer available" and prompt the
 *       user to re-select.
 *
 *   * `errorType: 'internal_error'` (500)
 *     → server-side bug (e.g. sqlite-vec write error). Generic
 *       "Reindex failed. Check server logs." -- do NOT retry
 *       automatically.
 *
 *   * `errorType: 'request_validation'` (422)
 *     → malformed `memory_ids` (too many, empty list,
 *       non-strings). Should not occur in normal UI flows --
 *       the caller builds the array from typed memory rows.
 *
 * Note on the response `ok` field: the endpoint uses the
 * boolean `{ok: true}` envelope (Phase 4 convention), not the
 * `{status: 'ok'}` envelope from Phase 1-3. `memoryFetch`
 * treats any non-2xx status as a throw, so a 200 always means
 * `ok: true` -- callers do not need to check the field.
 *
 * INVALIDATION CASCADE
 * --------------------
 * On success (`enqueued > 0` OR `enqueued === 0` with
 * `skipped > 0` -- both mean "jobs will be processed"):
 *
 *   * `memoryKeys.embedding.stats()` -- `pending_reindex`
 *     increases by `enqueued`; the next stats fetch reflects
 *     the new pending count. Consumers (ReindexPanel) poll
 *     this to render progress.
 *
 *   * `memoryKeys.automation.jobs.lists()` -- the new jobs
 *     appear in the extraction-jobs list with
 *     `job_type: 'embedding'`. The jobs list is the canonical
 *     place to inspect in-flight work.
 *
 * Does NOT invalidate:
 *   * `memoryKeys.settings.*` -- reindex READS the settings,
 *     it does not write them. (The model_key is validated
 *     server-side before any job is enqueued, so a successful
 *     response guarantees consistency with the current
 *     settings.)
 *   * `memoryKeys.entries.*` -- reindex does not modify
 *     entry content or membership, only adds embedding
 *     metadata.
 *   * `configKeys.embeddingModels()` -- the catalog is a
 *     static list of available models; populating embeddings
 *     for some of them does not change availability.
 *
 * TYPICAL FLOW
 * ------------
 *     const reindex = useReindexEmbeddings({
 *       onSuccess: (data) => {
 *         showToast.success(
 *           `Reindex queued: ${data.enqueued} enqueued, ` +
 *           `${data.skipped} skipped`
 *         );
 *       },
 *       onError: (err) => {
 *         const hint = uxHintFor(err);   // from types/errors.ts
 *         showToast.error(err.message);
 *         if (hint.suggestSettings) focusSectionA();
 *       },
 *     });
 *     // ...
 *     reindex.mutate({});   // reindex everything
 *     // or:
 *     reindex.mutate({ memory_ids: ['mem_abc', 'mem_def'] });
 */
export function useReindexEmbeddings(
  options: BaseMutationOptions<
    ReindexResponse,
    ReindexEmbeddingsVariables
  > = {},
): UseMutationResult<
  ReindexResponse,
  MemoryApiError,
  ReindexEmbeddingsVariables
> {
  const queryClient = useQueryClient();
  return useMutation<
    ReindexResponse,
    MemoryApiError,
    ReindexEmbeddingsVariables
  >({
    // `body ?? {}` -- the client already defaults its `body`
    // parameter to `{}`, but being explicit here keeps the
    // call site behavior unambiguous even if that default is
    // ever removed from the client signature.
    mutationFn: (body) =>
      memoryClient.embedding.reindex(body ?? {}),
    onSuccess: (data, variables) => {
      queryClient.invalidateQueries({
        queryKey: memoryKeys.embedding.stats(),
      });
      queryClient.invalidateQueries({
        queryKey: memoryKeys.automation.jobs.lists(),
      });
      options.onSuccess?.(data, variables);
    },
    onError: options.onError,
    onSettled: options.onSettled,
  });
}
