// src/features/memory/types/jobs.ts

/**
 * Domain shapes for the Phase 3 extraction-job subsystem.
 *
 * Mirrors ``omweb.memory.jobs.models.MemoryJob`` (the Pydantic
 * model) field-for-field. Every field's type is the JSON-safe
 * form produced by ``MemoryJob.to_dict()`` -- enums are string
 * literals, ``Optional[X]`` becomes ``X | null``, and
 * ``List[Dict]`` becomes ``Array<Record<string, unknown>>``.
 *
 * Boundary with the backend
 * -------------------------
 *
 * This module is the SINGLE SOURCE OF TRUTH for what the jobs
 * API returns. Response envelopes (``JobsListResponse``,
 * ``JobRetryResponse``, ...) live in ``responses.ts`` and REFER
 * to the ``MemoryJob`` type here -- they do not redefine it.
 *
 * Every field documented below has a corresponding attribute on
 * the backend ``MemoryJob`` class. Where the backend's
 * ``to_dict()`` deviates from the Pydantic schema (see
 * ``batch_id``), the deviation is documented explicitly rather
 * than silently typed away.
 *
 * Scope
 * -----
 *
 * This module carries ONLY the domain model + small helpers
 * that gate UI actions on the job's status. It does NOT contain
 * client methods, query keys, or response envelopes -- those
 * live in their respective modules (``client.ts``,
 * ``keys.ts``, ``responses.ts``).
 *
 * Why a separate file from ``batch.ts``
 * -------------------------------------
 *
 * ``MemoryJob`` and ``MemoryBatch`` (in ``batch.ts``) are
 * sibling concepts -- a batch CONTAINS jobs but the reverse is
 * not true (most jobs are created by ``after_turn`` /
 * ``after_session`` / ``manual`` triggers and have no batch
 * parent). Keeping the two types in separate files makes the
 * optional nature of the batch linkage explicit in the imports:
 * a component that only renders job detail does not need to
 * pull in the batch model at all.
 */

// ---------------------------------------------------------------------------
// Job type discriminator
// ---------------------------------------------------------------------------

/**
 * The kind of work this job performs.
 *
 * Mirrors ``omweb.memory.jobs.models.MemoryJobType``. Phase 1
 * defines exactly ONE value; future phases (consolidation,
 * embedding backfill, ...) will extend this union. Callers who
 * need to render a "job type" label should switch on this field
 * with a default fallback for future values, NOT assume
 * ``'extraction'`` -- so that a new type landing in a future
 * deployment does not break the UI with a compile-time exhaustiveness
 * error.
 */
export type MemoryJobType = 'extraction';

// ---------------------------------------------------------------------------
// Lifecycle status
// ---------------------------------------------------------------------------

/**
 * Lifecycle status of a job.
 *
 * Mirrors ``omweb.memory.jobs.models.MemoryJobStatus``.
 *
 * States (with the cursor-invariant rules from plan §19.3):
 *
 *   * ``pending``        -- queued, awaiting a worker to claim.
 *   * ``running``        -- claimed by a worker (holds a lease).
 *   * ``retry_pending``  -- failed on a previous attempt,
 *                           scheduled for retry at
 *                           ``next_retry_at``.
 *   * ``completed``      -- finished successfully. Advanced the
 *                           per-chat cursor (the job's
 *                           ``end_turn_index`` becomes the new
 *                           cursor).
 *   * ``failed``         -- exceeded ``max_attempts``. Cursor
 *                           NOT advanced. The user must retry
 *                           manually via ``/jobs/{id}/retry``.
 *   * ``cancelled``      -- cancelled by a user action (either
 *                           directly, or via batch pause/cancel).
 *                           Cursor NOT advanced.
 *
 * Only ``completed`` advances the cursor. Every other terminal
 * state leaves it untouched, so the same turn range can be
 * re-enqueued later (via manual retry or a fresh batch).
 */
export type MemoryJobStatus =
  | 'pending'
  | 'running'
  | 'retry_pending'
  | 'completed'
  | 'failed'
  | 'cancelled';

// ---------------------------------------------------------------------------
// Trigger discriminator
// ---------------------------------------------------------------------------

/**
 * What caused this job to be created.
 *
 * Mirrors ``peldrun.memory.domain.enums.ExtractionTrigger``.
 *
 * Unlike ``MemoryJobType``, this enum's members ARE used for UI
 * gating today:
 *
 *   * ``after_turn``    -- system-triggered, usually batched.
 *   * ``after_session`` -- system-triggered at session end.
 *   * ``batch``         -- created by the batch subsystem; the
 *                          jobs list displays the batch linkage
 *                          (see ``batch_id`` below).
 *   * ``manual``        -- created by an explicit user click
 *                          (from-session / range endpoint).
 *
 * The UI uses this to LABEL each job ("Auto (per-turn)",
 * "Batch", "Manual", ...) and to filter the list. It never
 * gates WRITE actions -- the ``status`` field is what determines
 * whether retry/cancel are available.
 */
