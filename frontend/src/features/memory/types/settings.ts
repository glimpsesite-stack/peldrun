// src/features/memory/types/settings.ts

/**
 * Memory subsystem configuration types.
 *
 * This module mirrors ``peldrun.memory.domain.settings`` one-to-one.
 * Everything here is snake_case to match the wire payload produced
 * by the backend's ``model_dump(mode="json")`` -- there is no
 * transformation layer (see ``entry.ts`` for the same policy).
 *
 * Boundary with ``responses.ts``:
 *
 *   * THIS module -- pure domain shapes (the ``MemorySettings``
 *     object and its sub-parts).
 *
 *   * ``responses.ts`` -- HTTP envelopes that WRAP these shapes
 *     (``{status: 'ok', operation: 'settings_get', settings: {...}}``).
 *
 * Keeping the two separate means UI logic can consume
 * ``MemorySettings`` directly (e.g. as a prop type) without
 * threading through the envelope, and endpoint callers can
 * narrow to the envelope without depending on domain details
 * they don't care about.
 *
 * Constraint source of truth
 * --------------------------
 *
 * Every numeric range below (``default_limit`` 1..50,
 * ``max_tokens`` 64..32768, etc.) mirrors the Pydantic
 * ``Field(ge=..., le=...)`` declarations on the backend's
 * ``MemorySettings`` and ``MemoryTokenBudget`` models. These
 * bounds are exported as constants below so that:
 *
 *   * ``features/memory/lib/validators.ts`` can compose Zod
 *     schemas from the same constants, guaranteeing client-side
 *     pre-validation matches server-side post-validation.
 *
 *   * ``SettingsForm`` can build ``<input min= max=>`` attributes
 *     from them without hardcoding literals.
 *
 *   * A backend change to a bound that is not reflected here
 *     becomes a visible bug in the first validation attempt
 *     (server rejects a value the client thought was valid),
 *     not a silent data corruption.
 */

import type { MemoryMode } from './enums';
import type {
  MemoryAISettings,
  MemoryAISettingsOverlay,
} from './ai';
import type {
  MemoryEmbeddingSettings,
  MemoryRetrievalSettings,
} from './embedding';

// ---------------------------------------------------------------------------
// Constraint constants
// ---------------------------------------------------------------------------

/**
 * Numeric bounds mirrored from the backend's Pydantic
 * ``Field(ge=..., le=...)`` declarations.
 *
 * Grouped here (not in ``constants.ts``) because they are part of
 * the settings contract, not generic UI constants. ``constants.ts``
 * re-exports them for convenience; consumers may import from
 * either module.
 *
 * NOTE: bounds for the Phase 4 retrieval sub-tree live in
 * `embedding.ts` as `MemoryRetrievalBounds` -- they are NOT
 * duplicated here because they describe a different model
 * (`MemoryRetrievalSettings`), not a top-level field of
 * `MemorySettings`. Validators that need both should import
 * from both modules.
 */
export const MemorySettingsBounds = {
  default_limit: { min: 1, max: 50 },
  min_relevance_score: { min: 0.0, max: 1.0 },
  boost_pinned: { min: 1.0, max: 10.0 },
  auto_extraction_min_turns: { min: 1, max: 100 },
  token_budget_max_tokens: { min: 64, max: 32768 },
  token_budget_warn_threshold: { min: 0.0, max: 1.0 },
} as const;

// ---------------------------------------------------------------------------
// Token budget
// ---------------------------------------------------------------------------

/**
 * Token allocation for retrieved memory injected into a prompt.
 *
 * Mirrors ``peldrun.memory.domain.settings.MemoryTokenBudget``.
 *
 * The budget fitter (see the spec's retrieval pipeline, stage 4)
 * uses these numbers to decide which retrieved entries survive.
 *
 * Field-by-field:
 *
 *   * ``max_tokens``               -- hard ceiling on total tokens
 *                                     spent on the memory section
 *                                     of the system prompt.
 *
 *   * ``reserved_for_user_scope``  -- Phase-1 reservation: this
 *                                     many tokens are set aside
 *                                     for ``user``-scope entries
 *                                     BEFORE any other scope is
 *                                     fitted. Prevents a chatty
 *                                     project from starving the
 *                                     user's own preferences.
 *                                     MUST be <= ``max_tokens``.
 *
 *   * ``warn_threshold``           -- fraction of ``max_tokens``
 *                                     above which the usage
 *                                     dashboard emits a warning
 *                                     ("you are near the budget").
 *                                     Typical: 0.8.
 *
 * The ``reserved_for_user_scope <= max_tokens`` invariant is
 * enforced by the backend's Pydantic validator; the client-side
 * Zod schema MUST enforce the same before submission (see
 * ``features/memory/lib/validators.ts``).
 */
