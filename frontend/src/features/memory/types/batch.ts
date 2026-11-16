// src/features/memory/types/batch.ts

/**
 * Domain shapes for the Phase 3 batch-extraction subsystem.
 *
 * Mirrors ``omweb.memory.batch.models.MemoryBatch`` (the
 * Pydantic model) field-for-field, plus the two aggregate
 * shapes returned by ``MemoryBatchService`` (``BatchProgress``
 * and ``BatchEstimate``).
 *
 * Three related shapes
 * --------------------
 *
 * 1. **`MemoryBatch`** -- one row of the ``memory_batches``
 *    table, as serialized by ``MemoryBatch.to_dict()``. This
 *    is what ``GET /batch``, ``GET /batch/{id}``, and the
 *    mutating batch endpoints return in their ``batch`` field.
 *
 * 2. **`BatchProgress`** -- the LIVE aggregate computed from
 *    ``memory_jobs WHERE batch_id = ?`` on every read. This is
 *    the authoritative view of "how many jobs are actually
 *    done"; the counters ON ``MemoryBatch`` are historical
 *    snapshots that may be stale. Batch API responses include
 *    both under separate keys (``batch`` and ``progress``) --
 *    see ``responses.ts``.
 *
 * 3. **`BatchEstimate`** -- the cost estimate returned by
 *    ``GET /batch/estimate``. A pure projection of the
 *    current state of every pending session; NO batch row is
 *    created to produce this.
 *
 * Why a separate file from ``jobs.ts``
 * ------------------------------------
 *
 * A batch is a CONTAINER of jobs; most jobs have no batch
 * parent (AFTER_TURN / AFTER_SESSION / MANUAL). Keeping the two
 * types separate means a component that renders job detail
 * does not pull the batch model into its dependency graph.
 * Response envelopes (which DO reference both types together)
 * live in ``responses.ts``.
 *
 * Enum semantics
 * --------------
 *
 * Unlike ``MemoryJobStatus``, ``MemoryBatchStatus`` has BOTH
 * intermediates and terminal states, and the "actionable"
 * states differ per action:
 *
 *   * ``pause`` is valid only on ``running``.
 *   * ``resume`` is valid only on ``paused``.
 *   * ``cancel`` is valid on any non-terminal state
 *     (``pending`` / ``running`` / ``paused``).
 *
 * The predicate helpers at the bottom of this file encode
 * those rules so the batch UI cannot accidentally render
 * "Cancel" on an already-completed batch.
 */

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

/**
 * Lifecycle status of a batch.
 *
 * Mirrors ``omweb.memory.batch.models.MemoryBatchStatus``.
 *
 * Transitions:
 *
 *     pending   -> running     (auto, after create_batch enqueues jobs)
 *     pending   -> completed   (auto, if the route produced 0 jobs)
 *     pending   -> cancelled   (user)
 *     pending   -> failed      (unrecoverable create error)
 *
 *     running   -> paused      (user)
 *     running   -> completed   (auto, when all jobs are terminal)
 *     running   -> cancelled   (user)
 *     running   -> failed      (unrecoverable error)
 *
 *     paused    -> running     (user resumes)
 *     paused    -> cancelled   (user)
 *
 *     completed -> (terminal)
 *     cancelled -> (terminal)
 *     failed    -> (terminal)
 *
 * The "auto" transitions are enforced by
 * ``MemoryBatchService.get_batch``'s self-healing logic: any
 * RUNNING batch whose jobs are all terminal is flipped to
 * COMPLETED during the read. PAUSED batches are NOT self-healed
 * -- pause is a deliberate user action and must survive job
 * completion (otherwise "pause" would be a no-op that lasts
 * only until the next read).
 */
export type MemoryBatchStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'cancelled'
  | 'failed';

// ---------------------------------------------------------------------------
// MemoryBatch
// ---------------------------------------------------------------------------

/**
 * One row of the ``memory_batches`` table, in its wire form.
 *
 * Mirrors ``MemoryBatch.to_dict()``. As with ``MemoryJob``, all
 * enum-typed fields are string unions on the wire and all
 * nullable fields are ``X | null`` (not optional).
 *
 * **Counters are historical, not live.**
 *
 * The counter fields (``completed_jobs``, ``failed_jobs``,
 * ``cancelled_jobs``, ``total_candidates``,
 * ``total_memories_created``, ``actual_cost_nano_usd``) are
 * snapshotted at the last ``update_batch`` call. They may lag
 * behind reality between updates. The AUTHORITATIVE live view
 * is the ``BatchProgress`` aggregate returned alongside the
 * batch in every read response -- the batch UI must render
 * progress from THAT, not from these fields.
 *
 * The counters exist so a caller who only sees the batch row
 * (e.g. from a log dump) can still estimate progress; the UI
 * uses them only as a fallback when the aggregate is
 * unavailable.
 */
