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
        return {
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DuplicateJobError",
    "JobNotFoundError",
    "JobResult",
    "MemoryOutbox",
]