export interface MemoryTokenBudget {
  max_tokens: number;
  reserved_for_user_scope: number;
  warn_threshold: number;
}

/**
 * A "settings overlay" -- a partial settings object in which
 * ``token_budget`` is ALSO partial.
 *
 * This is the shape accepted by ``computeEffectiveOverride`` and
 * ``mergeProjectSettings``. It intentionally differs from
 * ``Partial<MemorySettings>``, which would force callers to
 * supply a COMPLETE ``MemoryTokenBudget`` when they only want to
 * override one of its sub-fields (e.g. raise ``max_tokens`` but
 * leave ``reserved_for_user_scope`` at global).
 *
 * Both ``MemoryProjectOverride`` (per-project PUT body) and
 * ``MemorySettingsPatch`` (global PATCH body) are structurally
 * assignable to this type. Keeping them as separate named
 * interfaces documents the INTENT of each call site, while
 * routing both through this shared shape keeps the helpers
 * usable from either. TS structural typing makes the
 * assignment free -- no runtime cast, no wrapper.
 */
export interface MemorySettingsOverlay {
  mode?: MemoryMode;
  provider_id?: string;
  default_limit?: number;
  min_relevance_score?: number;
  boost_pinned?: number;
  token_budget?: Partial<MemoryTokenBudget>;
  /**
   * AI extraction configuration overlay.
   *
   * `MemoryAISettingsOverlay` (deep partial) because a caller
   * may supply only the fields they want to change -- including
   * only PART of a nested `extraction` or `consolidation`
   * object. `computeEffectiveOverride` and
   * `mergeProjectSettings` expand it to a FULL
   * `MemoryAISettings` via `mergeMemoryAISettings` -- see the
   * helper's JSDoc for why sending the full object (not a
   * partial) is mandatory on the wire.
   */
  ai?: MemoryAISettingsOverlay;
  /**
   * Phase 4 embedding overlay (delta form).
   *
   * `Partial<MemoryEmbeddingSettings>` because the backend's
   * PATCH uses a SHALLOW merge on the `embedding` sub-tree:
   * sending `{enabled: true}` alone preserves the currently-
   * stored `model_key`, and sending `{model_key: "..."}` alone
   * preserves `enabled`. The two fields are independently
   * patchable by contract (see `MemorySettingsPatch` JSDoc).
   *
   * `MemoryEmbeddingSettings` has only two fields (`enabled`,
   * `model_key`), so the flat `Partial<>` is sufficient --
   * unlike `ai`, there are no nested sub-objects requiring a
   * deep-partial helper.
   */
  embedding?: Partial<MemoryEmbeddingSettings>;
  /**
   * Phase 4 retrieval overlay (delta form).
   *
   * Same shallow-merge contract as `embedding`: any subset of
   * the six retrieval fields may be sent independently.
   */
  retrieval?: Partial<MemoryRetrievalSettings>;
  auto_extraction_enabled?: boolean;
  auto_extraction_min_turns?: number;
}

// ---------------------------------------------------------------------------
// Project override (delta form)
// ---------------------------------------------------------------------------