export interface MemoryBatch {
  // ------------------------- identity -----------------------------------

  /**
   * Stable batch identifier (``batch_<16 hex>``).
   *
   * Note the ``batch_`` prefix -- unlike jobs (``mjob_``),
   * batch ids carry their own prefix; the URL path
   * ``/batch/{batch_id}`` preserves it. Do not strip.
   */
  batch_id: string;

  // ------------------------- lifecycle ----------------------------------

  /** Current lifecycle status. See ``MemoryBatchStatus``. */
  status: MemoryBatchStatus;

  /**
   * Who / what created this batch.
   *
   * Known values: ``'manual'`` (the UI button, the default),
   * ``'auto_schedule'`` (the scheduler). Any other string is
   * preserved verbatim by the backend for custom
   * integrations, so typed as ``string`` not a union.
   */
  triggered_by: string;

  // ------------------------- scope (job spec) ---------------------------

  /**
   * Optional project scope recorded on the batch.
   *
   * NOTE: metadata only. The current backend's ``route_batch``
   * scans ALL projects regardless of this field. The batch UI
   * displays it for context; it does not affect what runs.
   */
  project_id: string | null;

  /**
   * Optional explicit chat list recorded on the batch.
   *
   * Same caveat as ``project_id``: metadata only, does not
   * filter what runs today. Empty array (never ``null``) --
   * the backend stores it as JSON and defaults to ``[]``.
   */
  chat_ids: string[];

  /** Optional lower bound on session update time (Unix seconds). */
  date_from: number | null;

  /** Optional upper bound on session update time (Unix seconds). */
  date_to: number | null;

  // ------------------------- work counters ------------------------------

  /** Number of sessions the batch targeted. Historical. */
  session_count: number;

  /** Number of jobs enqueued at creation. Historical. */
  job_count: number;

  /** Jobs that reached ``completed``. Historical. */
  completed_jobs: number;

  /** Jobs that reached ``failed``. Historical. */
  failed_jobs: number;

  /** Jobs that reached ``cancelled``. Historical. */
  cancelled_jobs: number;

  /** Sum of ``candidates_count`` across all jobs. Historical. */
  total_candidates: number;

  /** Sum of ``created_memories_count``. Historical. */
  total_memories_created: number;

  // ------------------------- cost ---------------------------------------

  /**
   * Cost estimate from creation time, in nano-USD.
   *
   * Computed by ``MemoryBatchService.estimate_cost``'s heuristic
   * at the moment the batch was created. It may differ from
   * reality -- the heuristic is a conservative upper bound, and
   * the model's pricing can change mid-run. The AUTHORITATIVE
   * actual cost is ``BatchProgress.actual_cost_nano_usd``.
   */
  estimated_cost_nano_usd: number;

  /** Sum of ``cost_nano_usd`` across all jobs. Historical. */
  actual_cost_nano_usd: number;

  // ------------------------- model snapshot -----------------------------

  /**
   * Provider id of the extraction model used by the batch's
   * jobs, snapshotted at creation.
   *
   * The operator can change the global extraction model between
   * batches; this field records WHICH model ran each batch, so
   * the audit trail survives later settings changes.
   */
  model_provider_id: string | null;

  /** Model id used by the batch's jobs, snapshotted at creation. */
  model_id: string | null;

  /**
   * ``min_confidence`` setting in effect at batch creation
   * time. Audit-trail only -- the actual threshold used per job
   * is the one active when the worker processes it.
   */
  min_confidence: number | null;

  // ------------------------- timings ------------------------------------

  /** Batch creation timestamp (Unix seconds). */
  created_at: number;

  /** Timestamp when status first became ``'running'``. */
  started_at: number | null;

  /** Timestamp of the most recent pause. */
  paused_at: number | null;

  /** Timestamp when status became ``'completed'``. */
  completed_at: number | null;

  /** Timestamp when status became ``'cancelled'``. */
  cancelled_at: number | null;

  /**
   * Last time the batch's aggregate progress changed (Unix
   * seconds). Used by the UI to distinguish a "stuck" batch
   * from one that simply has not started yet.
   */
  last_progress_at: number | null;

