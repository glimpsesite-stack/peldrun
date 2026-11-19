// src/features/memory/types/agentic.ts

/**
 * Phase 6 agentic retrieval -- HTTP contract types.
 *
 * Scope of this module
 * --------------------
 * ONLY the wire shapes of `POST /api/memory/search/agentic` and
 * `GET /api/memory/search/agentic/status`.
 *
 * The CONFIGURATION fields (`agentic_enabled`, `graph_search_*`, …)
 * are deliberately NOT declared here. They live on
 * `MemoryRetrievalSettings` in `./embedding.ts`, because that is the
 * single source of truth the settings PATCH, the dirty-check, and
 * the Zod validators all key off. Declaring a parallel
 * `AgenticSettings` interface here would create a second contract
 * that silently drifts from the first -- exactly the failure mode
 * `./settings.ts`'s module JSDoc warns about.
 *
 * Contract authority
 * ------------------
 * These types mirror `omweb/memory/api/memory_agentic.py` and
 * `peldrun/memory/agentic/contracts.py`. Two details are easy to
 * get wrong from the design mock-up, so they are called out here:
 *
 *   1. **There is no 503 for a disabled feature.**
 *      The endpoint ALWAYS answers 200 for a well-formed request.
 *      A disabled / misconfigured / timed-out run comes back as
 *      `is_available: false` with `error` set and the standard
 *      retrieval result under `fallback`. The UI branches on
 *      `is_available`, never on the status code.
 *
 *   2. **The fallback field is named `fallback`, not
 *      `fallback_context`, and its memory list is `memories`, not
 *      `results`.**
 */

import type { MemoryEntry } from './entry';

// ---------------------------------------------------------------------------
// Request
// ---------------------------------------------------------------------------

/**
 * Body of `POST /api/memory/search/agentic`.
 *
 * NO `scope_type` / `scope_id`: the endpoint resolves scope exactly
 * like `/search` (ambient chat + project + user). Offering an
 * arbitrary scope here would let the agentic path read a scope the
 * caller's normal retrieval cannot -- a privilege widening dressed
 * up as a convenience parameter.
 */
export interface AgenticSearchRequest {
  /** The complex question. 1..2000 chars. */
  query: string;

  /** Current chat id, if any. */
  chat_id?: string;

  /** Current project id, if any. */
  project_id?: string;

  /** Explicit user id. Backend defaults to `user:default`. */
  user_id?: string;

  /**
   * Per-request override for `retrieval.agentic_max_hops` [1, 5].
   * The backend threads this through as a per-call value, so it
   * never mutates shared state.
   */
  max_hops?: number;

  /**
   * When `false`, `evidence` comes back as an empty array.
   * Useful for a UI that only wants the synthesized answer.
   * Defaults to `true` on the backend.
   */
  include_evidence?: boolean;
}

// ---------------------------------------------------------------------------
// Why an agentic run degraded (the `error` field)
// ---------------------------------------------------------------------------

/**
 * Machine-readable reason an agentic run did not complete.
 *
 * `null` on a successful run. The union is CLOSED -- the backend
 * only ever emits these five values, so an exhaustive switch in the
 * UI is safe (and `agenticErrorCopy` below is that switch).
 */
export type AgenticUnavailableReason =
  | 'disabled' // retrieval.agentic_enabled is false, or no model resolved
  | 'empty_query' // query was blank after trimming
  | 'invalid_scope' // chat/project/user resolved to no valid scope
  | 'timeout' // exceeded retrieval.agentic_timeout_seconds
  | 'failed'; // unexpected pipeline error

// ---------------------------------------------------------------------------
// Evidence
// ---------------------------------------------------------------------------

/**
 * One supporting memory in an agentic answer.
 *
 * Wider than the design mock-up's `{content, score, memory_id}`:
 * the backend serializes the entry's identity fields so the UI can
 * label a source ("semantic · project:p1") without a second fetch.
 */
export interface AgenticEvidence {
  memory_id: string;
  content: string;
  /**
   * Retrieval score. Present on agentic runs; graph-seeded evidence
   * carries the graph's distance-derived score
   * (`1 / (1 + hop_distance)`).
   */
  score?: number;
  memory_type: string;
  scope_type: string;
  scope_id: string;
  status: string;
  importance: number;
  /** ISO-8601, or `null` for a legacy row. */
  created_at: string | null;
}

// ---------------------------------------------------------------------------
// Response
// ---------------------------------------------------------------------------

/**
 * The standard-retrieval payload returned when agentic runs cannot
 * happen. Mirrors what `/api/memory/search` would have produced.
 */
export interface AgenticFallbackContext {
  /** Strategy the standard path actually used. */
  search_mode: 'hybrid' | 'hybrid+graph' | 'hybrid+rerank' | 'vector' | 'fts' | 'graph';
  /** The standard retrieval results (NOT `results`). */
  memories: AgenticEvidence[];
  /** True when even the standard path found nothing. */
  is_empty: boolean;
}

/**
 * Body of a 200 response from `POST /api/memory/search/agentic`.
 *
 * ONE interface covers both outcomes rather than a discriminated
 * union. Reason: the backend returns the SAME envelope either way,
 * and a union would force every consumer to narrow on
 * `is_available` before touching `synthesized_answer` -- which is
 * `""` (not absent) on the fallback path. A single shape with
 * documented sentinel values is simpler to consume and impossible
 * to mis-narrow.
 */
export interface AgenticSearchResponse {
  /** Always `"ok"` on a 200. */
  status: 'ok';
  /** Always `"search/agentic"`. */
  operation: string;
  /** Mirrors `status` for callers that only check a boolean. */
  ok: true;