/**
 * A per-project delta applied on top of the global settings.
 *
 * Mirrors the ``project_overrides[project_id]`` value.
 *
 * Design:
 *
 *   * Every field is OPTIONAL. Absent fields inherit from the
 *     global settings -- the ``effective_for_project`` merge on
 *     the backend only walks fields that are present here.
 *
 *   * ``project_overrides`` itself is FORBIDDEN here -- a
 *     per-project override cannot contain a nested
 *     ``project_overrides`` map. The backend's
 *     ``MemorySettings`` validator rejects the nesting, and the
 *     memory_settings router has a defense-in-depth check for it.
 *
 *   * This is the shape accepted by
 *     ``PUT /api/memory/settings/projects/{id}``.
 *
 * Notes on what the API returns
 * -----------------------------
 *
 * The ``GET /api/memory/settings/projects/{id}`` response's
 * ``override`` field is typed as ``Partial<MemorySettings>`` (see
 * ``responses.ts``), not as this interface -- because the backend
 * MAY return a full settings object (with defaults filled in) as
 * the override, depending on how ``MemorySettings`` stores the
 * delta. This interface describes what the frontend SENDS in a
 * ``PUT`` body, which is unambiguously delta-only.
 *
 * If you need to know which fields are actually different from
 * global, use ``computeEffectiveOverride`` below rather than
 * trusting ``Object.keys`` of the response's ``override``.
 */
export interface MemoryProjectOverride {
  mode?: MemoryMode;
  provider_id?: string;
  default_limit?: number;
  min_relevance_score?: number;
  boost_pinned?: number;
  token_budget?: Partial<MemoryTokenBudget>;
  /**
   * Per-project AI override. Same semantics as the global patch:
   * fields present here win over the global values for this
   * project only. Sent to the backend as a FULL
   * `MemoryAISettings` (expanded via `mergeMemoryAISettings`),
   * not as a partial.
   */
  ai?: MemoryAISettingsOverlay;
  /**
   * Per-project embedding override (Phase 4).
   *
   * Same delta-merge semantics as the global `embedding`
   * overlay -- fields present here win over the global values
   * for this project only, and fields absent inherit from
   * global.
   */
  embedding?: Partial<MemoryEmbeddingSettings>;
  /**
   * Per-project retrieval override (Phase 4).
   */
  retrieval?: Partial<MemoryRetrievalSettings>;
  auto_extraction_enabled?: boolean;
  auto_extraction_min_turns?: number;
}

// ---------------------------------------------------------------------------
// Settings patch (global PATCH body)
// ---------------------------------------------------------------------------

/**
 * The shape accepted by ``PATCH /api/memory/settings``.
 *
 * Identical field set to ``MemoryProjectOverride`` at the type
 * level, but semantically distinct:
 *
 *   * ``MemoryProjectOverride`` -- "customize these fields for
 *     ONE project".
 *
 *   * ``MemorySettingsPatch``  -- "change these fields GLOBALLY,
 *     for every project that has no override".
 *
 * Kept as separate named types (rather than a type alias) so that
 * handler signatures read as their intent:
 * ``patchSettings(patch: MemorySettingsPatch)`` vs
 * ``putProjectOverride(id, override: MemoryProjectOverride)``.
 *
 * Deliberately EXCLUDES ``project_overrides`` -- the whitelist on
 * the backend's ``_PATCHABLE_FIELDS`` omits it because per-project
 * overrides are managed by the dedicated endpoints, not by a bulk
 * PATCH. Sending it via PATCH would bypass per-project merge
 * semantics.
 *
 * An EMPTY patch (``{}``) is rejected by the backend (400
 * ``empty_update``). The UI must guard against sending an empty
 * patch -- see ``SettingsForm`` for the "nothing changed"
 * short-circuit.
 */