  // ------------------------- error / options ---------------------------

  /**
   * Human-readable failure detail when ``status === 'failed'``.
   * ``null`` for every other state.
   */
  error_message: string | null;

  /**
   * Free-form options bag for future extensions.
   *
   * The current backend writes ``{}`` here by default and does
   * not consume any specific keys. Typed as a record of
   * ``unknown`` so callers can read known keys defensively
   * without assuming the object is empty.
   */
  options: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// BatchProgress (live aggregate)
// ---------------------------------------------------------------------------

/**
 * Live aggregate of a batch's jobs.
 *
 * Computed by ``MemoryOutbox.aggregate_batch_progress`` on
 * every batch read (``GET /batch/{id}``, list responses, and
 * every mutating endpoint's response). Authoritative -- never
 * stale, unlike the counters on ``MemoryBatch`` itself.
 *
 * The distinction matters for the batch UI:
 *
 *   * Progress bar / percentages / "N of M done" counters
 *     read from HERE.
 *   * ``MemoryBatch.job_count`` (historical) is only used to
 *     show the CREATION-TIME job count, not the live one.
 */
export interface BatchProgress {
  /**
   * Total jobs linked to this batch (sum of all statuses).
   *
   * May differ from ``MemoryBatch.job_count`` if:
   *   * jobs were added or removed out-of-band (rare), or
   *   * the historical counter was never updated after a
   *     partial failure during creation.
   */
  total: number;

  /**
   * Per-status job counts, keyed by ``MemoryJobStatus``.
   *
   * Statuses with zero jobs are OMITTED (not set to 0) -- the
   * backend builds this dict from a ``GROUP BY``, which does
   * not emit empty groups. The UI must treat a missing key as
   * "0", not "unknown".
   */
  by_status: Partial<Record<string, number>>;

  /** Convenience count: ``by_status.completed ?? 0``. */
  completed: number;

  /** Convenience count: ``by_status.failed ?? 0``. */
  failed: number;

  /** Convenience count: ``by_status.cancelled ?? 0``. */
  cancelled: number;

  /**
   * Jobs still requiring work: ``pending + retry_pending +
   * running``. When this is 0, the batch is eligible for the
   * self-heal transition to COMPLETED (if currently RUNNING).
   */
  pending_work: number;

  /** Sum of ``candidates_count`` across all jobs in the batch. */
  total_candidates: number;

  /** Sum of ``created_memories_count`` across all jobs. */
  total_memories_created: number;

  /** Sum of ``cost_nano_usd`` across all jobs. */
  actual_cost_nano_usd: number;
}

// ---------------------------------------------------------------------------
// BatchEstimate
// ---------------------------------------------------------------------------

/**
 * Cost / scope estimate for a fresh batch created NOW.
 *
 * Returned by ``GET /batch/estimate`` -- a pure projection over
 * the current pending-session state. NO batch row is created,
 * no job is enqueued. The estimate is a conservative upper
 * bound derived from per-turn and per-job token budgets; it is
 * NOT a quote.
 *
 * The batch-create dialog displays all five fields:
 *
 *   * "N sessions will be processed"
 *   * "M jobs will be created"       (usually == session count)
 *   * "K turns will be extracted"
 *   * "About X tokens"
 *   * "At most $Y (estimated)"
 */
export interface BatchEstimate {
  /** Number of sessions with pending turns. */
  session_count: number;

  /**
   * Number of jobs the batch would create if run now.
   *
   * Equal to ``session_count`` in the current backend --
   * ``route_batch`` emits exactly one job per session with
   * pending turns. Kept as a distinct field so a future
   * implementation that splits large sessions into multiple
   * jobs does not break the UI.
   */
  estimated_jobs: number;

  /** Total turns that would be extracted across all sessions. */
  total_pending_turns: number;

  /** Estimated input + output tokens (upper bound). */
  estimated_tokens: number;