  /** `"agentic"` when the pipeline ran; `"agentic+fallback"` otherwise. */
  search_mode: 'agentic' | 'agentic+fallback';

  /** `true` only when the agentic pipeline ran to completion. */
  is_available: boolean;

  /** Why it did not run, or `null` when successful. */
  error: AgenticUnavailableReason | null;

  /** The original question, echoed back. */
  query: string;

  /** `""` when nothing was synthesized. */
  synthesized_answer: string;

  /** `0.0`–`1.0`. `0.0` when nothing was synthesized. */
  confidence: number;

  /** Whether `confidence` reached `retrieval.agentic_confidence_threshold`. */
  confidence_threshold_met: boolean;

  /** 1-based indices into `evidence`. Empty when nothing was synthesized. */
  citations: number[];

  /** Retrieval rounds executed (search hops only; 0 on the fallback path). */
  hops_used: number;

  /** Every distinct sub-query the run searched, in order. */
  sub_queries_used: string[];

  /** Supporting memories. Empty when `include_evidence: false`. */
  evidence: AgenticEvidence[];

  /** `evidence.length` as reported by the backend (pre-serialization). */
  evidence_count: number;

  /** Wall-clock duration of the agentic run, in milliseconds. */
  elapsed_ms: number;

  /** Present only on the fallback path. */
  fallback: AgenticFallbackContext | null;
}

/**
 * Body of `GET /api/memory/search/agentic/status`.
 *
 * The CHEAP way for the UI to decide whether to offer the feature:
 * no LLM call, no search, no settings fetch.
 *
 * `enabled` vs `configured`
 * -------------------------
 * They are different failures and the UI should say so:
 *
 *   * `enabled: false`    -- the operator has not turned the feature
 *                            on. Message: "enable it in Settings".
 *   * `configured: false` -- it IS enabled but no model resolved.
 *                            Message: "configure a model in Settings
 *                            → Memory → AI". Retrying the toggle would
 *                            not help.
 *
 * `available` is the AND of the two.
 */
export interface AgenticStatusResponse {
  status: 'ok';
  operation: string;
  enabled: boolean;
  configured: boolean;
  available: boolean;
  /**
   * Whether `ai.enabled` (the LLM master switch) is on.
   *
   * Exposed so the UI can distinguish the two ways `configured` can
   * be false. Without it, "no model resolved" is shown even when the
   * real cause is that AI is switched off entirely.
   */
  ai_enabled: boolean;
  /** Whether `ai.agentic` / the fallbacks resolve to a model. */
  model_resolved: boolean;
  max_hops: number;
  timeout_seconds: number;
  confidence_threshold: number;
  graph_search_enabled: boolean;
  /** Present only on an unexpected settings-read failure. */
  configuration_error?: string;
}

// ---------------------------------------------------------------------------
// Presentation helpers
// ---------------------------------------------------------------------------

/** Confidence buckets the UI colours by. */
export type ConfidenceBucket = 'high' | 'medium' | 'low';

/**
 * Bucket a confidence value.
 *
 * Thresholds (0.8 / 0.6) are PRESENTATION choices, deliberately
 * independent of `agentic_confidence_threshold` (a backend setting
 * the operator can move). Mixing the two would make the badge's
 * colour change when a setting changes, which is confusing.
 */
export function confidenceBucket(confidence: number): ConfidenceBucket {
  if (confidence >= 0.8) return 'high';
  if (confidence >= 0.6) return 'medium';
  return 'low';
}

/**
 * Format a raw confidence as a percentage string.
 *
 * Rounds to a whole percent: `0.8532` renders as `"85%"`, never as
 * `"0.8532"`. Values outside `[0, 1]` are clamped so a malformed
 * backend value can never render `"8734%"`.
 */
export function formatConfidence(confidence: number): string {
  const clamped = Number.isFinite(confidence)
    ? Math.min(1, Math.max(0, confidence))
    : 0;
  return `${Math.round(clamped * 100)}%`;
}

/**
 * Human-readable copy for an `AgenticUnavailableReason`.
 *
 * Exhaustive over the union: adding a reason to
 * `AgenticUnavailableReason` without adding it here is a compile
 * error (the `never` check at the bottom).
 */
export function agenticErrorCopy(reason: AgenticUnavailableReason | null): string {
  switch (reason) {
    case null:
      return '';
    case 'disabled':
      return 'Agentic search is off. Enable it in Settings → Memory → Agentic.';
    case 'empty_query':
      return 'Enter a question first.';
    case 'invalid_scope':
      return 'No memory scope is available for this chat.';
    case 'timeout':
      return 'Agentic search took too long and was stopped. Showing standard results instead.';
    case 'failed':
      return 'Agentic search failed. Showing standard results instead.';
  }
  // Compile-time exhaustiveness guard.
  const unreachable: never = reason;
  return String(unreachable);
}

/**
 * True when a response carries a usable synthesized answer.
 *
 * The single predicate consumers should branch on: a non-empty
 * `synthesized_answer` AND a successful run. Checking only
 * `synthesized_answer` would treat the fallback envelope (which
 * always has `""`) correctly by accident, but checking
 * `is_available` alone would treat an empty answer as usable.
 */
export function hasAgenticAnswer(
  response: AgenticSearchResponse | undefined,
): response is AgenticSearchResponse {
  return (
    response !== undefined &&
    response.is_available &&
    response.synthesized_answer.trim().length > 0
  );
}

/** Convenience type guard for the fallback path. */
export function isAgenticFallback(
  response: AgenticSearchResponse,
): boolean {
  return response.fallback !== null;
}

/**
 * Re-exported so a consumer that only needs the entry shape does not
 * have to reach into `./entry` for evidence rendering.
 */
export type { MemoryEntry };
