# backend/omweb/memory/jobs/models.py
"""
PELDRUN Memory Platform - Phase 1-A: MemoryJob Domain Model.

Mirrors the `memory_jobs` SQL table (Migration v2, §6.2 of the
engineering plan) as a Pydantic v2 value object. Every field,
default, and constraint in the SQL DDL has a corresponding
attribute here, so the repository layer (`MemoryOutbox`) can
perform a 1:1 mapping in both directions without any hand-written
conversion tables.

Design invariants
-----------------

1. 1:1 mapping with the SQL schema.
   Changing this model requires a corresponding Migration. Any
   field-name or type drift between this class and `memory_jobs`
   is a bug that would corrupt state on the first save/load cycle.

2. Float timestamps, matching SQL REAL.
   All timing fields are `float` (Unix seconds), not `datetime`.
   Rationale: the worker operates in monotonic seconds for lease
   expiry and heartbeat scheduling; converting to aware `datetime`
   on every claim/heartbeat would add overhead with no benefit.
   The API layer formats these floats into ISO strings for display
   when needed.

3. Enum-typed status and trigger.
   `status` is a `MemoryJobStatus`; `trigger_type` is an
   `ExtractionTrigger`; `scope_type` is a `MemoryScope`. These
   enums are the SINGLE source of truth for their closed
   vocabularies. A typo in a string status (e.g. "pending"
   written as "pendng") is rejected at construction time, before
   it reaches SQLite.

4. `decisions` as a Python list, not JSON.
   The SQL column is `decisions_json TEXT DEFAULT '[]'`. The
   repository handles the JSON encode/decode at the storage
   boundary so every consumer of `MemoryJob` works with a plain
   `List[Dict[str, Any]]`.

5. `compute_idempotency_key` is the ONLY sanctioned way to
   produce an idempotency key.
   See §18.1 of the plan. Callers MUST use this class method;
   hand-rolled keys silently break the duplicate-suppression
   guarantee that the UNIQUE constraint depends on.
"""

from __future__ import annotations

import hashlib
import time
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)


# ---------------------------------------------------------------------------
# Job type discriminator
# ---------------------------------------------------------------------------


class MemoryJobType(str, Enum):
    """Job type discriminator.

    Phase 1 defines exactly ONE type (`extraction`). Future phases
    may add `consolidation`, `embedding_backfill`, etc. The field
    exists now so that a future migration can add new job types
    without touching this class.
    """

    EXTRACTION = "extraction"


# ---------------------------------------------------------------------------
# Lifecycle status
# ---------------------------------------------------------------------------


class MemoryJobStatus(str, Enum):
    """Lifecycle status of a memory_jobs row.

    States (see §19.3 of the plan for the cursor-invariant rules):

        pending       -> queued, awaiting a worker to claim.
        running       -> claimed by a worker (holds a lease).
        retry_pending -> failed on a previous attempt, scheduled
                         for retry at `next_retry_at`.
        completed     -> finished successfully. Advanced the cursor.
        failed        -> exceeded max_attempts. Cursor NOT advanced.
        cancelled     -> user or operator cancelled. Cursor NOT
                         advanced.

    Only `completed` advances the per-chat cursor. Every other
    terminal state leaves the cursor untouched so the same turn
    range can be re-enqueued later (via a manual retry or a fresh
    batch).
    """

    PENDING = "pending"
    RUNNING = "running"
    RETRY_PENDING = "retry_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True for states after which the job can never transition again."""
        return self in (
            MemoryJobStatus.COMPLETED,
            MemoryJobStatus.FAILED,
            MemoryJobStatus.CANCELLED,
        )

    @property
    def is_claimable(self) -> bool:
        """True for states a worker can pick up in `claim_one()`."""
        return self in (
            MemoryJobStatus.PENDING,
            MemoryJobStatus.RETRY_PENDING,
        )


# ---------------------------------------------------------------------------
# MemoryJob
# ---------------------------------------------------------------------------