export interface MemorySettingsPatch {
  mode?: MemoryMode;
  provider_id?: string;
  default_limit?: number;
  min_relevance_score?: number;
  boost_pinned?: number;
  token_budget?: Partial<MemoryTokenBudget>;
  /**
   * AI extraction configuration patch (Phase 1).
   *
   * IMPORTANT: on the wire, this MUST be the FULL
   * `MemoryAISettings` object, not a partial. Reason: the
   * backend's `MemorySettingsManager.update()` applies the
   * patch via `merged_data.update(kwargs)` -- a SHALLOW
   * replace. Sending a partial `ai` object would therefore
   * cause Pydantic's `MemoryAISettings.model_validate` to
   * fill every omitted field with its DEFAULT (not with the
   * current value), silently resetting fields the caller did
   * not intend to touch.
   *
   * `computeEffectiveOverride` enforces this by building the
   * full object via `mergeMemoryAISettings` before including
   * it in the returned patch. Do NOT construct a patch with
   * a partial `ai` by hand -- always go through
   * `computeEffectiveOverride`.
   *
   * The type is `MemoryAISettingsOverlay` (deep-partial) so
   * that callers who go through `computeEffectiveOverride`
   * can pass a partial nested object. On the wire, the field
   * is always the FULL `MemoryAISettings` -- the compiler type
   * is deliberately looser than the wire contract so the
   * helper's job (expanding the partial) type-checks.
   */
  ai?: MemoryAISettingsOverlay;
  /**
   * Phase 4 embedding patch (delta form).
   *
   * DELTA-merge semantics on the wire -- see the module-level
   * note under `MemorySettings.embedding` for the exact
   * behavior. In short: sending `{embedding: {enabled: true}}`
   * alone preserves the currently-stored `model_key`, and
   * sending `{embedding: {model_key: "..."}}` alone preserves
   * the currently-stored `enabled`. The two fields are
   * independently patchable by contract.
   *
   * The backend rejects an empty `embedding: {}` object with
   * HTTP 422 (`empty_update`) -- send at least one field, or
   * omit the key entirely.
   */
  embedding?: Partial<MemoryEmbeddingSettings>;
  /**
   * Phase 4 retrieval patch (delta form).
   *
   * Same delta-merge contract as `embedding`: any subset of
   * the six retrieval fields may be sent independently, and
   * the backend shallow-merges into the current retrieval
   * subtree. Sending an empty `retrieval: {}` is rejected with
   * HTTP 422.
   */
  retrieval?: Partial<MemoryRetrievalSettings>;
  auto_extraction_enabled?: boolean;
  auto_extraction_min_turns?: number;
}

// ---------------------------------------------------------------------------
// Global settings (full shape)
// ---------------------------------------------------------------------------

/**
 * The complete memory subsystem configuration.
 *
 * Mirrors ``peldrun.memory.domain.settings.MemorySettings``.
 *
 * Every field has a backend default, so ``MemorySettings`` is
 * always fully populated -- partial states exist only DURING the
 * editing process (as a ``MemorySettingsPatch`` or a
 * ``MemoryProjectOverride``).
 *
 * Field notes (deeper per-field contract lives on the linked
 * sub-interfaces):
 *
 *   * ``mode`` -- the master switch. Every UI gate derives from
 *     ``mode``. See ``useMemoryMode`` for the derived helpers
 *     (``canRead``, ``canWrite``, ``isDisabled``).
 *
 *   * ``provider_id`` -- the storage backend's registered id.
 *     Currently only ``"sqlite_fts"`` ships; future providers are
 *     still ``string`` here so the UI does not need to change
 *     when a new one lands.
 *
 *   * ``default_limit`` -- the ranker's default ``LIMIT`` (how
 *     many entries retrieval considers). Range 1..50. Feeds the
 *     ranker and the search preview's ``token_budget`` response
 *     calculation.
 *
 *   * ``min_relevance_score`` -- entries whose normalized
 *     relevance falls below this threshold are dropped by the
 *     ranker BEFORE the budget fitter runs. Range 0..1.
 *
 *   * ``boost_pinned`` -- multiplier applied to pinned entries'
 *     final score. Range 1.0..10.0. A value of 1.0 disables the
 *     pin boost without disabling pinning itself.
 *
 *   * ``token_budget`` -- see ``MemoryTokenBudget``.
 *
 *   * ``auto_extraction_enabled`` -- whether the background
 *     extraction job runs after conversations. Off by default
 *     for privacy/accounting reasons.
 *
 *   * ``auto_extraction_min_turns`` -- minimum number of user/
 *     assistant turn pairs before extraction is attempted.
 *     Range 1..100.
 *
 *   * ``project_overrides`` -- a map from ``project_id`` to
 *     ``MemoryProjectOverride``. Only projects that have at
 *     least one customized field appear as keys. The empty map
 *     means "no project has an override; everyone uses global
 *     settings".
 */