export type ExtractionTrigger =
  | 'after_turn'
  | 'after_session'
  | 'batch'
  | 'manual';

// ---------------------------------------------------------------------------
// Per-candidate decision
// ---------------------------------------------------------------------------

/**
 * One entry in a job's ``decisions`` log.
 *
 * The log is built across two layers:
 *
 *   1. The **orchestrator** records the LLM's accept/reject
 *      decisions per candidate (``evidence`` validation,
 *      ``min_confidence`` threshold, ``max_candidates`` cap).
 *
 *   2. The **worker** appends one entry per persisted candidate
 *      after the write pipeline runs (``ADD`` / ``SKIP`` /
 *      ``SUPERSEDE`` / ``REACTIVATE``), with the pipeline's
 *      reason and any error.
 *
 * The exact shape depends on which layer wrote the entry. Both
 * layers write a superset of the four fields below; callers
 * should treat unknown keys as opaque (``[key: string]:
 * unknown`` in the index signature) so a future backend field
 * addition does not break the UI.
 */
export interface MemoryJobDecision {
  /** The action taken for this candidate (ADD / SKIP / ...). */
  action?: string;
  /** Human-readable reason for the action. */
  reason?: string;
  /** The memory_id created or updated, when applicable. */
  target_id?: string | null;
  /** Error text when the write failed for this candidate. */
  error?: string | null;
  /** Forward-compatible catch-all for future fields. */
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// MemoryJob
// ---------------------------------------------------------------------------

/**
 * One row of the ``memory_jobs`` table, in its wire form.
 *
 * Mirrors the backend's ``MemoryJob.to_dict()`` output. Note:
 * enum-typed fields are strings on the wire (the backend calls
 * ``.value`` on each before serializing), and nullable fields
 * are ``X | null`` -- NOT ``X | undefined``. A JSON ``null`` is
 * a distinct state from a missing key, and this type preserves
 * that distinction so call sites cannot accidentally conflate
 * the two.
 *
 * Field-by-field documentation (grouped by concern):
 */
export interface MemoryJob {
  // ------------------------- identity -----------------------------------

  /**
   * Stable job identifier (``mjob_<16 hex>``).
   *
   * Note the ``mjob_`` prefix -- the jobs API's URLs
   * (``/jobs/{job_id}/retry``) preserve this prefix, so passing
   * the id straight into a URL is safe. Do NOT strip the
   * prefix: the backend matches on the full string.
   */
  job_id: string;

  /** The job type. Phase 1 always returns ``'extraction'``. */
  job_type: MemoryJobType;

  /**
   * The batch this job belongs to, or ``null`` for on-the-fly
   * jobs (AFTER_TURN / AFTER_SESSION / MANUAL).
   *
   * The key is ALWAYS present on the wire (possibly ``null``) --
   * the backend serializes it unconditionally via
   * ``MemoryJob.to_dict()``. ``null`` distinguishes "this job
   * has no batch" (the normal state for most jobs) from a
   * hypothetical serialization bug where the field is missing;
   * the two states must not be conflated.
   */
  batch_id: string | null;

  // ------------------------- chat context -------------------------------

  /** The chat this job extracts from. The cursor is per-chat. */
  chat_id: string;

  /** The owning project, or ``null`` for standalone chats. */
  project_id: string | null;

  // ------------------------- scope --------------------------------------

  /**
   * The default scope for extracted memories.
   *
   * Derived at enqueue time: ``'project'`` when ``project_id``
   * is set, ``'chat'`` otherwise. Individual candidates may
   * override this via their own ``scope_type``/``scope_id``
   * fields, but the job-level scope is the fallback.
   */
  scope_type: string;

  /** The concrete id matching ``scope_type``. */
  scope_id: string;

  // ------------------------- trigger ------------------------------------

  /** What caused this job to be created. */
  trigger_type: ExtractionTrigger;

  /**
   * Absolute path to the ``session.json`` this job reads.
   *
   * Used by the worker's snapshot re-check on every heartbeat.
   * ``null`` when the job was enqueued by an older code path
   * that predates snapshot tracking -- such jobs are NOT
   * verified (the worker allows them through with a note).
   */
  source_session_path: string | null;

  /** Inclusive start of the turn range (0-based). */
  start_turn_index: number | null;

