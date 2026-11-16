# backend/omweb/memory/jobs/outbox.py
"""
PELDRUN Memory Platform - Phase 1-A: Durable Job Outbox.

SQLite-backed FIFO queue for memory extraction jobs. This is the
ONLY sanctioned path for enqueuing jobs, claiming them, and
transitioning them through their lifecycle (see §5 of the plan:
"Single Write Path" applies to jobs the same way it applies to
memories).

Design invariants
-----------------

1. FIFO per-chat, parallel across chats (§18.3).
   `claim_one()` returns the oldest pending job for the oldest
   chat that has no currently-running job. This guarantees strict
   ordering within a chat (so extraction jobs process turns in
   order) while allowing different chats to make progress
   concurrently.

2. Cursor invariant is enforced HERE, not by callers (§19.3).
   Only `complete()` advances `memory_chat_cursors.last_processed_turn_index`.
   `fail()` and `retry()` deliberately do NOT touch it. Callers
   never manipulate the cursor table directly -- that is the
   single strongest guarantee that a failed job does not
   silently skip turns.

3. Idempotency is a database-level guarantee (§18.1).
   The `idempotency_key` column has a UNIQUE constraint (see
   Migration v2). `enqueue()` catches `IntegrityError` from the
   insert and raises `DuplicateJobError(existing.job_id)`. There
   is NO read-then-write race window.

4. No transaction survives across an `await`.
   Every async method is a thin wrapper around a sync method
   executed via `asyncio.to_thread()`. All SQL for one logical
   operation happens inside ONE thread, inside ONE transaction,
   on ONE connection. No transaction is ever held across an
   `await` boundary.

5. The outbox owns its connection.
   `open()` creates the connection with WAL + busy_timeout;
   `close()` releases it. No global state, no import-time side
   effects. Tests can spin up an in-memory outbox per test.

6. Row / model round-trip is lossless.
   `_row_to_job()` and `_job_to_row_values()` cover every field.
   A `MemoryJob` that round-trips through `enqueue` → `get_by_id`
   compares equal to the original (modulo timestamps the
   database generated).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from peldrun.memory.domain.enums import (
    ExtractionTrigger,
    MemoryScope,
)
from omweb.memory.jobs.models import (
    MemoryJob,
    MemoryJobStatus,
    MemoryJobType,
)
from omweb.memory.batch.models import (
    MemoryBatch,
    MemoryBatchStatus,
)


logger = logging.getLogger("omweb.memory.jobs.outbox")


# ---------------------------------------------------------------------------
# Exceptions (control-flow signals specific to the outbox layer)
# ---------------------------------------------------------------------------


class DuplicateJobError(Exception):
    """Raised by `enqueue()` when a job with the same idempotency
    key is already present.

    This is a CONTROL-FLOW SIGNAL, not a failure. Callers almost
    always catch it and treat it as success ("the job is already
    enqueued, nothing to do"). It is defined here (not in
    `peldrun.memory.domain.errors`) because:
      * the domain's error hierarchy is a closed set of failure
        modes, and a duplicate is not a failure;
      * the exception is meaningful only to callers of
        `MemoryOutbox`, and belongs next to them.

    Attributes
    ----------
    existing_job_id
        The `job_id` of the row already present in the table.
        Useful for logging, for the caller's own idempotency
        handling, and for tests.
    """

    def __init__(self, existing_job_id: str) -> None:
        self.existing_job_id = existing_job_id
        super().__init__(
            f"Job already enqueued (existing job_id: {existing_job_id})"
        )


class JobNotFoundError(Exception):
    """Raised by `complete()`, `fail()`, `retry()`, and
    `update_heartbeat()` when the given `job_id` does not exist.

    Signals a caller bug (double-complete, or a job_id from a
    different database). Callers should log and move on; there is
    no recovery path.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"Memory job not found: {job_id}")


# ---------------------------------------------------------------------------
# JobResult -- the outcome of a successful job
# ---------------------------------------------------------------------------