export interface MemorySettings {
  mode: MemoryMode;
  provider_id: string;
  default_limit: number;
  min_relevance_score: number;
  boost_pinned: number;
  token_budget: MemoryTokenBudget;
  /**
   * AI extraction subsystem configuration (Phase 1).
   *
   * See `MemoryAISettings` in `./ai.ts` for the full gating
   * contract. This field is ALWAYS present in the response
   * (backend fills defaults), so UI code can read
   * `settings.ai.enabled` without null checks.
   */
  ai: MemoryAISettings;
  /**
   * Semantic search configuration (Phase 4).
   *
   * See `MemoryEmbeddingSettings` in `./embedding.ts` for the
   * full field contract. Always present in the response
   * (backend fills defaults) -- `enabled` defaults to `false`
   * and `model_key` to `null`, which is a legitimate
   * "disabled" state, not an error.
   *
   * DELTA-MERGE SEMANTICS (informs the patch contract):
   *
   * `PATCH /api/memory/settings` accepts a partial `embedding`
   * sub-object and shallow-merges it into the current value.
   * The two fields are independently patchable:
   *
   *   * `{embedding: {enabled: true}}` alone PRESERVES the
   *     currently-stored `model_key` (which may still be
   *     `null` -- that is the "enabled but not yet configured"
   *     state, and the UI MUST warn when it occurs).
   *
   *   * `{embedding: {model_key: "openai::..."}}` alone
   *     PRESERVES the currently-stored `enabled` flag.
   *
   * This is why `MemorySettingsPatch.embedding` and
   * `MemorySettingsOverlay.embedding` are typed as
   * `Partial<MemoryEmbeddingSettings>` rather than the full
   * shape -- a caller changing one field MUST NOT be forced to
   * echo the other back.
   */
  embedding: MemoryEmbeddingSettings;
  /**
   * Retrieval strategy and fusion tuning (Phase 4).
   *
   * See `MemoryRetrievalSettings` in `./embedding.ts` for the
   * full field contract. Always present in the response
   * (backend fills defaults).
   *
   * IMPORTANT behavioral note for the UI:
   *
   * `retrieval.mode` may be set to a semantic mode (`vector`,
   * `hybrid`, `automatic`) even when `embedding.enabled` is
   * `false`. This is a LEGITIMATE state -- it lets a user
   * pre-configure their preferred strategy before enabling
   * embeddings. The backend silently falls back to `lexical`
   * at retrieval time in that case, and logs the fallback at
   * INFO level only.
   *
   * The settings UI MUST NOT reject or block this
   * combination -- instead, show a warning explaining that
   * the semantic mode will not take effect until embeddings
   * are enabled.
   */
  retrieval: MemoryRetrievalSettings;
  /**
   * @deprecated Phase-0 placeholder for the heuristic extractor.
   * Superseded by `ai.enabled` + `ai.extraction_enabled` for the
   * LLM extractor. Kept for backward compatibility with
   * `MemorySettingsManager`'s Pydantic schema and existing
   * settings files; removed in a future cleanup once no
   * deployments reference it.
   */
  auto_extraction_enabled: boolean;
  /**
   * @deprecated See `auto_extraction_enabled`.
   */
  auto_extraction_min_turns: number;
  project_overrides: Record<string, MemoryProjectOverride>;
}

// ---------------------------------------------------------------------------
// Patchable field whitelist
// ---------------------------------------------------------------------------

/**
 * The exact set of fields a caller may include in a
 * ``PATCH /api/memory/settings`` body.
 *
 * Mirrors the backend's ``_PATCHABLE_FIELDS`` frozenset.
 *
 * Type constraint: ``(keyof MemorySettingsPatch)[]`` guarantees
 * every entry here is a real field on the patch type -- adding a
 * typo or a stale name is a compile error. Conversely,
 * ``as const satisfies`` ensures the constant itself is treated
 * as a literal tuple by the type system, so callers can use it in
 * ``Object.fromEntries`` and exhaustive checks.
 */