  /**
   * Inclusive end of the turn range.
   *
   * On ``complete()``, this becomes the new per-chat cursor (if
   * it is greater than the current value). ``null`` means "the
   * whole session" -- the worker skips cursor advancement in
   * that case (callers should have supplied an explicit end).
   */
  end_turn_index: number | null;

  /**
   * Snapshot of the cursor value AT ENQUEUE TIME.
   *
   * Diagnostic only -- the authoritative cursor lives in
   * ``memory_chat_cursors``. The special value ``-1`` means "no
   * cursor row existed" (fresh chat); ``0`` means "turn 0 has
   * been processed".
   */
  last_processed_turn_index_at_enqueue: number;

  // ------------------------- snapshot -----------------------------------

  /**
   * SHA-256 prefix of the session's turn_ids + content_hashes
   * at enqueue time.
   *
   * The worker re-verifies this on every heartbeat; a mismatch
   * fails the job with ``SESSION_CHANGED``. The UI uses this
   * field for diagnostics ("why did this job fail?") -- it is
   * not otherwise user-meaningful.
   */
  message_snapshot_hash: string;

  /** Last successful snapshot verification (Unix seconds). */
  snapshot_verified_at: number | null;

  // ------------------------- status -------------------------------------

  /** Current lifecycle status. See ``MemoryJobStatus``. */
  status: MemoryJobStatus;

  /**
   * Number of FAILED attempts so far.
   *
   * The worker increments this on every ``fail()`` and
   * ``retry()`` call. Success is NOT counted (a job that
   * succeeded on the first attempt has ``attempts: 0``). So
   * the invariant is ``attempts < max_attempts`` while the job
   * is retryable; ``attempts == max_attempts`` on terminal
   * failure.
   */
  attempts: number;

  /** The attempt cap. Typically 3. */
  max_attempts: number;

  // ------------------------- model --------------------------------------

  /** The extraction LLM's provider id (e.g. ``'lmstudio'``). */
  model_provider_id: string;

  /** The extraction LLM's model id. */
  model_id: string;

  // ------------------------- idempotency --------------------------------

  /**
   * The deterministic idempotency key (32-char hex).
   *
   * Derived from (chat_id, trigger, range, snapshot_hash,
   * batch_id) at enqueue time. The UNIQUE constraint on the
   * backend's ``memory_jobs.idempotency_key`` column prevents
   * duplicates. The UI displays a shortened form in the job
   * list as a "correlation id" for support tickets; do NOT
   * expose it as an actionable identifier -- the ``job_id`` is
   * what the API uses.
   */
  idempotency_key: string;

  // ------------------------- lease --------------------------------------

  /**
   * Worker id currently holding the lease, or ``null``.
   *
   * Non-null ONLY while ``status === 'running'``. A stale lease
   * (``lease_expires_at < now``) is reclaimed by
   * ``recover_stale_leases`` on the next worker startup.
   */
  lease_owner: string | null;

  /** When the current lease expires (Unix seconds). */
  lease_expires_at: number | null;

  /** Last heartbeat from the worker (Unix seconds). */
  heartbeat_at: number | null;

  // ------------------------- timings ------------------------------------

  /** Enqueue timestamp (Unix seconds). */
  created_at: number;

  /** First claim timestamp (Unix seconds). */
  started_at: number | null;

  /** Terminal-state transition timestamp (Unix seconds). */
  completed_at: number | null;

  /**
   * Earliest time the job may be claimed again after a
   * retryable failure (Unix seconds). ``null`` when the job
   * is not scheduled for retry.
   */
  next_retry_at: number | null;

  // ------------------------- errors -------------------------------------

  /**
   * Machine-readable error code, or ``null``.
   *
   * Known values: ``'SESSION_CHANGED'``, ``'SESSION_MISSING'``,
   * ``'EXTRACTION_FAILED'``, ``'LEASE_EXPIRED'``,
   * ``'MODEL_UNAVAILABLE'``, ``'EVIDENCE_INVALID'``,
   * ``'BATCH_PAUSED'``, ``'BATCH_CANCELLED'``.
   *
   * Typed as ``string`` (not a union) because a future backend
   * release may add codes the current frontend does not know.
   * The UI should render the code verbatim with a
   * ``uxHintFor(error_code)`` lookup that falls back to the
   * raw code -- never throw on an unknown value.
   */
  error_code: string | null;

  /** Human-readable error detail for the audit log. */
  error_message: string | null;

  // ------------------------- results ------------------------------------

  /** Candidates the LLM proposed (pre-evidence-validation). */
  candidates_count: number;

  /** Memories actually persisted after validation + dedupe. */
  created_memories_count: number;

  /** Total LLM cost for this job, in nano-USD (10^-9 USD). */
  cost_nano_usd: number;