class JobResult(BaseModel):
    """Outcome of a successfully completed extraction job.

    Passed to `MemoryOutbox.complete()` so the job row records the
    results (candidates produced, memories persisted, cost spent,
    per-candidate decisions) atomically with the status transition
    to `completed` and the cursor advancement.
    """

    candidates_count: int = Field(default=0, ge=0)
    created_memories_count: int = Field(default=0, ge=0)
    cost_nano_usd: int = Field(default=0, ge=0)
    decisions: List[Dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# MemoryOutbox
# ---------------------------------------------------------------------------


class MemoryOutbox:
    """Durable FIFO outbox for `memory_jobs` rows.

    One instance per process. Thread-safe via `asyncio.Lock` at the
    async boundary; the underlying SQLite connection is opened
    with `check_same_thread=False` and WAL mode so concurrent
    `to_thread` executions are serialized by SQLite itself.

    Lifecycle
    ---------
    * `await open()`    -- before any other method.
    * CRUD operations.
    * `await close()`   -- on shutdown.

    `open()` and `close()` are idempotent (safe to call twice).
    """

    _LEASE_DURATION_SECONDS: int = 60
    """Default lease length. Worker heartbeats extend it; a stale
    lease (heartbeat older than this) is reclaimable by
    `recover_stale_leases()`."""

    def __init__(
        self,
        db_path: Union[str, Path],
        *,
        busy_timeout_ms: int = 60_000,
    ) -> None:
        """
        Parameters
        ----------
        db_path
            Path to the SQLite database file. The SAME path the
            MemoryRepository uses; both connections coexist under
            WAL.
        busy_timeout_ms
            Time a writer will wait for the write lock before
            raising "database is locked". Defaults to 60s per §14
            Risk #2 mitigation.
        """
        self._db_path = str(db_path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """Open the SQLite connection (idempotent)."""
        if self._conn is not None:
            return
        self._conn = await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,
            timeout=self._busy_timeout_ms / 1000.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        logger.info(
            "MemoryOutbox opened: path=%r busy_timeout=%dms",
            self._db_path,
            self._busy_timeout_ms,
        )
        return conn

    async def close(self) -> None:
        """Close the SQLite connection (idempotent)."""
        if self._conn is None:
            return
        conn = self._conn
        self._conn = None
        await asyncio.to_thread(conn.close)
        logger.info("MemoryOutbox closed.")

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError(
                "MemoryOutbox is not open; call `await open()` first."
            )
        return self._conn

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    async def enqueue(self, job: MemoryJob) -> MemoryJob:
        """Insert a new job; raise `DuplicateJobError` on key collision.

        The UNIQUE constraint on `idempotency_key` (Migration v2)
        rejects duplicates at the storage layer. No read-then-write
        race is possible: the insert either succeeds, or the
        database raises IntegrityError, in which case we surface
        the existing row's `job_id`.
        """
        async with self._lock:
            return await asyncio.to_thread(self._enqueue_sync, job)

    def _enqueue_sync(self, job: MemoryJob) -> MemoryJob:
        conn = self._require_conn()
        values = self._job_to_row_values(job)
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        sql = (
            f"INSERT INTO memory_jobs ({columns}) "
            f"VALUES ({placeholders})"
        )
        try:
            with conn:
                conn.execute(sql, tuple(values.values()))
        except sqlite3.IntegrityError as exc:
            # Only the UNIQUE(idempotency_key) constraint is expected
            # to fire here; every other NOT NULL / CHECK violation
            # would indicate a caller bug in `MemoryJob` construction
            # (which Pydantic already prevents).
            existing = self._get_by_key_sync(job.idempotency_key)
            if existing is not None:
                raise DuplicateJobError(existing.job_id) from exc
            # IntegrityError without a matching row means a schema
            # drift bug. Re-raise with context.
            logger.error(
                "Unexpected IntegrityError in enqueue (%s): %s",
                job.job_id,
                exc,
            )
            raise
        logger.info(
            "Enqueued memory job %s (chat=%s trigger=%s range=[%s..%s])",
            job.job_id,
            job.chat_id,
            job.trigger_type.value,
            job.start_turn_index,
            job.end_turn_index,
        )
        return job

    # ------------------------------------------------------------------
    # Claim (FIFO per-chat, §18.3)
    # ------------------------------------------------------------------

    async def claim_one(self, worker_id: str) -> Optional[MemoryJob]:
        """Claim the next eligible job, granting a lease (§18.3).

        FIFO ordering rules:
          * Oldest chat with a claimable job AND no currently-
            running job wins.
          * Within that chat, the oldest pending job wins.
          * The claimed job transitions to `running` with the
            caller's `worker_id` as lease owner.

        Returns None when no job is claimable (either the queue is
        empty, or every chat that has pending work also has a
        running job).
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_one_sync, worker_id
            )

    def _claim_one_sync(self, worker_id: str) -> Optional[MemoryJob]:
        conn = self._require_conn()
        now = time.time()
        lease_until = now + self._LEASE_DURATION_SECONDS

        with conn:
            # Step 1 -- pick the oldest chat_id that has a
            # claimable job and no currently-running job.
            chat_row = conn.execute(
                """
                SELECT chat_id, MIN(created_at) AS first_created
                FROM memory_jobs
                WHERE status IN ('pending', 'retry_pending')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM memory_jobs AS running
                      WHERE running.chat_id = memory_jobs.chat_id
                        AND running.status = 'running'
                  )
                GROUP BY chat_id
                ORDER BY first_created ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if chat_row is None:
                return None

            chat_id = chat_row["chat_id"]

            # Step 2 -- the oldest claimable job in that chat.
            job_row = conn.execute(
                """
                SELECT * FROM memory_jobs
                WHERE chat_id = ?
                  AND status IN ('pending', 'retry_pending')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (chat_id, now),
            ).fetchone()
            if job_row is None:
                # Race: another worker claimed the last job of this
                # chat between Step 1 and Step 2. Treat as "nothing
                # to claim"; the caller's loop will retry.
                return None

            job_id = job_row["job_id"]

            # Step 3 -- grant the lease.
            conn.execute(
                """
                UPDATE memory_jobs
                SET status = 'running',
                    lease_owner = ?,
                    lease_expires_at = ?,
                    heartbeat_at = ?,
                    started_at = COALESCE(started_at, ?)
                WHERE job_id = ?
                """,
                (worker_id, lease_until, now, now, job_id),
            )

        # Re-read inside the transaction result to return the
        # post-update state.
        refreshed = conn.execute(
            "SELECT * FROM memory_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if refreshed is None:
            return None
        logger.info(
            "Claimed memory job %s by %s (chat=%s)",
            job_id, worker_id, chat_id,
        )
        return self._row_to_job(refreshed)

    # ------------------------------------------------------------------
    # Complete (advances cursor -- §19.3)
    # ------------------------------------------------------------------

    async def complete(
        self, job_id: str, result: JobResult
    ) -> None:
        """Transition the job to `completed` AND advance the cursor.

        Both the row update and the cursor upsert happen inside ONE
        transaction, so a crash between them cannot leave a
        completed job whose cursor was never advanced (which would
        cause the same turns to be re-extracted on next enqueue).
        """
        async with self._lock:
            await asyncio.to_thread(
                self._complete_sync, job_id, result
            )

    def _complete_sync(
        self, job_id: str, result: JobResult
    ) -> None:
        conn = self._require_conn()
        now = time.time()
        with conn:
            row = conn.execute(
                "SELECT chat_id, end_turn_index FROM memory_jobs "
                "WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)

            chat_id = row["chat_id"]
            end_turn_index = row["end_turn_index"]

            conn.execute(
                """
                UPDATE memory_jobs
                SET status = 'completed',
                    completed_at = ?,
                    candidates_count = ?,
                    created_memories_count = ?,
                    cost_nano_usd = ?,
                    decisions_json = ?,
                    error_code = NULL,
                    error_message = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (
                    now,
                    result.candidates_count,
                    result.created_memories_count,
                    result.cost_nano_usd,
                    json.dumps(result.decisions),
                    job_id,
                ),
            )

            # Cursor advancement -- ONLY here, and ONLY if the
            # end_turn_index is an int (None means "whole session,
            # unknown total"; skip the cursor update in that case,
            # the caller should have supplied an explicit end).
            if isinstance(end_turn_index, int):
                conn.execute(
                    """
                    INSERT INTO memory_chat_cursors
                        (chat_id, last_processed_turn_index, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        last_processed_turn_index = MAX(
                            memory_chat_cursors.last_processed_turn_index,
                            excluded.last_processed_turn_index
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, end_turn_index, now),
                )
        logger.info(
            "Completed memory job %s (candidates=%d created=%d cost=%d)",
            job_id,
            result.candidates_count,
            result.created_memories_count,
            result.cost_nano_usd,
        )

    # ------------------------------------------------------------------
    # Fail (does NOT touch the cursor -- §19.3)
    # ------------------------------------------------------------------

    async def fail(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        """Transition to `failed`. Does NOT advance the cursor.

        Increments ``attempts`` so the final state records the
        TOTAL number of failed attempts that occurred before the
        job was abandoned. Combined with `retry()` (which also
        increments), this makes ``attempts`` mean "total failed
        attempts", which is what post-mortem analysis and the
        API's ``GET /jobs`` endpoint expect:

          * first failure, no retry   -> attempts=1  (terminal)
          * 1 retry, second failure   -> attempts=2  (terminal)
          * 2 retries, then success   -> attempts=2  (completed)

        The invariant the caller depends on
        (``MemoryExtractionWorker._handle_job_error``) is:

            job.attempts (from claim) + 1 >= job.max_attempts
            -> terminal fail

        With this increment, a job with ``max_attempts=2`` that
        fails twice ends with ``attempts=2 == max_attempts``,
        which matches the plan's T6 assertion (§13).
        """
        async with self._lock:
            await asyncio.to_thread(
                self._fail_sync, job_id, error_code, error_message
            )

    def _fail_sync(
        self, job_id: str, error_code: str, error_message: str
    ) -> None:
        conn = self._require_conn()
        now = time.time()
        with conn:
            cur = conn.execute(
                """
                UPDATE memory_jobs
                SET status = 'failed',
                    attempts = attempts + 1,
                    completed_at = ?,
                    error_code = ?,
                    error_message = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (now, error_code, error_message, job_id),
            )
            if cur.rowcount == 0:
                raise JobNotFoundError(job_id)
        logger.warning(
            "Failed memory job %s (%s): %s",
            job_id, error_code, error_message,
        )

    # ------------------------------------------------------------------
    # Retry (does NOT touch the cursor -- §19.3)
    # ------------------------------------------------------------------

    async def retry(
        self, job_id: str, next_retry_at: float
    ) -> None:
        """Schedule a retry. Increments `attempts`. No cursor touch."""
        async with self._lock:
            await asyncio.to_thread(
                self._retry_sync, job_id, next_retry_at
            )

    def _retry_sync(
        self, job_id: str, next_retry_at: float
    ) -> None:
        conn = self._require_conn()
        with conn:
            cur = conn.execute(
                """
                UPDATE memory_jobs
                SET status = 'retry_pending',
                    attempts = attempts + 1,
                    next_retry_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (next_retry_at, job_id),
            )
            if cur.rowcount == 0:
                raise JobNotFoundError(job_id)
        logger.info(
            "Scheduled retry for memory job %s at %.0f",
            job_id, next_retry_at,
        )

    # ------------------------------------------------------------------
    # Manual requeue (UI retry of a failed / cancelled job)
    # ------------------------------------------------------------------

    async def requeue(self, job_id: str) -> MemoryJob:
        """Re-queue a FAILED or CANCELLED job for fresh execution.

        Distinct from ``retry()`` -- this is the UI's "Retry"
        button, not the worker's automatic backoff. A UI retry is
        an EXPLICIT user action that (per plan §19.3) is the ONLY
        sanctioned remediation for a terminal-failure state, so
        it resets the failure bookkeeping instead of adding to it.

        Preconditions
        -------------
        The job MUST be in ``failed`` or ``cancelled``. Any other
        state raises ``ValueError`` -- retrying a completed job
        would double-extract, and retrying a pending / running
        job would just duplicate work the worker is already
        doing.

        What is reset
        -------------
        * ``status``           -> ``pending``
        * ``attempts``         -> ``0``
        * ``next_retry_at``    -> ``NULL``
        * ``error_code``       -> ``NULL``
        * ``error_message``    -> ``NULL``
        * ``lease_owner``      -> ``NULL``
        * ``lease_expires_at`` -> ``NULL``
        * ``completed_at``     -> ``NULL``
        * ``started_at``       -> ``NULL``
        * ``heartbeat_at``     -> ``NULL``

        What is PRESERVED
        -----------------
        * ``message_snapshot_hash``
            Preserved on purpose. If the session has changed
            since the original enqueue, the worker's
            ``_verify_snapshot`` step fails the requeued job
            with ``SESSION_CHANGED`` -- which is the CORRECT
            behavior: the user must decide whether to accept
            the new session state (fresh job) or investigate
            the change (cancel + manual re-run).

        * ``source_session_path``, ``start_turn_index``,
          ``end_turn_index``, ``scope_type``, ``scope_id``,
          ``idempotency_key``, ``created_at``
            All preserved: the requeued job is the SAME logical
            unit of work, and its ``created_at`` keeps it in its
            original FIFO slot (a requeued old job must not jump
            ahead of newer pending jobs).

        Returns
        -------
        MemoryJob
            The requeued job, in its post-reset state (status
            ``pending``, attempts ``0``).

        Raises
        ------
        JobNotFoundError
            When ``job_id`` does not exist.
        ValueError
            When the job is in a state other than ``failed`` or
            ``cancelled``. The message names the current status
            so the API can return a precise 409 Conflict.
        """
        async with self._lock:
            return await asyncio.to_thread(self._requeue_sync, job_id)

    def _requeue_sync(self, job_id: str) -> MemoryJob:
        conn = self._require_conn()
        with conn:
            row = conn.execute(
                "SELECT status FROM memory_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)

            current_status = row["status"]
            if current_status not in ("failed", "cancelled"):
                raise ValueError(
                    f"Cannot requeue job {job_id!r}: current "
                    f"status is {current_status!r}; only "
                    f"'failed' and 'cancelled' jobs can be "
                    f"requeued."
                )

            # The WHERE clause repeats the status filter as a
            # race guard: if another process transitioned the
            # job between the SELECT and this UPDATE, the
            # rowcount will be 0 and we surface the same error
            # the state check would have produced.
            cur = conn.execute(
                """
                UPDATE memory_jobs
                SET status = 'pending',
                    attempts = 0,
                    next_retry_at = NULL,
                    error_code = NULL,
                    error_message = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    completed_at = NULL,
                    started_at = NULL,
                    heartbeat_at = NULL
                WHERE job_id = ?
                  AND status IN ('failed', 'cancelled')
                """,
                (job_id,),
            )
            if cur.rowcount == 0:
                raise ValueError(
                    f"Cannot requeue job {job_id!r}: it changed "
                    f"state during the operation; refresh and "
                    f"retry."
                )

        refreshed = conn.execute(
            "SELECT * FROM memory_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if refreshed is None:
            # Extremely unlikely; a concurrent DELETE would have
            # to run between the UPDATE and this SELECT.
            raise JobNotFoundError(job_id)
        logger.info(
            "Requeued memory job %s (was %r); attempts reset to 0.",
            job_id, current_status,
        )
        return self._row_to_job(refreshed)

    # ------------------------------------------------------------------
    # Cancellation (does NOT touch the cursor)
    # ------------------------------------------------------------------

    async def cancel(self, job_id: str) -> None:
        """Transition to `cancelled`. No cursor touch."""
        async with self._lock:
            await asyncio.to_thread(self._cancel_sync, job_id)

    def _cancel_sync(self, job_id: str) -> None:
        conn = self._require_conn()
        now = time.time()
        with conn:
            cur = conn.execute(
                """
                UPDATE memory_jobs
                SET status = 'cancelled',
                    completed_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            if cur.rowcount == 0:
                raise JobNotFoundError(job_id)

    async def cancel_if_active(self, job_id: str) -> MemoryJob:
        """Cancel a job only when it is still non-terminal.

        Distinct from ``cancel()`` (which blindly writes
        ``status='cancelled'`` regardless of the current state).
        This is the STRICT variant used by the HTTP API, where
        overwriting a terminal state is never acceptable:

          * cancelling a ``completed`` job would silently
            erase the "this succeeded" fact and lose the
            cursor-advance audit trail,
          * cancelling a ``failed`` job would hide the
            failure diagnostic that the operator needs.

        Preconditions
        -------------
        The job MUST be in ``pending``, ``retry_pending``, or
        ``running``. Terminal states (``completed``, ``failed``,
        ``cancelled``) raise ``ValueError`` so the API can map
        it to a 409 Conflict. This mirrors the requirement of
        the "Cancel" button in the UI -- it should only be
        clickable while the job is actually cancellable.

        Race safety
        -----------
        The UPDATE includes a ``WHERE ... AND status IN (...)``
        guard so that if another process transitions the job to
        a terminal state between the state check and the UPDATE,
        the rowcount becomes 0 and we surface the SAME
        ``ValueError`` (with a "state changed" message). No
        window exists where a terminal job is silently
        cancelled.

        Returns
        -------
        MemoryJob
            The cancelled job, in its post-transition state.

        Raises
        ------
        JobNotFoundError
            When ``job_id`` does not exist.
        ValueError
            When the job is in a terminal state, or its state
            changed during the operation.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._cancel_if_active_sync, job_id
            )

    def _cancel_if_active_sync(self, job_id: str) -> MemoryJob:
        conn = self._require_conn()
        now = time.time()
        with conn:
            row = conn.execute(
                "SELECT status FROM memory_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)

            current_status = row["status"]
            if current_status not in (
                "pending", "retry_pending", "running",
            ):
                raise ValueError(
                    f"Cannot cancel job {job_id!r}: current "
                    f"status is {current_status!r}; only "
                    f"non-terminal jobs can be cancelled."
                )

            cur = conn.execute(
                """
                UPDATE memory_jobs
                SET status = 'cancelled',
                    completed_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE job_id = ?
                  AND status IN (
                      'pending', 'retry_pending', 'running'
                  )
                """,
                (now, job_id),
            )
            if cur.rowcount == 0:
                raise ValueError(
                    f"Cannot cancel job {job_id!r}: it changed "
                    f"state during the operation; refresh and "
                    f"retry."
                )

        refreshed = conn.execute(
            "SELECT * FROM memory_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if refreshed is None:
            raise JobNotFoundError(job_id)
        logger.info(
            "Cancelled memory job %s (was %r).",
            job_id, current_status,
        )
        return self._row_to_job(refreshed)

    async def cancel_with_code(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str = "",
        allowed_statuses: Tuple[str, ...] = (
            "pending", "retry_pending", "running",
        ),
    ) -> MemoryJob:
        """Cancel a job with error metadata (batch operations).

        Unlike ``cancel_if_active`` (which is the UI's "Cancel"
        button and always allows the same three states), this
        variant lets the CALLER choose which statuses are
        cancellable AND attaches an ``error_code`` so the caller
        can later identify WHY the job was cancelled:

          * batch **pause** uses
            ``allowed_statuses=("pending", "retry_pending")``
            and ``error_code="BATCH_PAUSED"``. Running jobs
            are left alone -- the current attempt finishes,
            its success is recorded, and no work is lost. The
            paused batch's resume logic queries
            ``error_code='BATCH_PAUSED'`` to know which jobs
            to requeue.

          * batch **cancel** uses
            ``allowed_statuses=("pending", "retry_pending",
            "running")`` and ``error_code="BATCH_CANCELLED"``.
            Every non-terminal job is stopped.

        The ``error_code`` lands in ``memory_jobs.error_code``
        and ``error_message`` lands in
        ``memory_jobs.error_message``. Both survive until
        ``requeue`` is called (which clears them).

        Preconditions
        -------------
        The job's status MUST be one of ``allowed_statuses``.
        Any other status raises ``ValueError`` -- both at the
        pre-check and, defensively, at the UPDATE WHERE clause
        (mid-operation state change is caught the same way
        ``cancel_if_active`` handles it).

        Returns
        -------
        MemoryJob
            The cancelled job in its post-transition state.

        Raises
        ------
        JobNotFoundError
            Unknown ``job_id``.
        ValueError
            Status not in ``allowed_statuses``, or the state
            changed between the pre-check and the UPDATE.
        """
        if not allowed_statuses:
            raise ValueError(
                "allowed_statuses must not be empty"
            )
        # Validate upfront so the SQL IN clause is safe to build.
        for s in allowed_statuses:
            if not isinstance(s, str) or not s:
                raise ValueError(
                    f"allowed_statuses entries must be non-empty "
                    f"strings, got {s!r}"
                )

        async with self._lock:
            return await asyncio.to_thread(
                self._cancel_with_code_sync,
                job_id,
                error_code,
                error_message,
                tuple(allowed_statuses),
            )

    def _cancel_with_code_sync(
        self,
        job_id: str,
        error_code: str,
        error_message: str,
        allowed_statuses: Tuple[str, ...],
    ) -> MemoryJob:
        conn = self._require_conn()
        now = time.time()
        placeholders = ", ".join("?" for _ in allowed_statuses)
        with conn:
            row = conn.execute(
                "SELECT status FROM memory_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)

            current_status = row["status"]
            if current_status not in allowed_statuses:
                raise ValueError(
                    f"Cannot cancel job {job_id!r}: current "
                    f"status is {current_status!r}; expected "
                    f"one of {list(allowed_statuses)}."
                )

            cur = conn.execute(
                f"""
                UPDATE memory_jobs
                SET status = 'cancelled',
                    completed_at = ?,
                    error_code = ?,
                    error_message = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE job_id = ?
                  AND status IN ({placeholders})
                """,
                (
                    now, error_code, error_message, job_id,
                    *allowed_statuses,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(
                    f"Cannot cancel job {job_id!r}: it changed "
                    f"state during the operation."
                )

        refreshed = conn.execute(
            "SELECT * FROM memory_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if refreshed is None:
            raise JobNotFoundError(job_id)
        logger.info(
            "Cancelled memory job %s with code %r (was %r).",
            job_id, error_code, current_status,
        )
        return self._row_to_job(refreshed)

    # ------------------------------------------------------------------
    # Recovery (startup)
    # ------------------------------------------------------------------

    async def recover_stale_leases(self) -> int:
        """Reset `running` jobs whose lease has expired (§19.4).

        Called once at worker startup, BEFORE `worker.start()`. A
        crashed worker leaves jobs stuck in `running` with a lease
        that will never be renewed; this method flips them back to
        `retry_pending` so the new worker can pick them up.

        Returns the number of rows recovered (0 in the common case
        of a clean shutdown).
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._recover_stale_leases_sync
            )

    def _recover_stale_leases_sync(self) -> int:
        conn = self._require_conn()
        now = time.time()
        with conn:
            cur = conn.execute(
                """
                UPDATE memory_jobs
                SET status = 'retry_pending',
                    attempts = attempts + 1,
                    next_retry_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    error_code = 'LEASE_EXPIRED',
                    error_message = 'Worker died before completing'
                WHERE status = 'running'
                  AND (
                      lease_expires_at IS NULL
                      OR lease_expires_at <= ?
                  )
                """,
                (now, now),
            )
            count = cur.rowcount
        if count > 0:
            logger.warning(
                "Recovered %d stale lease(s) at startup", count
            )
        return count

    # ------------------------------------------------------------------
    # Heartbeat (worker)
    # ------------------------------------------------------------------

    async def update_heartbeat(self, job_id: str) -> None:
        """Extend the lease and update `heartbeat_at` (worker call)."""
        async with self._lock:
            await asyncio.to_thread(
                self._update_heartbeat_sync, job_id
            )

    def _update_heartbeat_sync(self, job_id: str) -> None:
        conn = self._require_conn()
        now = time.time()
        with conn:
            cur = conn.execute(
                """
                UPDATE memory_jobs
                SET heartbeat_at = ?,
                    lease_expires_at = ?,
                    snapshot_verified_at = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (now, now + self._LEASE_DURATION_SECONDS, now, job_id),
            )
            if cur.rowcount == 0:
                # The job may have been recovered or cancelled while
                # the worker was running. Do not raise here -- the
                # worker's next action will fail with a clearer
                # error from `complete()` / `fail()`.
                logger.debug(
                    "Heartbeat no-op for job %s (not running)", job_id
                )

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    async def get_by_id(self, job_id: str) -> Optional[MemoryJob]:
        async with self._lock:
            return await asyncio.to_thread(self._get_by_id_sync, job_id)

    def _get_by_id_sync(self, job_id: str) -> Optional[MemoryJob]:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM memory_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._row_to_job(row) if row is not None else None

    async def get_by_key(
        self, idempotency_key: str
    ) -> Optional[MemoryJob]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_by_key_sync, idempotency_key
            )

    def _get_by_key_sync(
        self, idempotency_key: str
    ) -> Optional[MemoryJob]:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM memory_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return self._row_to_job(row) if row is not None else None

    async def list_by_chat(
        self,
        chat_id: str,
        *,
        limit: int = 50,
    ) -> List[MemoryJob]:
        """Return recent jobs for a chat, newest first."""
        async with self._lock:
            return await asyncio.to_thread(
                self._list_by_chat_sync, chat_id, limit
            )

    def _list_by_chat_sync(
        self, chat_id: str, limit: int
    ) -> List[MemoryJob]:
        conn = self._require_conn()
        rows = conn.execute(
            """
            SELECT * FROM memory_jobs
            WHERE chat_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (chat_id, int(limit)),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    async def list_jobs(
        self,
        *,
        status: Optional[Union[MemoryJobStatus, str]] = None,
        chat_id: Optional[str] = None,
        trigger_type: Optional[Union[ExtractionTrigger, str]] = None,
        batch_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[MemoryJob]:
        """List jobs with optional filters, newest first.

        This is the general-purpose listing used by the jobs UI
        (``GET /api/memory/automation/jobs``) AND by the batch
        service (for pausing / resuming / cancelling a batch's
        jobs). Filters are AND-combined; ``None`` means "no
        filter on this field".

        Parameters
        ----------
        status
            Restrict to one status. Accepts both the
            ``MemoryJobStatus`` enum member and its string value.
        chat_id
            Restrict to jobs for one chat. Whitespace-only values
            are treated as ``None`` (no filter).
        trigger_type
            Restrict to one trigger type. Accepts both the
            ``ExtractionTrigger`` member and its string value.
        batch_id
            Restrict to jobs belonging to one batch. Whitespace-
            only values are treated as ``None`` (no filter).
        limit
            Page size, clamped to ``[1, 200]``. Default 50.
        offset
            Page offset, clamped to ``>= 0``. Default 0.

        Returns
        -------
        List[MemoryJob]
            Empty list when no rows match.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._list_jobs_sync,
                status, chat_id, trigger_type, batch_id,
                limit, offset,
            )

    def _list_jobs_sync(
        self,
        status: Optional[Union[MemoryJobStatus, str]],
        chat_id: Optional[str],
        trigger_type: Optional[Union[ExtractionTrigger, str]],
        batch_id: Optional[str],
        limit: int,
        offset: int,
    ) -> List[MemoryJob]:
        conn = self._require_conn()
        where_sql, params = self._build_jobs_filter(
            status, chat_id, trigger_type, batch_id
        )
        limit_int = max(1, min(int(limit), 200))
        offset_int = max(0, int(offset))

        sql = (
            f"SELECT * FROM memory_jobs {where_sql} "
            f"ORDER BY created_at DESC "
            f"LIMIT ? OFFSET ?"
        )
        rows = conn.execute(
            sql, params + [limit_int, offset_int]
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    async def count_jobs(
        self,
        *,
        status: Optional[Union[MemoryJobStatus, str]] = None,
        chat_id: Optional[str] = None,
        trigger_type: Optional[Union[ExtractionTrigger, str]] = None,
        batch_id: Optional[str] = None,
    ) -> int:
        """Count jobs matching the same filters as ``list_jobs``.

        Used by the API to compute ``has_more`` for pagination
        without fetching the full result set. The filter
        semantics are byte-identical to ``list_jobs`` -- both
        call the shared ``_build_jobs_filter`` helper.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._count_jobs_sync,
                status, chat_id, trigger_type, batch_id,
            )

    def _count_jobs_sync(
        self,
        status: Optional[Union[MemoryJobStatus, str]],
        chat_id: Optional[str],
        trigger_type: Optional[Union[ExtractionTrigger, str]],
        batch_id: Optional[str],
    ) -> int:
        conn = self._require_conn()
        where_sql, params = self._build_jobs_filter(
            status, chat_id, trigger_type, batch_id
        )
        sql = f"SELECT COUNT(*) FROM memory_jobs {where_sql}"
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def _build_jobs_filter(
        self,
        status: Optional[Union[MemoryJobStatus, str]],
        chat_id: Optional[str],
        trigger_type: Optional[Union[ExtractionTrigger, str]],
        batch_id: Optional[str] = None,
    ) -> tuple[str, List[Any]]:
        """Build the WHERE clause + params shared by list/count.

        Empty filter set -> ``("", [])``. Otherwise ->
        ``("WHERE ... AND ...", [params])``.

        Centralized here so ``list_jobs`` and ``count_jobs``
        cannot drift apart -- any future filter added to one must
        be added to ``_build_jobs_filter`` and both will pick it
        up automatically.
        """
        clauses: List[str] = []
        params: List[Any] = []

        if status is not None:
            status_value = (
                status.value
                if isinstance(status, MemoryJobStatus)
                else str(status)
            )
            clauses.append("status = ?")
            params.append(status_value)

        if chat_id is not None:
            clean_chat = chat_id.strip()
            if clean_chat:
                clauses.append("chat_id = ?")
                params.append(clean_chat)

        if trigger_type is not None:
            trigger_value = (
                trigger_type.value
                if isinstance(trigger_type, ExtractionTrigger)
                else str(trigger_type)
            )
            clauses.append("trigger_type = ?")
            params.append(trigger_value)

        if batch_id is not None:
            clean_batch = batch_id.strip()
            if clean_batch:
                clauses.append("batch_id = ?")
                params.append(clean_batch)

        if not clauses:
            return "", params
        return "WHERE " + " AND ".join(clauses), params

    async def count_by_status(
        self, status: MemoryJobStatus
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._count_by_status_sync, status
            )

    def _count_by_status_sync(
        self, status: MemoryJobStatus
    ) -> int:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM memory_jobs WHERE status = ?",
            (status.value,),
        ).fetchone()
        return int(row[0]) if row else 0

    async def get_cursor(self, chat_id: str) -> int:
        """Return the last_processed_turn_index for a chat (0 if none).

        The cursor is the ONLY authoritative record of how many
        turns a chat has processed. It advances ONLY on
        `complete()` (see §19.3). A chat with no row has
        implicitly processed 0 turns.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._get_cursor_sync, chat_id
            )

    def _get_cursor_sync(self, chat_id: str) -> int:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT last_processed_turn_index "
            "FROM memory_chat_cursors WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    async def get_cursor_or_none(self, chat_id: str) -> Optional[int]:
        """Return cursor value, or None when no row exists.

        Distinguishes the two states that `get_cursor()` collapses:

          * No row exists     -> None  (nothing ever processed;
                                        next turn is index 0)
          * Row value is 0    -> 0     (turn 0 was processed;
                                        next turn is index 1)

        The distinction matters for `pending_turns` calculation in
        `MemorySessionScanner`:

            pending = total_turns - cursor - 1  (row exists)
            pending = total_turns               (no row)

        Without this distinction, a fresh 1-turn chat would show
        `pending_turns=0` and be incorrectly skipped by batch
        scans that filter on `only_unprocessed=True`.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._get_cursor_or_none_sync, chat_id
            )

    def _get_cursor_or_none_sync(self, chat_id: str) -> Optional[int]:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT last_processed_turn_index "
            "FROM memory_chat_cursors WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return int(row[0]) if row is not None else None

    # ------------------------------------------------------------------
    # Batch CRUD (Phase 3)
    # ------------------------------------------------------------------
    #
    # The outbox owns the `memory_batches` table access (rather than
    # a separate repository) because:
    #
    #   * it already owns the shared SQLite connection and its
    #     asyncio.Lock -- adding another connection+lock would
    #     introduce deadlock risk for zero benefit,
    #   * the batch CRUD methods are tightly coupled to the
    #     memory_jobs table (aggregate_batch_progress joins both),
    #   * the MemoryBatchService (omweb.memory.batch.service)
    #     composes batch CRUD with job CRUD (cancel/requeue), so
    #     having both surfaces on the same object keeps the
    #     service thin and testable.

    async def create_batch(
        self,
        batch: "MemoryBatch",
    ) -> "MemoryBatch":
        """Insert a new batch row.

        The caller (``MemoryBatchService.create_batch``) is
        responsible for populating every field EXCEPT ``created_at``
        (which the model auto-fills) and the counters (which
        start at 0). This method does not validate business
        invariants -- it trusts the ``MemoryBatch`` instance
        that Pydantic already validated.

        Raises
        ------
        sqlite3.IntegrityError
            On a duplicate ``batch_id``. The caller's
            ``batch_id`` generator (``batch_<16 hex>``) makes
            this statistically impossible in practice, but the
            exception is NOT swallowed: a duplicate id is a
            caller bug worth surfacing, not masking.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._create_batch_sync, batch
            )

    def _create_batch_sync(self, batch: "MemoryBatch") -> "MemoryBatch":
        conn = self._require_conn()
        values = self._batch_to_row_values(batch)
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        sql = (
            f"INSERT INTO memory_batches ({columns}) "
            f"VALUES ({placeholders})"
        )
        with conn:
            conn.execute(sql, tuple(values.values()))
        logger.info(
            "Created batch %s (status=%s, project=%r, trigger=%r).",
            batch.batch_id,
            batch.status.value,
            batch.project_id,
            batch.triggered_by,
        )
        return batch

    async def get_batch(
        self, batch_id: str
    ) -> Optional["MemoryBatch"]:
        """Return one batch row, or None when the id is unknown."""
        async with self._lock:
            return await asyncio.to_thread(
                self._get_batch_sync, batch_id
            )

    def _get_batch_sync(
        self, batch_id: str
    ) -> Optional["MemoryBatch"]:
        conn = self._require_conn()
        row = conn.execute(
            "SELECT * FROM memory_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        return self._row_to_batch(row) if row is not None else None

    async def update_batch(
        self,
        batch_id: str,
        /,
        **fields: Any,
    ) -> "MemoryBatch":
        """Update the given fields on a batch row.

        Only columns that actually exist on the ``memory_batches``
        table are accepted -- an unknown key raises ``ValueError``
        (defense in depth: a typo in the caller would otherwise
        silently do nothing). ``batch_id`` is immutable and is
        rejected if passed.

        The ``batch_id`` parameter is positional-only (``/``,
        PEP 570). This makes ``update_batch("id", batch_id="x")``
        route the keyword ``batch_id`` into ``**fields`` rather
        than colliding with the positional parameter -- so the
        immutability check inside ``_update_batch_sync`` fires
        and raises ``ValueError`` (the intended contract).
        Without ``/``, Python raises a confusing ``TypeError``
        about duplicate arguments BEFORE our validation runs.

        Returns the post-update ``MemoryBatch``.

        Raises
        ------
        JobNotFoundError
            When ``batch_id`` does not exist.
        ValueError
            On ``batch_id`` in ``fields`` or an unknown field
            name.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._update_batch_sync, batch_id, fields
            )

    def _update_batch_sync(
        self,
        batch_id: str,
        fields: Dict[str, Any],
    ) -> "MemoryBatch":
        conn = self._require_conn()

        # Whitelist of updatable fields (matches the SQL schema
        # minus the immutable batch_id / created_at).
        _UPDATABLE = frozenset({
            "status", "triggered_by", "project_id",
            "chat_ids", "date_from", "date_to",
            "session_count", "job_count",
            "completed_jobs", "failed_jobs", "cancelled_jobs",
            "total_candidates", "total_memories_created",
            "estimated_cost_nano_usd", "actual_cost_nano_usd",
            "model_provider_id", "model_id", "min_confidence",
            "started_at", "paused_at", "completed_at",
            "cancelled_at", "last_progress_at",
            "error_message", "options",
        })

        # Translate model field names to SQL column names and
        # normalize enum / JSON values. Unknown fields raise so
        # the caller cannot silently persist nothing.
        column_values: Dict[str, Any] = {}
        for key, value in fields.items():
            if key == "batch_id":
                raise ValueError(
                    "batch_id is immutable; cannot be updated"
                )
            if key not in _UPDATABLE:
                raise ValueError(
                    f"unknown batch field {key!r}; "
                    f"updatable fields are "
                    f"{sorted(_UPDATABLE)}"
                )
            if key == "chat_ids":
                column_values["chat_ids_json"] = json.dumps(
                    list(value or [])
                )
            elif key == "options":
                column_values["options_json"] = json.dumps(
                    dict(value or {})
                )
            elif key == "status":
                status_value = (
                    value.value
                    if hasattr(value, "value")
                    else str(value)
                )
                column_values["status"] = status_value
            else:
                column_values[key] = value

        if not column_values:
            # No-op update; return current state.
            current = self._get_batch_sync(batch_id)
            if current is None:
                raise JobNotFoundError(batch_id)
            return current

        set_clause = ", ".join(
            f"{col} = ?" for col in column_values.keys()
        )
        params = list(column_values.values()) + [batch_id]

        with conn:
            cur = conn.execute(
                f"UPDATE memory_batches SET {set_clause} "
                f"WHERE batch_id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise JobNotFoundError(batch_id)

        refreshed = conn.execute(
            "SELECT * FROM memory_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if refreshed is None:
            raise JobNotFoundError(batch_id)
        logger.info(
            "Updated batch %s (fields=%s).",
            batch_id, sorted(fields.keys()),
        )
        return self._row_to_batch(refreshed)

    async def list_batches(
        self,
        *,
        status: Optional[Union["MemoryBatchStatus", str]] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List["MemoryBatch"]:
        """List batches, newest first. Same filter contract as jobs."""
        async with self._lock:
            return await asyncio.to_thread(
                self._list_batches_sync, status, limit, offset
            )

    def _list_batches_sync(
        self,
        status: Optional[Union["MemoryBatchStatus", str]],
        limit: int,
        offset: int,
    ) -> List["MemoryBatch"]:
        conn = self._require_conn()
        limit_int = max(1, min(int(limit), 200))
        offset_int = max(0, int(offset))

        if status is None:
            sql = (
                "SELECT * FROM memory_batches "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            params: List[Any] = [limit_int, offset_int]
        else:
            status_value = (
                status.value
                if hasattr(status, "value")
                else str(status)
            )
            sql = (
                "SELECT * FROM memory_batches "
                "WHERE status = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            params = [status_value, limit_int, offset_int]

        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_batch(r) for r in rows]

    async def count_batches(
        self,
        *,
        status: Optional[Union["MemoryBatchStatus", str]] = None,
    ) -> int:
        """Count batches matching the same filter as ``list_batches``."""
        async with self._lock:
            return await asyncio.to_thread(
                self._count_batches_sync, status
            )

    def _count_batches_sync(
        self,
        status: Optional[Union["MemoryBatchStatus", str]],
    ) -> int:
        conn = self._require_conn()
        if status is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_batches"
            ).fetchone()
        else:
            status_value = (
                status.value
                if hasattr(status, "value")
                else str(status)
            )
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_batches WHERE status = ?",
                (status_value,),
            ).fetchone()
        return int(row[0]) if row else 0

    async def aggregate_batch_progress(
        self, batch_id: str
    ) -> Optional[Dict[str, Any]]:
        """Aggregate live progress for a batch from ``memory_jobs``.

        Returns None when ``batch_id`` does not exist in
        ``memory_batches``. Otherwise returns a dict with:

          * ``total``                -- total jobs in the batch
          * ``by_status``            -- ``{status: count}`` map
          * ``completed`` / ``failed`` / ``cancelled``
                                     -- convenience totals
          * ``pending_work``         -- pending + retry_pending
                                        + running
          * ``total_candidates``     -- sum candidates_count
          * ``total_memories_created`` -- sum created_memories_count
          * ``actual_cost_nano_usd`` -- sum cost_nano_usd

        This is the AUTHORITATIVE live view. The batch row's own
        counters are historical snapshots; this aggregation runs
        directly against ``memory_jobs WHERE batch_id = ?`` so it
        cannot drift regardless of how many jobs were requeued,
        cancelled, or added out-of-band.
        """
        async with self._lock:
            return await asyncio.to_thread(
                self._aggregate_batch_progress_sync, batch_id
            )

    def _aggregate_batch_progress_sync(
        self, batch_id: str
    ) -> Optional[Dict[str, Any]]:
        conn = self._require_conn()

        # Existence check first so we can return None for
        # unknown batches rather than an all-zero dict that
        # would mask a caller typo.
        exists = conn.execute(
            "SELECT 1 FROM memory_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if exists is None:
            return None

        status_rows = conn.execute(
            """
            SELECT status, COUNT(*) AS cnt
            FROM memory_jobs
            WHERE batch_id = ?
            GROUP BY status
            """,
            (batch_id,),
        ).fetchall()
        by_status: Dict[str, int] = {
            str(r["status"]): int(r["cnt"]) for r in status_rows
        }
        total = sum(by_status.values())

        sum_row = conn.execute(
            """
            SELECT
                COALESCE(SUM(candidates_count), 0)         AS candidates,
                COALESCE(SUM(created_memories_count), 0)   AS created,
                COALESCE(SUM(cost_nano_usd), 0)            AS cost
            FROM memory_jobs
            WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()

        completed = by_status.get("completed", 0)
        failed = by_status.get("failed", 0)
        cancelled = by_status.get("cancelled", 0)
        pending_work = (
            by_status.get("pending", 0)
            + by_status.get("retry_pending", 0)
            + by_status.get("running", 0)
        )

        return {
            "total": total,
            "by_status": by_status,
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "pending_work": pending_work,
            "total_candidates": int(sum_row["candidates"]),
            "total_memories_created": int(sum_row["created"]),
            "actual_cost_nano_usd": int(sum_row["cost"]),
        }

    # ---- batch row <-> model conversion -----------------------------

    def _row_to_batch(self, row: sqlite3.Row) -> "MemoryBatch":
        """Convert a ``memory_batches`` row to a ``MemoryBatch``.

        Handles JSON decode for ``chat_ids_json`` and
        ``options_json``; coerces the enum-typed ``status``.
        """
        data: Dict[str, Any] = dict(row)

        raw_chat_ids = data.pop("chat_ids_json", None)
        if raw_chat_ids:
            try:
                parsed = json.loads(raw_chat_ids)
            except (json.JSONDecodeError, TypeError):
                parsed = []
            data["chat_ids"] = (
                parsed if isinstance(parsed, list) else []
            )
        else:
            data["chat_ids"] = []

        raw_options = data.pop("options_json", None)
        if raw_options:
            try:
                parsed_opts = json.loads(raw_options)
            except (json.JSONDecodeError, TypeError):
                parsed_opts = {}
            data["options"] = (
                parsed_opts if isinstance(parsed_opts, dict) else {}
            )
        else:
            data["options"] = {}

        data["status"] = MemoryBatchStatus(data["status"])
        return MemoryBatch(**data)

    def _batch_to_row_values(
        self, batch: "MemoryBatch"
    ) -> Dict[str, Any]:
        """Inverse of ``_row_to_batch`` -- every column, no defaults."""
        return {
            "batch_id": batch.batch_id,
            "status": batch.status.value,
            "triggered_by": batch.triggered_by,
            "project_id": batch.project_id,
            "chat_ids_json": json.dumps(list(batch.chat_ids)),
            "date_from": batch.date_from,
            "date_to": batch.date_to,
            "session_count": batch.session_count,
            "job_count": batch.job_count,
            "completed_jobs": batch.completed_jobs,
            "failed_jobs": batch.failed_jobs,
            "cancelled_jobs": batch.cancelled_jobs,
            "total_candidates": batch.total_candidates,
            "total_memories_created": batch.total_memories_created,
            "estimated_cost_nano_usd": batch.estimated_cost_nano_usd,
            "actual_cost_nano_usd": batch.actual_cost_nano_usd,
            "model_provider_id": batch.model_provider_id,
            "model_id": batch.model_id,
            "min_confidence": batch.min_confidence,
            "created_at": batch.created_at,
            "started_at": batch.started_at,
            "paused_at": batch.paused_at,
            "completed_at": batch.completed_at,
            "cancelled_at": batch.cancelled_at,
            "last_progress_at": batch.last_progress_at,
            "error_message": batch.error_message,
            "options_json": json.dumps(dict(batch.options)),
        }

    # ------------------------------------------------------------------
    # Row <-> model conversion
    # ------------------------------------------------------------------

    def _row_to_job(self, row: sqlite3.Row) -> MemoryJob:
        """Convert a `memory_jobs` row into a `MemoryJob`.

        Handles:
          * `decisions_json` TEXT -> `decisions` list;
          * enum value coercion for `status`, `job_type`,
            `trigger_type`, `scope_type`.

        Every column name in the `SELECT *` MUST match a field on
        `MemoryJob`; anything else is a schema/model drift bug that
        Pydantic catches immediately with `extra="forbid"`.
        """
        data: Dict[str, Any] = dict(row)

        raw_decisions = data.pop("decisions_json", None)
        if raw_decisions:
            try:
                data["decisions"] = json.loads(raw_decisions)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Corrupt decisions_json for job %s; using []",
                    data.get("job_id"),
                )
                data["decisions"] = []
        else:
            data["decisions"] = []

        data["status"] = MemoryJobStatus(data["status"])
        data["job_type"] = MemoryJobType(data["job_type"])
        data["trigger_type"] = ExtractionTrigger(data["trigger_type"])
        data["scope_type"] = MemoryScope(data["scope_type"])

        return MemoryJob(**data)

    def _job_to_row_values(self, job: MemoryJob) -> Dict[str, Any]:
        """Inverse of `_row_to_job` -- every column, no defaults.

        The order of keys is the INSERT column order and matches
        the order of placeholders in `_enqueue_sync`. SQLite does
        not care about column ORDER in an INSERT with named
        columns, but keeping the mapping deterministic makes
        debugging output stable.
        """
        values: Dict[str, Any] = {
            "job_id": job.job_id,
            "job_type": job.job_type.value,
            "chat_id": job.chat_id,
            "project_id": job.project_id,
            "scope_type": job.scope_type.value,
            "scope_id": job.scope_id,
            "trigger_type": job.trigger_type.value,
            "source_session_path": job.source_session_path,
            "start_turn_index": job.start_turn_index,
            "end_turn_index": job.end_turn_index,
            "last_processed_turn_index_at_enqueue":
                job.last_processed_turn_index_at_enqueue,
            "message_snapshot_hash": job.message_snapshot_hash,
            "snapshot_verified_at": job.snapshot_verified_at,
            "status": job.status.value,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "model_provider_id": job.model_provider_id,
            "model_id": job.model_id,
            "idempotency_key": job.idempotency_key,
            "lease_owner": job.lease_owner,
            "lease_expires_at": job.lease_expires_at,
            "heartbeat_at": job.heartbeat_at,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "next_retry_at": job.next_retry_at,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "candidates_count": job.candidates_count,
            "created_memories_count": job.created_memories_count,
            "cost_nano_usd": job.cost_nano_usd,
            "decisions_json": json.dumps(job.decisions),
        }
        # Defensive batch_id handling: inline the column into the
        # INSERT only when the job actually carries a batch id.
        #
        # Why this matters
        # ----------------
        # 1. Non-batch jobs (the vast majority): the column is
        #    OMITTED from the INSERT entirely. On a v3 database,
        #    SQLite applies the column default (NULL). On a v2
        #    database (migration not yet applied), the INSERT
        #    succeeds unchanged -- no reference to a column that
        #    does not exist. This is what keeps a rolling upgrade
        #    from breaking the moment `outbox.py` is deployed
        #    ahead of `migrations.py`'s v3.
        #
        # 2. Batch jobs (Phase 3): `job.batch_id` is non-None, so
        #    the column IS included. On a v3 database the INSERT
        #    persists the link. On a v2 database it fails with
        #    "no such column: batch_id" -- which is the CORRECT
        #    signal that the operator must run the migration
        #    before enabling the batch subsystem.
        if getattr(job, "batch_id", None) is not None:
            values["batch_id"] = job.batch_id
        return values


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DuplicateJobError",
    "JobNotFoundError",
    "JobResult",
    "MemoryOutbox",
]