export const MEMORY_SETTINGS_PATCHABLE_FIELDS = [
  'mode',
  'provider_id',
  'default_limit',
  'min_relevance_score',
  'boost_pinned',
  'token_budget',
  'ai',
  'embedding',
  'retrieval',
  'auto_extraction_enabled',
  'auto_extraction_min_turns',
] as const satisfies readonly (keyof MemorySettingsPatch)[];

/**
 * Type-level assertion that ``MEMORY_SETTINGS_PATCHABLE_FIELDS``
 * covers every key of ``MemorySettingsPatch``.
 *
 * If a future field is added to ``MemorySettingsPatch`` but
 * forgotten in the whitelist, the ``_MissingPatchField`` helper
 * below will fail to be ``never`` and this module will not
 * compile. This is the "compile-time lint" pattern that keeps
 * the two declarations synchronized without a runtime test.
 */
type _MissingPatchField = Exclude<
  keyof MemorySettingsPatch,
  (typeof MEMORY_SETTINGS_PATCHABLE_FIELDS)[number]
>;
// If any key is missing above, this line fails to compile.
const _patchableFieldsAreExhaustive: _MissingPatchField extends never
  ? true
  : never = true;
// Reference to silence "unused" warnings without changing the
// value's semantics.
void _patchableFieldsAreExhaustive;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Merge a partial AI settings override onto a complete base.
 *
 * Used by `computeEffectiveOverride` and `mergeProjectSettings`
 * to expand a `Partial<MemoryAISettings>` into a full
 * `MemoryAISettings` before comparison or transmission.
 *
 * Why deep merge for `extraction` and `consolidation`
 * ---------------------------------------------------
 * These two sub-objects are `MemoryTaskModel` instances with
 * multiple fields. A caller who changes only
 * `extraction.provider_id` should NOT lose
 * `extraction.max_output_tokens` in the merged result -- the
 * shallow spread `{...base, ...override}` would lose the nested
 * fields when `override.extraction` is present. Merging
 * field-by-field inside each nested object preserves the
 * unchanged fields.
 *
 * All other fields on `MemoryAISettings` are scalars or
 * booleans, so the shallow spread handles them correctly.
 *
 * Returns a NEW object; neither `base` nor `override` is
 * mutated.
 */
export function mergeMemoryAISettings(
  base: MemoryAISettings,
  override: MemoryAISettingsOverlay,
): MemoryAISettings {
  return {
    ...base,
    ...override,
    extraction: override.extraction
      ? { ...base.extraction, ...override.extraction }
      : base.extraction,
    consolidation: override.consolidation
      ? { ...base.consolidation, ...override.consolidation }
      : base.consolidation,
    // Phase 6: same deep-merge rule as the two task models above.
    // The overlay types `agentic` as `Partial<MemoryTaskModel>`, so
    // a plain `...override` would make the result's `agentic`
    // potentially-partial and fail to satisfy `MemoryAISettings`
    // (which requires every field). Merging field-by-field both
    // fixes the type and preserves the untouched siblings.
    agentic: override.agentic
      ? { ...base.agentic, ...override.agentic }
      : base.agentic,
  };
}

/**
 * Deep-partial compare: return only the fields of ``override``
 * whose values DIFFER from the corresponding fields of ``base``.
 *
 * Rationale: the backend's project-settings response MAY return
 * a full ``MemorySettings`` object in its ``override`` field
 * (with default values filled in), OR only the explicitly-set
 * delta -- the exact behavior depends on how the backend's
 * ``with_project_override`` stores the delta internally. Rather
 * than assume one, the UI computes the EFFECTIVE override by
 * comparing against the global settings.
 *
 * The returned object is safe to display as "what you have
 * customized for this project". Fields whose effective override
 * value matches the global default are omitted, so a user who
 * re-set ``default_limit`` to the same value as global sees
 * "no customization" (which matches their intent).
 *
 * ``token_budget`` is compared field-by-field, not by object
 * identity: a project that only overrides ``max_tokens`` but
 * leaves ``reserved_for_user_scope`` and ``warn_threshold`` at
 * their global values returns
 * ``{ token_budget: { max_tokens: <new> } }`` -- not the whole
 * budget object.
 */