class MemoryJob(BaseModel):
    """One row of `memory_jobs` -- one unit of extraction work.

    Constructed by ``MemoryOutbox.enqueue()``; mutated in place by
    the worker (via repository methods that rebuild the row) as the
    job progresses through its lifecycle. The class is treated as
    immutable-by-convention: the Outbox uses
    ``model_copy(update=...)`` rather than direct field assignment
    so that validators re-run on every transition.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- identity ------------------------------------

    job_id: str = Field(
        default_factory=lambda: f"mjob_{uuid4().hex[:16]}",
        description=(
            "Stable job identifier. 'mjob_' prefix + 16 hex chars. "
            "Never reused across jobs."
        ),
    )
    job_type: MemoryJobType = Field(
        default=MemoryJobType.EXTRACTION,
        description="Job discriminator; Phase 1 only defines 'extraction'.",
    )

    # ------------------------- batch linkage -------------------------------
    #
    # Nullable link to the batch that spawned this job. NULL for
    # on-the-fly jobs created by AFTER_TURN / AFTER_SESSION /
    # MANUAL triggers; non-NULL only for retroactive BATCH jobs
    # created by the Phase 3 batch subsystem.
    #
    # The column itself was introduced by Migration v3 (which also
    # creates `memory_batches`). This field is declared here so the
    # Pydantic model round-trips cleanly with the SQL schema AFTER
    # the migration is applied. `MemoryJob.model_config` sets
    # `extra="forbid"`, so ANY column added to `memory_jobs` MUST
    # have a matching field here -- otherwise `SELECT *` returns a
    # row whose `batch_id` key would raise `ValidationError` on
    # `MemoryJob(**data)`.
    #
    # The `_job_to_row_values` helper in `outbox.py` inlines this
    # column into the INSERT ONLY when it is non-None. That keeps
    # the "job is not part of a batch" INSERT compatible with v2
    # databases that have not yet been migrated to v3 (rolling
    # upgrade safety), while still writing it for real batch jobs.
    batch_id: Optional[str] = Field(
        default=None,
        description=(
            "Batch id this job belongs to, or None for on-the-fly "
            "jobs. Set only by the batch subsystem (Phase 3); "
            "AFTER_TURN / AFTER_SESSION / MANUAL jobs leave it "
            "None."
        ),
    )

    # ------------------------- chat context --------------------------------

    chat_id: str = Field(
        ...,
        description="The chat this job extracts from. Cursor is per-chat.",
    )
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "The project the chat belongs to (may be None for "
            "standalone chats)."
        ),
    )

    # ------------------------- scope ---------------------------------------

    scope_type: MemoryScope = Field(
        ...,
        description=(
            "Default scope for extracted memories. Derived from "
            "chat_id/project_id at enqueue time: PROJECT when "
            "project_id is set, CHAT otherwise."
        ),
    )
    scope_id: str = Field(
        ...,
        description="The concrete id matching `scope_type`.",
    )

    # ------------------------- trigger -------------------------------------

    trigger_type: ExtractionTrigger = Field(
        ...,
        description=(
            "What caused this job to be created. Determines retry "
            "policy and UI presentation; does not affect extraction "
            "logic itself."
        ),
    )
    source_session_path: Optional[str] = Field(
        default=None,
        description=(
            "Path to the session.json this job reads from. Used by "
            "the snapshot re-check in the worker's heartbeat."
        ),
    )
    start_turn_index: Optional[int] = Field(
        default=None,
        ge=0,
        description="Inclusive start of the turn range to process.",
    )
    end_turn_index: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Inclusive end of the turn range. On `complete()`, this "
            "becomes the new `last_processed_turn_index` (if it is "
            "greater than the current value)."
        ),
    )
    last_processed_turn_index_at_enqueue: int = Field(
        default=0,
        ge=-1,
        description=(
            "Snapshot of the per-chat cursor at enqueue time. "
            "Used for diagnostics; the authoritative cursor lives "
            "in `memory_chat_cursors`. "
            "Special value -1 means NO cursor row existed at "
            "enqueue time (fresh chat, zero turns ever processed) "
            "-- distinct from 0, which means turn 0 has been "
            "processed and the next turn to extract is turn 1."
        ),
    )

    # ------------------------- snapshot ------------------------------------

    message_snapshot_hash: str = Field(
        ...,
        description=(
            "SHA-256 prefix of the turn_ids + content_hashes of the "
            "session at enqueue time (see §21.1). The worker re-"
            "computes this on every heartbeat; a mismatch fails the "
            "job with SESSION_CHANGED."
        ),
    )
    snapshot_verified_at: Optional[float] = Field(
        default=None,
        description=(
            "Last time the snapshot was re-verified (Unix seconds)."
        ),
    )

    # ------------------------- status --------------------------------------

    status: MemoryJobStatus = Field(
        default=MemoryJobStatus.PENDING,
        description="Current lifecycle status.",
    )
    attempts: int = Field(
        default=0,
        ge=0,
        description="Number of failed attempts so far.",
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        description="Attempts before the job transitions to `failed`.",
    )

    # ------------------------- model ---------------------------------------

    model_provider_id: str = Field(
        ...,
        description=(
            "Provider id for the extraction LLM (e.g. 'openai')."
        ),
    )
    model_id: str = Field(
        ...,
        description=(
            "Model id for the extraction LLM (e.g. 'gpt-4o-mini')."
        ),
    )

    # ------------------------- idempotency ---------------------------------

    idempotency_key: str = Field(
        ...,
        description=(
            "Deterministic key (§18.1). UNIQUE constraint in SQL. "
            "A duplicate insert raises DuplicateJobError from the "
            "Outbox, which the caller treats as 'already enqueued'."
        ),
    )

    # ------------------------- lease ---------------------------------------

    lease_owner: Optional[str] = Field(
        default=None,
        description=(
            "Worker id currently holding the lease. NULL when the "
            "job is not running. A stale lease (expires_at < now) "
            "is recovered on worker startup via "
            "`recover_stale_leases()`."
        ),
    )
    lease_expires_at: Optional[float] = Field(
        default=None,
        description=(
            "When the current lease expires (Unix seconds)."
        ),
    )
    heartbeat_at: Optional[float] = Field(
        default=None,
        description=(
            "Last heartbeat from the worker (Unix seconds). The "
            "worker updates this every `heartbeat_interval_seconds` "
            "while running; the reaper uses this to detect dead "
            "workers."
        ),
    )

    # ------------------------- timing --------------------------------------

    created_at: float = Field(
        default_factory=time.time,
        description="Enqueue timestamp (Unix seconds).",
    )
    started_at: Optional[float] = Field(
        default=None,
        description="First claim timestamp (Unix seconds).",
    )
    completed_at: Optional[float] = Field(
        default=None,
        description=(
            "Terminal-state transition timestamp (Unix seconds)."
        ),
    )
    next_retry_at: Optional[float] = Field(
        default=None,
        description=(
            "Earliest time the job may be claimed again after a "
            "retryable failure. NULL when the job is not scheduled "
            "for retry."
        ),
    )

    # ------------------------- errors --------------------------------------

    error_code: Optional[str] = Field(
        default=None,
        description=(
            "Machine-readable error code (e.g. 'SESSION_CHANGED', "
            "'MODEL_UNAVAILABLE', 'EVIDENCE_INVALID'). Set on the "
            "transition to `failed` or when scheduling a retry."
        ),
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Human-readable error detail for the audit log.",
    )

    # ------------------------- results -------------------------------------

    candidates_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of extraction candidates produced by the LLM."
        ),
    )
    created_memories_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of MemoryEntry rows actually persisted."
        ),
    )
    cost_nano_usd: int = Field(
        default=0,
        ge=0,
        description=(
            "Total LLM cost of this job, in nano-USD (10^-9 USD)."
        ),
    )
    decisions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Per-candidate decision log (ADD/SKIP/MERGE/CONFLICT). "
            "Mirrors the `decisions_json` SQL column; the Outbox "
            "handles JSON encode/decode."
        ),
    )

    # ------------------------- validators ----------------------------------

    @field_validator(
        "job_id",
        "chat_id",
        "scope_id",
        "model_provider_id",
        "model_id",
        "idempotency_key",
        mode="after",
    )
    @classmethod
    def _validate_required_strings(cls, v: str, info: Any) -> str:
        """Trim and non-empty-check every required identifier."""
        if not isinstance(v, str):
            raise ValueError(
                f"{info.field_name} must be a string, "
                f"got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                f"{info.field_name} must not be empty"
            )
        return stripped

    @field_validator("message_snapshot_hash", mode="after")
    @classmethod
    def _validate_snapshot_hash(cls, v: str) -> str:
        """Non-empty, length-bounded snapshot hash."""
        if not isinstance(v, str):
            raise ValueError(
                f"message_snapshot_hash must be a string, "
                f"got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "message_snapshot_hash must not be empty"
            )
        if len(stripped) > 64:
            raise ValueError(
                f"message_snapshot_hash exceeds 64 chars "
                f"(got {len(stripped)})"
            )
        return stripped

    # ------------------------- derived properties --------------------------

    @property
    def is_terminal(self) -> bool:
        """See ``MemoryJobStatus.is_terminal``."""
        return self.status.is_terminal

    @property
    def is_claimable(self) -> bool:
        """See ``MemoryJobStatus.is_claimable``."""
        return self.status.is_claimable

    def has_active_lease(
        self, now: Optional[float] = None
    ) -> bool:
        """True when the job is RUNNING and its lease has not expired."""
        if self.status is not MemoryJobStatus.RUNNING:
            return False
        if self.lease_expires_at is None:
            return False
        effective_now = now if now is not None else time.time()
        return self.lease_expires_at > effective_now

    def is_retry_exhausted(self) -> bool:
        """True when ``attempts >= max_attempts``."""
        return self.attempts >= self.max_attempts

    # ------------------------- class helpers -------------------------------

    @classmethod
    def compute_idempotency_key(
        cls,
        *,
        chat_id: str,
        trigger_type: ExtractionTrigger,
        start_turn_index: Optional[int],
        end_turn_index: Optional[int],
        message_snapshot_hash: str,
        batch_id: Optional[str] = None,
    ) -> str:
        """Compute the deterministic idempotency key (§18.1).

        The key is a 32-char hex prefix of SHA-256 over a canonical
        string encoding every input that affects WHAT this job
        processes:

          * chat_id                -- different chat, different job
          * trigger_type           -- different trigger, different job
          * start/end_turn_index   -- different range, different job
          * message_snapshot_hash  -- different session content,
                                      different job
          * batch_id               -- different batch, different
                                      logical operation

        Two enqueues with the same inputs produce the same key,
        which the UNIQUE constraint on
        ``memory_jobs.idempotency_key`` rejects. Two enqueues with
        ANY differing input produce distinct keys and both jobs run.

        ``batch_id`` inclusion rationale
        --------------------------------
        A batch is a DISTINCT user-initiated operation, not a
        dedup opportunity. If the operator creates two batches
        back-to-back on the same unchanged sessions, both must
        run -- the second batch's jobs must not be silently
        swallowed as duplicates of the first batch's. Prepending
        ``batch_id`` to the canonical string gives each batch
        its own idempotency namespace.

        On-the-fly jobs (AFTER_TURN / AFTER_SESSION / MANUAL)
        pass ``batch_id=None`` (the default). Their keys are
        byte-identical to the pre-batch format, so existing
        rows in the database remain valid -- the change is
        backward-compatible.
        """
        start_s = (
            "none" if start_turn_index is None
            else str(start_turn_index)
        )
        end_s = (
            "none" if end_turn_index is None
            else str(end_turn_index)
        )
        base = (
            f"{chat_id}:{trigger_type.value}:"
            f"{start_s}:{end_s}:"
            f"{message_snapshot_hash[:16]}"
        )
        if batch_id:
            raw = f"{batch_id}:{base}"
        else:
            raw = base
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dict for logging / API responses.

        Includes ``batch_id`` (added in Migration v3). It is ``None``
        for on-the-fly jobs (AFTER_TURN / AFTER_SESSION / MANUAL)
        and non-``None`` for jobs created by the batch subsystem.

        The key is ALWAYS present (possibly ``null``) so consumers
        can distinguish "this field was not serialized" from "this
        job has no batch" -- the former would be a serialization
        bug, the latter is the normal state for most jobs.
        """
        return {
            "job_id": self.job_id,
            "job_type": self.job_type.value,
            "batch_id": self.batch_id,
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "scope_type": self.scope_type.value,
            "scope_id": self.scope_id,
            "trigger_type": self.trigger_type.value,
            "source_session_path": self.source_session_path,
            "start_turn_index": self.start_turn_index,
            "end_turn_index": self.end_turn_index,
            "last_processed_turn_index_at_enqueue": (
                self.last_processed_turn_index_at_enqueue
            ),
            "message_snapshot_hash": self.message_snapshot_hash,
            "snapshot_verified_at": self.snapshot_verified_at,
            "status": self.status.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "model_provider_id": self.model_provider_id,
            "model_id": self.model_id,
            "idempotency_key": self.idempotency_key,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "heartbeat_at": self.heartbeat_at,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "next_retry_at": self.next_retry_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "candidates_count": self.candidates_count,
            "created_memories_count": self.created_memories_count,
            "cost_nano_usd": self.cost_nano_usd,
            "decisions": list(self.decisions),
        }