  /**
   * Per-candidate decision log. See ``MemoryJobDecision``.
   *
   * Empty for jobs that never reached the extraction stage
   * (pending, cancelled before claiming, snapshot-failed).
   */
  decisions: MemoryJobDecision[];
}

// ---------------------------------------------------------------------------
// Status enumerations (for UI iteration)
// ---------------------------------------------------------------------------

/**
 * Every job status, in lifecycle order (not alphabetical).
 *
 * Used by filter dropdowns to render the options in a
 * predictable sequence. The order matters for the UI: "pending
 * -> running -> retry_pending -> completed -> failed ->
 * cancelled" tells a coherent story; alphabetical ordering
 * ("cancelled, completed, failed, ...") does not.
 */
export const MEMORY_JOB_STATUSES: readonly MemoryJobStatus[] = [
  'pending',
  'running',
  'retry_pending',
  'completed',
  'failed',
  'cancelled',
] as const;

/**
 * Every extraction trigger, in "system -> user" order.
 *
 * Same rationale as ``MEMORY_JOB_STATUSES``: the filter
 * dropdown groups the two automatic triggers first (because
 * they dominate the job list), then batch, then manual.
 */
export const EXTRACTION_TRIGGERS: readonly ExtractionTrigger[] = [
  'after_turn',
  'after_session',
  'batch',
  'manual',
] as const;

// ---------------------------------------------------------------------------
// Status predicates
// ---------------------------------------------------------------------------
//
// These mirror the backend's status checks and are used by the
// jobs UI to gate the Retry / Cancel buttons. They are PREDICATES
// (not exhaustive switches) so adding a new status in a future
// backend release will not break the UI at compile time -- it
// will simply render neither button for the unknown status, which
// is the safest failure mode (no actionable UI for an action we
// do not understand).

/**
 * ``true`` when the job can never transition again.
 *
 * Matches ``MemoryJobStatus.is_terminal`` on the backend.
 */
export function isTerminalJobStatus(status: MemoryJobStatus): boolean {
  return (
    status === 'completed' ||
    status === 'failed' ||
    status === 'cancelled'
  );
}

/**
 * ``true`` when a worker can claim the job.
 *
 * Matches ``MemoryJobStatus.is_claimable`` on the backend.
 * Informational -- the UI never needs to gate on this (the
 * worker is a background process, not a UI action).
 */
export function isClaimableJobStatus(status: MemoryJobStatus): boolean {
  return status === 'pending' || status === 'retry_pending';
}

/**
 * ``true`` when ``POST /jobs/{id}/cancel`` will succeed.
 *
 * Matches the backend's ``cancel_if_active`` precondition
 * (``pending`` / ``retry_pending`` / ``running``). The jobs UI
 * uses this to enable/disable the "Cancel" button.
 *
 * Note: ``running`` IS cancellable -- the worker will notice
 * the transition on its next heartbeat and skip the
 * ``complete()`` call.
 */
export function isCancellableJobStatus(status: MemoryJobStatus): boolean {
  return (
    status === 'pending' ||
    status === 'retry_pending' ||
    status === 'running'
  );
}

/**
 * ``true`` when ``POST /jobs/{id}/retry`` will succeed.
 *
 * Matches the backend's ``requeue`` precondition (``failed`` /
 * ``cancelled``). The jobs UI uses this to enable/disable the
 * "Retry" button.
 *
 * Deliberately EXCLUDES the intermediate states
 * (``pending`` / ``retry_pending`` / ``running``) -- retrying
 * those would duplicate the worker's effort. And ``completed``
 * is a terminal success, so re-running it would double-extract.
 */
export function isRetryableJobStatus(status: MemoryJobStatus): boolean {
  return status === 'failed' || status === 'cancelled';
}

// ---------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------

/**
 * Human-readable label for a job status.
 *
 * Used by the job-list table's "Status" column. The strings are
 * designed for end-users (not operators): ``retry_pending``
 * becomes "Retrying", not "retry_pending".
 */
export function jobStatusLabel(status: MemoryJobStatus): string {
  switch (status) {
    case 'pending':
      return 'Pending';
    case 'running':
      return 'Running';
    case 'retry_pending':
      return 'Retrying';
    case 'completed':
      return 'Completed';
    case 'failed':
      return 'Failed';
    case 'cancelled':
      return 'Cancelled';
  }
}

/**
 * Human-readable label for a trigger type.
 */
export function extractionTriggerLabel(trigger: ExtractionTrigger): string {
  switch (trigger) {
    case 'after_turn':
      return 'Auto (per-turn)';
    case 'after_session':
      return 'Auto (session)';
    case 'batch':
      return 'Batch';
    case 'manual':
      return 'Manual';
  }
}