export function computeEffectiveOverride(
  base: MemorySettings,
  override: MemorySettingsOverlay,
): MemoryProjectOverride {
  const effective: MemoryProjectOverride = {};

  if (
    override.mode !== undefined &&
    override.mode !== base.mode
  ) {
    effective.mode = override.mode;
  }
  if (
    override.provider_id !== undefined &&
    override.provider_id !== base.provider_id
  ) {
    effective.provider_id = override.provider_id;
  }
  if (
    override.default_limit !== undefined &&
    override.default_limit !== base.default_limit
  ) {
    effective.default_limit = override.default_limit;
  }
  if (
    override.min_relevance_score !== undefined &&
    override.min_relevance_score !== base.min_relevance_score
  ) {
    effective.min_relevance_score = override.min_relevance_score;
  }
  if (
    override.boost_pinned !== undefined &&
    override.boost_pinned !== base.boost_pinned
  ) {
    effective.boost_pinned = override.boost_pinned;
  }
  if (override.token_budget !== undefined) {
    const budgetDelta: Partial<MemoryTokenBudget> = {};
    if (
      override.token_budget.max_tokens !== undefined &&
      override.token_budget.max_tokens !== base.token_budget.max_tokens
    ) {
      budgetDelta.max_tokens = override.token_budget.max_tokens;
    }
    if (
      override.token_budget.reserved_for_user_scope !== undefined &&
      override.token_budget.reserved_for_user_scope !==
        base.token_budget.reserved_for_user_scope
    ) {
      budgetDelta.reserved_for_user_scope =
        override.token_budget.reserved_for_user_scope;
    }
    if (
      override.token_budget.warn_threshold !== undefined &&
      override.token_budget.warn_threshold !==
        base.token_budget.warn_threshold
    ) {
      budgetDelta.warn_threshold = override.token_budget.warn_threshold;
    }
    if (Object.keys(budgetDelta).length > 0) {
      effective.token_budget = budgetDelta;
    }
  }
  if (
    override.auto_extraction_enabled !== undefined &&
    override.auto_extraction_enabled !== base.auto_extraction_enabled
  ) {
    effective.auto_extraction_enabled = override.auto_extraction_enabled;
  }
  if (
    override.auto_extraction_min_turns !== undefined &&
    override.auto_extraction_min_turns !==
      base.auto_extraction_min_turns
  ) {
    effective.auto_extraction_min_turns =
      override.auto_extraction_min_turns;
  }
  // ai: expand the partial override to a FULL MemoryAISettings,
  // then compare against the current full object. The full
  // object (not the partial) is what gets included in the patch
  // -- see MemorySettingsPatch's JSDoc for the reason.
  if (override.ai !== undefined) {
    const mergedAi = mergeMemoryAISettings(base.ai, override.ai);
    if (JSON.stringify(mergedAi) !== JSON.stringify(base.ai)) {
      effective.ai = mergedAi;
    }
  }
  // embedding: expand the partial overlay to a FULL
  // MemoryEmbeddingSettings, then compare against base. The
  // explicit `!== undefined` check on `model_key` is what lets
  // a caller send `{model_key: null}` to CLEAR the stored key
  // -- a plain `??` would treat `null` as "keep base value",
  // which would make clearing impossible.
  if (override.embedding !== undefined) {
    const mergedEmbedding: MemoryEmbeddingSettings = {
      enabled:
        override.embedding.enabled !== undefined
          ? override.embedding.enabled
          : base.embedding.enabled,
      model_key:
        override.embedding.model_key !== undefined
          ? override.embedding.model_key
          : base.embedding.model_key,
    };
    if (
      JSON.stringify(mergedEmbedding) !==
      JSON.stringify(base.embedding)
    ) {
      effective.embedding = mergedEmbedding;
    }
  }
  // retrieval: same expansion-then-compare as embedding. All
  // six fields are primitives (no nested objects), so a flat
  // spread of the overlay over base suffices.
  if (override.retrieval !== undefined) {
    const mergedRetrieval: MemoryRetrievalSettings = {
      ...base.retrieval,
      ...override.retrieval,
    };
    if (
      JSON.stringify(mergedRetrieval) !==
      JSON.stringify(base.retrieval)
    ) {
      effective.retrieval = mergedRetrieval;
    }
  }

  return effective;
}