# ---------------------------------------------------------------------------
# Embedding job type (Phase 4)
# ---------------------------------------------------------------------------
#
# Phase 4's vector search requires every "embedding-eligible" memory
# to have a precomputed vector under the currently-active model.
# This is the durable job row that tracks that work:
#
#   * enqueued when a memory is created/updated and the active
#     embedding settings designate a model,
#   * drained by ``EmbeddingWorker`` (Task 4.4.2), which embeds the
#     memory's content and persists the vector,
#   * SEPARATE from ``memory_jobs`` because the two have different
#     lifecycle requirements:
#
#       memory_jobs             memory_embedding_jobs
#       ------------            ---------------------
#       FIFO per-chat           Independent per-memory
#       Cursor advanced on      No cursor
#         completion
#       Lease-based heartbeat   Timeout-based stale recovery
#       Chat-scoped (chat_id,   Memory-scoped (memory_id only)
#         scope, snapshot)
#
# Mixing them into one table would force every embedding job to
# carry fields (chat_id, scope_type, scope_id, snapshot hash,
# cursor snapshot) that embeddings do not use -- and would force
# the embedding worker to satisfy cursor invariants that are
# meaningless for its task. Two tables, two models, two queues.


class EmbeddingJobStatus(str, Enum):
    """Lifecycle status of a ``memory_embedding_jobs`` row.

    States mirror ``MemoryJobStatus`` for API/UI consistency, but
    the transitions differ:

        pending       -> queued, awaiting a worker.
        running       -> claimed by a worker. Short-lived
                         (embeddings complete in seconds), so no
                         heartbeat -- only a timeout-based
                         ``recover_stale_running``.
        retry_pending -> scheduled for retry at ``next_retry_at``.
        completed     -> embedding persisted (or already present
                         and up-to-date).
        failed        -> exceeded max_attempts, or hit a permanent
                         error (auth, model not offered,
                         dimension mismatch).
        cancelled     -> operator action (no Phase-4 UI, defined
                         for completeness and future cleanup).
    """

    PENDING = "pending"
    RUNNING = "running"
    RETRY_PENDING = "retry_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """True for states the job can never transition out of."""
        return self in (
            EmbeddingJobStatus.COMPLETED,
            EmbeddingJobStatus.FAILED,
            EmbeddingJobStatus.CANCELLED,
        )

    @property
    def is_claimable(self) -> bool:
        """True for states a worker can pick up in ``claim_next()``."""
        return self in (
            EmbeddingJobStatus.PENDING,
            EmbeddingJobStatus.RETRY_PENDING,
        )


class EmbeddingJob(BaseModel):
    """One row of ``memory_embedding_jobs`` -- one unit of embedding work.

    1:1 mirror of the SQL schema created by ``_apply_v4`` in
    ``migrations.py``, minus one deliberate omission: this model
    does NOT carry an ``idempotency_key`` column. Deduplication is
    enforced by a **UNIQUE index** on the natural key
    ``(memory_id, model_id, model_version, content_version)`` --
    which the ``EmbeddingOutbox`` creates on open (see its
    docstring). The two representations are equivalent for
    deduplication, and the natural-key form has the additional
    benefit of expressing the business intent directly in the
    index definition.

    Minimum sufficient fields
    -------------------------
    The job stores only what is needed to:

      1. identify which memory to embed (``memory_id``);
      2. identify which vector space to produce (``model_id`` +
         ``model_version``);
      3. track retries (``attempts``, ``error_*``);
      4. record cost (``cost_nano_usd``).

    The memory's **content** is NOT stored here. The worker
    re-reads it from ``memories`` on every attempt. Rationale:

      * An embedding must reflect the memory's CURRENT content.
        A cached copy at enqueue time would produce a vector for
        stale text if the user edited the memory between enqueue
        and processing.

      * Storage duplication is avoided. The content already lives
        in ``memories.content``; storing it twice in the same
        database grows the file by the size of the memory corpus
        for zero benefit.

    No ``scope_type`` / ``scope_id``
    ---------------------------------
    Embeddings are scope-agnostic: the vector of "User loves
    coffee" is identical regardless of which project scope the
    memory belongs to. The retrieval layer filters by scope when
    it joins hits against ``memories``, not here.

    No lease / heartbeat
    --------------------
    Each embedding is one API call plus one DB write -- typically
    a few hundred milliseconds to a few seconds. A crashed worker
    leaves the job in ``running``; ``recover_stale_running``
    (in the outbox) flips it back to ``retry_pending`` after a
    fixed timeout. This is simpler and cheaper than the
    lease-based heartbeat the extraction worker uses, which is
    justified there by multi-minute LLM calls.

    No snapshot hash
    ----------------
    The worker re-reads memory content on every attempt. If the
    content changed since enqueue, the new embedding is
    generated and overwrites the old one (both in
    ``memory_embeddings`` and in the ``memory_vectors`` vec0
    index). This is the DESIRED behavior: a fresh vector for
    edited content is always better than a rejected job that
    leaves the memory without a vector.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- identity ------------------------------------

    job_id: str = Field(
        default_factory=lambda: f"eemb_{uuid4().hex[:16]}",
        description=(
            "Stable job identifier. 'eemb_' prefix (embedding) + "
            "16 hex chars. The distinct prefix from the extraction "
            "queue's 'mjob_' allows greps and log filters to "
            "separate the two job families without joining any "
            "table."
        ),
    )

    # ------------------------- target --------------------------------------

    memory_id: str = Field(
        ...,
        description=(
            "The memory entry this job embeds. Reference to "
            "``memories.memory_id``. Not declared as a SQL "
            "FOREIGN KEY: a memory can be purged while its "
            "embedding job is queued -- the worker treats "
            "'memory no longer exists' as a terminal fail "
            "(MEMORY_NOT_FOUND), which is a clearer error than "
            "a constraint violation that would block the delete."
        ),
    )

    # ------------------------- model identity ------------------------------

    model_id: str = Field(
        ...,
        description=(
            "Provider-reported model identifier (e.g. "
            "'text-embedding-3-small'). Combined with "
            "``model_version``, defines the vector space "
            "(namespace). Matches the corresponding field on "
            "``memory_embeddings``'s primary key so the worker's "
            "upsert writes to the correct namespace."
        ),
    )
    model_version: str = Field(
        ...,
        description=(
            "Opaque version string for the model. Usually equals "
            "``model_id``. Distinct when a provider versions "
            "models independently (rare; e.g. Cohere's model "
            "revisions)."
        ),
    )
    content_version: int = Field(
        default=1,
        ge=1,
        description=(
            "Monotonic counter for the CONTENT this embedding "
            "targets. Starts at 1. Phase 5 consolidation "
            "increments it when rewriting a memory's content; "
            "the new value produces a fresh idempotency key, so "
            "a re-embed job is accepted where the natural key "
            "would otherwise have been considered already "
            "processed. In Phase 4, always 1."
        ),
    )

    # ------------------------- status --------------------------------------

    status: EmbeddingJobStatus = Field(
        default=EmbeddingJobStatus.PENDING,
        description="Current lifecycle status.",
    )
    attempts: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of failed attempts so far. Incremented by "
            "``EmbeddingOutbox.fail()`` and ``.retry()`` (same "
            "semantics as ``MemoryJob.attempts``: it counts total "
            "failed attempts, not remaining retries)."
        ),
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "Attempts before the job transitions to ``failed``. "
            "3 matches the extraction-jobs default: enough to "
            "absorb a transient network error, not so many that "
            "a genuinely broken model spams the API with retries."
        ),
    )

    # ------------------------- timing --------------------------------------

    created_at: float = Field(
        default_factory=time.time,
        description=(
            "Enqueue timestamp (Unix seconds). Preserved across "
            "requeues so an old job retains its original FIFO slot "
            "when the operator retries it manually."
        ),
    )
    started_at: Optional[float] = Field(
        default=None,
        description=(
            "First claim timestamp. Used by "
            "``recover_stale_running`` to detect a worker that "
            "died mid-embedding: any row with "
            "``status='running'`` and "
            "``started_at < now - stale_timeout`` is flipped back "
            "to ``retry_pending`` on the next worker startup."
        ),
    )
    completed_at: Optional[float] = Field(
        default=None,
        description="Terminal-state transition timestamp.",
    )
    next_retry_at: Optional[float] = Field(
        default=None,
        description=(
            "Earliest time the job may be claimed again after a "
            "retryable failure. NULL when the job is not "
            "scheduled for retry (pending, running, or terminal)."
        ),
    )

    # ------------------------- errors --------------------------------------

    error_code: Optional[str] = Field(
        default=None,
        description=(
            "Machine-readable error code. Known values: "
            "MEMORY_NOT_FOUND, MODEL_UNAVAILABLE, "
            "DIMENSION_MISMATCH, TRANSPORT_ERROR, "
            "WORKER_TIMEOUT, INTERNAL_ERROR. Set on the "
            "transition to failed and on every retry."
        ),
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Human-readable error detail for the audit log.",
    )

    # ------------------------- cost ---------------------------------------

    cost_nano_usd: int = Field(
        default=0,
        ge=0,
        description=(
            "Cost of the SUCCESSFUL embedding call, in nano-USD. "
            "0 for local models that do not charge, and 0 on "
            "failed attempts (the provider rejected the request "
            "before consuming tokens). Cumulative only for the "
            "single attempt that succeeded."
        ),
    )

    # ------------------------- validators ----------------------------------

    @field_validator(
        "job_id",
        "memory_id",
        "model_id",
        "model_version",
        mode="after",
    )
    @classmethod
    def _validate_required_strings(cls, v: str, info: Any) -> str:
        """Trim and non-empty-check every required identifier.

        Matches ``MemoryJob._validate_required_strings`` -- the
        two classes share the same trimming policy so a bug in
        one cannot produce jobs that are accepted by the other.
        """
        if not isinstance(v, str):
            raise ValueError(
                f"{info.field_name} must be a string, "
                f"got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                f"{info.field_name} must not be empty"
            )
        return stripped

    # ------------------------- derived properties --------------------------

    @property
    def is_terminal(self) -> bool:
        """See ``EmbeddingJobStatus.is_terminal``."""
        return self.status.is_terminal

    @property
    def is_claimable(self) -> bool:
        """See ``EmbeddingJobStatus.is_claimable``."""
        return self.status.is_claimable

    def is_retry_exhausted(self) -> bool:
        """True when ``attempts >= max_attempts``."""
        return self.attempts >= self.max_attempts

    # ------------------------- helpers -------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dict for logging / API responses."""
        return {
            "job_id": self.job_id,
            "memory_id": self.memory_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "content_version": self.content_version,
            "status": self.status.value,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "next_retry_at": self.next_retry_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "cost_nano_usd": self.cost_nano_usd,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EmbeddingJob",
    "EmbeddingJobStatus",
    "MemoryJob",
    "MemoryJobStatus",
    "MemoryJobType",
]