  /**
   * Estimated cost in nano-USD (10^-9 USD). Divide by 1e9 to
   * get USD.
   *
   * The estimate uses a conservative blended rate (see
   * ``_COST_NANO_USD_PER_MILLION_TOKENS`` in the backend's
   * ``batch/service.py``); actual cost may be lower. The UI
   * should label it as "estimated" / "at most".
   */
  estimated_cost_nano_usd: number;
}

// ---------------------------------------------------------------------------
// Constants (for UI iteration)
// ---------------------------------------------------------------------------

/**
 * Every batch status, in lifecycle order (not alphabetical).
 *
 * Used by filter dropdowns. The order matches the state
 * machine's progression so "pending -> running -> paused ->
 * completed -> failed -> cancelled" reads as a coherent
 * narrative rather than alphabetization noise.
 */
export const MEMORY_BATCH_STATUSES: readonly MemoryBatchStatus[] = [
  'pending',
  'running',
  'paused',
  'completed',
  'cancelled',
  'failed',
] as const;

// ---------------------------------------------------------------------------
// Status predicates
// ---------------------------------------------------------------------------
//
// Each predicate mirrors a backend precondition exactly.
// Mismatch between UI gating and backend enforcement would
// either:
//   * enable a button whose POST returns 409 (bad UX), or
//   * disable a button that would actually succeed (silently
//     broken feature).
//
// The tests in ``__tests__/batch.test.ts`` (M9e) assert that
// these predicates match the backend's service-level checks --
// keeping them aligned is a hard requirement.

/**
 * ``true`` when the batch can never transition again.
 *
 * Matches ``MemoryBatchStatus.is_terminal`` on the backend.
 * Applies to ``completed``, ``cancelled``, and ``failed``.
 */
export function isTerminalBatchStatus(
  status: MemoryBatchStatus,
): boolean {
  return (
    status === 'completed' ||
    status === 'cancelled' ||
    status === 'failed'
  );
}

/**
 * ``true`` when the batch is between creation and a terminal
 * state. Inverse of ``isTerminalBatchStatus``.
 *
 * Used by the batch list's "show active only" filter.
 */
export function isActiveBatchStatus(
  status: MemoryBatchStatus,
): boolean {
  return !isTerminalBatchStatus(status);
}

/**
 * ``true`` when ``POST /batch/{id}/pause`` will succeed.
 *
 * The backend's ``pause_batch`` raises ``BatchStateError`` for
 * any status other than ``'running'``. Notably: a batch in
 * ``'pending'`` CANNOT be paused (there are no jobs to cancel
 * yet, and the create flow transitions to ``'running'``
 * automatically once jobs are enqueued). The UI must disable
 * the Pause button in that window; it usually flashes briefly.
 */
export function isPausableBatchStatus(
  status: MemoryBatchStatus,
): boolean {
  return status === 'running';
}

/**
 * ``true`` when ``POST /batch/{id}/resume`` will succeed.
 *
 * The backend's ``resume_batch`` raises ``BatchStateError`` for
 * any status other than ``'paused'``.
 */
export function isResumableBatchStatus(
  status: MemoryBatchStatus,
): boolean {
  return status === 'paused';
}

/**
 * ``true`` when ``POST /batch/{id}/cancel`` will succeed.
 *
 * The backend's ``cancel_batch`` raises ``BatchStateError`` for
 * any TERMINAL state. So the cancel button is enabled on
 * ``pending``, ``running``, and ``paused`` -- which is exactly
 * ``isActiveBatchStatus``. Kept as a separate function anyway
 * so a future backend change (e.g. forbidding cancel on
 * ``pending``) is a one-line change here rather than a
 * semantic overload of ``isActiveBatchStatus``.
 */
export function isCancellableBatchStatus(
  status: MemoryBatchStatus,
): boolean {
  return isActiveBatchStatus(status);
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

/**
 * Human-readable label for a batch status.
 *
 * Same conventions as ``jobStatusLabel`` -- designed for
 * end-users, not operators. ``pending`` becomes "Queued" (not
 * "Pending") because "Queued" better matches what the batch
 * actually is: waiting for jobs to be created.
 */
export function batchStatusLabel(status: MemoryBatchStatus): string {
  switch (status) {
    case 'pending':
      return 'Queued';
    case 'running':
      return 'Running';
    case 'paused':
      return 'Paused';
    case 'completed':
      return 'Completed';
    case 'cancelled':
      return 'Cancelled';
    case 'failed':
      return 'Failed';
  }
}

/**
 * Convert nano-USD to USD.
 *
 * The backend consistently reports costs in nano-USD
 * (10^-9 USD) to preserve precision across small per-job
 * amounts. The UI displays USD; this helper is the ONLY place
 * that division happens (so a future currency change is a
 * one-line edit).
 *
 * Returns a number. Callers should format with
 * ``Intl.NumberFormat`` or the project's standard currency
 * formatter.
 */
export function nanoUsdToUsd(nanoUsd: number): number {
  return nanoUsd / 1_000_000_000;
}