/**
 * Return ``true`` when a patch would be a no-op relative to the
 * current settings.
 *
 * Used by ``SettingsForm`` to disable the Save button and to
 * short-circuit before hitting ``PATCH /settings`` -- the
 * backend rejects empty patches with ``400 empty_update``, and
 * "changed a value and changed it back" is a common user path
 * that should not produce an error toast.
 *
 * Semantics: a patch is "no-op" when ``computeEffectiveOverride``
 * of it against the current settings is empty. Because that
 * helper compares per-field (including nested
 * ``token_budget``), sending ``{default_limit: <current value>}``
 * is correctly recognized as a no-op.
 */
export function isNoOpPatch(
  current: MemorySettings,
  patch: MemorySettingsPatch,
): boolean {
  return Object.keys(computeEffectiveOverride(current, patch)).length === 0;
}

/**
 * Merge a project override onto the base settings to produce the
 * effective settings for one project.
 *
 * Mirrors the backend's ``MemorySettings.effective_for_project``
 * behavior. The frontend uses this primarily for OPTIMISTIC UI --
 * after a ``PUT`` succeeds, the freshly-returned ``effective``
 * from the response is authoritative; this helper exists so the
 * UI can preview the result during the request (e.g. disable a
 * field based on the merged mode) without waiting.
 *
 * Only defined fields of the override participate in the merge;
 * undefined fields inherit from ``base`` verbatim. ``token_budget``
 * is merged field-by-field (a partial override of the budget
 * leaves the unspecified budget fields at their base values).
 *
 * Returns a NEW object -- ``base`` and ``override`` are not
 * mutated.
 */
export function mergeProjectSettings(
  base: MemorySettings,
  override: MemorySettingsOverlay,
): MemorySettings {
  const token_budget: MemoryTokenBudget = {
    max_tokens:
      override.token_budget?.max_tokens ?? base.token_budget.max_tokens,
    reserved_for_user_scope:
      override.token_budget?.reserved_for_user_scope ??
      base.token_budget.reserved_for_user_scope,
    warn_threshold:
      override.token_budget?.warn_threshold ??
      base.token_budget.warn_threshold,
  };

  return {
    mode: override.mode ?? base.mode,
    provider_id: override.provider_id ?? base.provider_id,
    default_limit: override.default_limit ?? base.default_limit,
    min_relevance_score:
      override.min_relevance_score ?? base.min_relevance_score,
    boost_pinned: override.boost_pinned ?? base.boost_pinned,
    token_budget,
    // ai: when the override supplies any sub-field, deep-merge it
    // onto the base; otherwise inherit the base ai verbatim. The
    // result is always a FULL MemoryAISettings (never partial) --
    // consumers can rely on every field being present.
    ai: override.ai
      ? mergeMemoryAISettings(base.ai, override.ai)
      : base.ai,
    // embedding: shallow-merge the partial overlay onto base
    // when present; otherwise inherit the base verbatim. Same
    // `!== undefined` nuance as `computeEffectiveOverride` --
    // `{model_key: null}` is a legitimate "clear the key"
    // instruction and must not be swallowed by `??`.
    embedding: override.embedding
      ? {
          enabled:
            override.embedding.enabled !== undefined
              ? override.embedding.enabled
              : base.embedding.enabled,
          model_key:
            override.embedding.model_key !== undefined
              ? override.embedding.model_key
              : base.embedding.model_key,
        }
      : base.embedding,
    // retrieval: all six fields are primitives, so a flat
    // spread of the overlay over base produces the correct
    // merged object.
    retrieval: override.retrieval
      ? { ...base.retrieval, ...override.retrieval }
      : base.retrieval,
    auto_extraction_enabled:
      override.auto_extraction_enabled ?? base.auto_extraction_enabled,
    auto_extraction_min_turns:
      override.auto_extraction_min_turns ??
      base.auto_extraction_min_turns,
    // project_overrides on the merge result is inherited from
    // base -- overrides do not nest overrides, so there is
    // nothing project-specific to merge in. The response's
    // ``effective`` field will carry the same value.
    project_overrides: base.project_overrides,
  };
}



