# backend/omweb/memory/jobs/worker.py
"""
PELDRUN Memory Platform - Phase 1-C: Production Extraction Worker.

Long-running worker that drains the durable job queue
(`MemoryOutbox`) by claiming, processing, and transitioning
`MemoryJob` rows through their lifecycle.

Two operating modes
-------------------

**Stub mode (Phase 1-A backward-compatible default).**
Constructed WITHOUT `scanner`, `orchestrator`, and
`write_pipeline_provider` -- the worker returns an empty
`JobResult` from `_process_job` without any extraction work.
This is what Phase 1-A's queue-mechanics tests rely on.

**Production mode (Phase 1-C).**
Constructed WITH all three dependencies -- the worker runs the
full extraction pipeline end-to-end:

    claim  ->  verify snapshot  ->  load session
       ->  extract via orchestrator
       ->  convert candidates to MemoryEntry rows
       ->  persist via write pipeline
       ->  complete (advances cursor) or fail

Why the mode distinction
------------------------

Keeping both modes in ONE class (rather than two worker
classes) means:

  * a single code path for the queue mechanics
    (claim/heartbeat/backoff/recovery) -- tested once,
    trusted everywhere,

  * the Phase 1-A test doubles (which subclass this worker
    and override `_process_job`) keep working without
    modification,

  * swapping into production is a one-line change at the
    call site (pass the three deps), not a class swap.

Design invariants
-----------------

1. **Snapshot verification is the FIRST production step.**
   Before any LLM call or DB write, `_verify_snapshot` re-reads
   the session file and compares its hash to
   `job.message_snapshot_hash`. A mismatch fails the job with
   `SESSION_CHANGED` -- the worker NEVER retries this failure
   (per §21.3, user-manual retry is the only remediation).

2. **Failure classification is centralized.**
   `_handle_job_error` decides retry vs terminal-fail based on
   the exception type. It is the ONLY place that calls
   `outbox.retry()` or `outbox.fail()`. Every other method
   either succeeds or raises a specific exception.

3. **The write pipeline is re-fetched per job.**
   `write_pipeline_provider()` is called on EVERY job, not
   captured at construction. This is what allows a settings
   reload (`memory_service.reload_settings`) to change
   `mode=read_write -> read_only` and have the NEXT job honor
   the new mode without restarting the worker.

4. **Candidate -> entry conversion is atomic-ish.**
   Every candidate that reaches `_candidate_to_entry` is fully
   validated (by the orchestrator) and has evidence. The
   conversion itself cannot fail on well-formed input; the
   `try/except` in `_process_job` is defense in depth against
   future drift between orchestrator and worker contracts.

5. **Cursor advancement is `complete()`'s job.**
   The worker NEVER touches the cursor directly. A successful
   `_process_job` returns a `JobResult`; the caller
   (`_run_loop`) calls `outbox.complete(job_id, result)` which
   advances the cursor ATOMICALLY with the status update.
   `_handle_job_error` uses `fail()`/`retry()` which do NOT
   advance the cursor. This preserves §19.3 across every path.

6. **`_JobAlreadyTerminal` is a control-flow signal.**
   When `_verify_snapshot` fails the job, `_process_job` raises
   `_JobAlreadyTerminal`. `_run_loop` catches it, logs, and
   moves on without calling `complete()` (which would advance
   the cursor erroneously). It is NOT an error condition.

7. **Heartbeat and process share the snapshot check.**
   `_verify_snapshot` is called from BOTH `_process_job` (once,
   before work) and `_heartbeat_loop` (periodically, while the
   job runs). A session modified mid-extraction is caught by
   the next heartbeat, even if the modification happened AFTER
   `_process_job`'s initial check.

Cancellation contract
---------------------

`asyncio.CancelledError` is propagated immediately. Every
`finally` block nulls out `_current_job_id` and lets the lease
expire naturally -- `recover_stale_leases` on the next startup
resets the job to `retry_pending`.

Backoff schedule
----------------

Exponential with a cap:

    attempt 0 -> 60s   (first retry after 1 minute)
    attempt 1 -> 120s
    attempt 2 -> 240s
    attempt 3 -> 480s
    attempt N -> min(60 * 2**N, 900s)   (capped at 15 min)

Tests override `_compute_backoff_seconds` to return 0.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from typing import TYPE_CHECKING, Callable, List, Optional

from omweb.memory.jobs.models import MemoryJob
from omweb.memory.jobs.outbox import (
    JobNotFoundError,
    JobResult,
    MemoryOutbox,
)


if TYPE_CHECKING:
    from omweb.memory.extraction.orchestrator import (
        ExtractionOrchestrator,
    )
    from omweb.memory.extraction.session_scanner import (
        MemorySessionScanner,
    )
    from omweb.memory.manager.write_pipeline import (
        MemoryWritePipeline,
    )


logger = logging.getLogger("omweb.memory.jobs.worker")


# ---------------------------------------------------------------------------
# Control-flow signal
# ---------------------------------------------------------------------------


class _JobAlreadyTerminal(Exception):
    """Raised when `_process_job` has already transitioned the job.

    Used by `_verify_snapshot`'s failure paths: the job was moved
    to `failed` with `SESSION_CHANGED` / `SESSION_MISSING`, and
    `_run_loop` must NOT call `complete()` on it (which would
    advance the cursor). The exception carries no payload --
    `_run_loop` just logs and moves on.
    """


# ---------------------------------------------------------------------------
# Worker id helper
# ---------------------------------------------------------------------------


def _default_worker_id() -> str:
    return (
        f"worker_{socket.gethostname()}"
        f"_{os.getpid()}"
        f"_{uuid.uuid4().hex[:8]}"
    )


# ---------------------------------------------------------------------------
# MemoryExtractionWorker
# ---------------------------------------------------------------------------


class MemoryExtractionWorker:
    """Drains `memory_jobs` FIFO-per-chat.

    See the module docstring for the mode distinction and design
    invariants. In stub mode (no deps injected), all extra methods
    (`_verify_snapshot`, `_candidate_to_entry`) are unused; in
    production mode, they run on every job.
    """

    _DEFAULT_POLL_INTERVAL_SECONDS: float = 1.0
    _DEFAULT_HEARTBEAT_INTERVAL_SECONDS: float = 5.0
    _BASE_BACKOFF_SECONDS: int = 60
    _MAX_BACKOFF_SECONDS: int = 15 * 60

    def __init__(
        self,
        outbox: MemoryOutbox,
        *,
        scanner: Optional["MemorySessionScanner"] = None,
        orchestrator: Optional["ExtractionOrchestrator"] = None,
        write_pipeline_provider: Optional[
            Callable[[], "MemoryWritePipeline"]
        ] = None,
        worker_id: Optional[str] = None,
        heartbeat_interval_seconds: Optional[float] = None,
        poll_interval_seconds: Optional[float] = None,
    ) -> None:
        """
        Parameters
        ----------
        outbox
            The job queue.
        scanner
            Session scanner. When `None`, worker runs in stub
            mode (returns empty `JobResult` from `_process_job`).
        orchestrator
            Extraction orchestrator. When `None`, worker runs in
            stub mode.
        write_pipeline_provider
            Callable returning the current `MemoryWritePipeline`.
            When `None`, worker runs in stub mode. Re-invoked on
            every job so a settings reload is honored without
            restarting the worker.
        worker_id
            Unique id for lease ownership. Auto-generated when
            `None`.
        heartbeat_interval_seconds
            How often to renew the lease + verify snapshot.
        poll_interval_seconds
            How often to poll the queue when idle.
        """
        self._outbox = outbox
        self._scanner = scanner
        self._orchestrator = orchestrator
        self._write_pipeline_provider = write_pipeline_provider
        self._worker_id = worker_id or _default_worker_id()

        self._heartbeat_interval_seconds = (
            float(heartbeat_interval_seconds)
            if heartbeat_interval_seconds is not None
            else self._DEFAULT_HEARTBEAT_INTERVAL_SECONDS
        )
        self._poll_interval_seconds = (
            float(poll_interval_seconds)
            if poll_interval_seconds is not None
            else self._DEFAULT_POLL_INTERVAL_SECONDS
        )
        if self._heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be > 0")
        if self._poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")

        self._run_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._current_job_id: Optional[str] = None
        self._current_job: Optional[MemoryJob] = None

    # ------------------------------------------------------------------
    # Production-mode predicate
    # ------------------------------------------------------------------

    @property
    def is_production_mode(self) -> bool:
        """True when all three extraction deps were injected."""
        return (
            self._scanner is not None
            and self._orchestrator is not None
            and self._write_pipeline_provider is not None
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Recover stale leases and start the two background tasks."""
        if self._run_task is not None and not self._run_task.done():
            logger.debug(
                "Worker %s already running; start() is a no-op.",
                self._worker_id,
            )
            return

        try:
            recovered = await self._outbox.recover_stale_leases()
            if recovered > 0:
                logger.info(
                    "Worker %s recovered %d stale lease(s) on startup.",
                    self._worker_id, recovered,
                )
        except Exception as exc:
            logger.warning(
                "Worker %s failed to recover stale leases "
                "(continuing anyway): %s",
                self._worker_id, exc,
            )

        self._shutdown_event.clear()
        self._run_task = asyncio.create_task(
            self._run_loop(),
            name=f"MemoryWorker-Run-{self._worker_id}",
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name=f"MemoryWorker-Heartbeat-{self._worker_id}",
        )
        logger.info(
            "MemoryExtractionWorker started "
            "(worker_id=%s, mode=%s, heartbeat_interval=%.1fs, "
            "poll_interval=%.1fs).",
            self._worker_id,
            "production" if self.is_production_mode else "stub",
            self._heartbeat_interval_seconds,
            self._poll_interval_seconds,
        )

    async def stop(self, *, timeout_seconds: int = 30) -> None:
        """Graceful shutdown within ``timeout_seconds``."""
        if self._run_task is None:
            return

        self._shutdown_event.set()

        tasks = [
            t for t in (self._run_task, self._heartbeat_task)
            if t is not None and not t.done()
        ]
        if tasks:
            done, pending = await asyncio.wait(
                tasks, timeout=float(timeout_seconds)
            )
            if pending:
                logger.warning(
                    "Worker %s did not stop within %ds; cancelling "
                    "%d task(s). The current job's lease will "
                    "expire naturally.",
                    self._worker_id, timeout_seconds, len(pending),
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        self._run_task = None
        self._heartbeat_task = None
        self._current_job_id = None
        self._current_job = None
        logger.info(
            "MemoryExtractionWorker stopped (worker_id=%s).",
            self._worker_id,
        )

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Claim -> process -> transition, forever, until shutdown."""
        while not self._shutdown_event.is_set():
            try:
                job = await self._outbox.claim_one(self._worker_id)
                if job is None:
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(),
                            timeout=self._poll_interval_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                self._current_job_id = job.job_id
                self._current_job = job
                try:
                    result = await self._process_job(job)
                    await self._outbox.complete(job.job_id, result)
                except _JobAlreadyTerminal:
                    logger.info(
                        "Job %s was already transitioned to a "
                        "terminal state by _process_job; "
                        "skipping complete().",
                        job.job_id,
                    )
                except JobNotFoundError:
                    logger.warning(
                        "Job %s disappeared between claim and "
                        "complete (external cancel or stale-lease "
                        "recovery). Ignoring.",
                        job.job_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._handle_job_error(job, exc)
                finally:
                    self._current_job_id = None
                    self._current_job = None

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Worker %s run loop error; will retry in 1s: %s",
                    self._worker_id, exc,
                )
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------------
    # Job processing
    # ------------------------------------------------------------------

    async def _process_job(self, job: MemoryJob) -> JobResult:
        """Process one job and return its result.

        STUB MODE: returns an empty ``JobResult()`` immediately
        (Phase 1-A behavior -- no extraction deps injected).

        PRODUCTION MODE: runs the full extraction pipeline:

          1. Verify the snapshot (fails the job if the session
             changed since enqueue -- `_JobAlreadyTerminal`).
          2. Load the session via the scanner.
          3. Call `orchestrator.extract()`.
          4. Convert each accepted candidate to a `MemoryEntry`.
          5. Persist all entries via the write pipeline
             (`save_many`).
          6. Return a `JobResult` with the counts + decisions.

        Every failure path either:
          * raises `_JobAlreadyTerminal` (job already failed),
          * raises `JobNotFoundError` (job already gone),
          * raises a plain exception (retried by
            `_handle_job_error`).
        """
        if not self.is_production_mode:
            logger.debug(
                "Processing job %s in STUB mode (no extraction "
                "deps injected; returning empty result).",
                job.job_id,
            )
            return JobResult()

        # --- Step 1: verify snapshot -------------------------------
        if not await self._verify_snapshot(job):
            # _verify_snapshot already failed the job.
            raise _JobAlreadyTerminal()

        # --- Step 2: load session ----------------------------------
        assert self._scanner is not None  # guaranteed by is_production_mode
        detail = await self._scanner.get_session_by_id(job.chat_id)
        if detail is None:
            logger.warning(
                "Job %s: session %r is unreadable; failing.",
                job.job_id, job.chat_id,
            )
            await self._outbox.fail(
                job.job_id,
                "SESSION_MISSING",
                f"Session {job.chat_id!r} could not be read.",
            )
            raise _JobAlreadyTerminal()

        payload, source = detail.get_extraction_payload()

        # --- Step 3: extract ---------------------------------------
        assert self._orchestrator is not None
        result = await self._orchestrator.extract(
            job=job,
            session=payload,
            recommended_source=source,
        )

        # --- Step 4: convert candidates to entries -----------------
        entries = []
        conversion_errors: List[str] = []
        for idx, candidate in enumerate(result.candidates):
            try:
                entry = self._candidate_to_entry(candidate, job)
                entries.append(entry)
            except Exception as exc:
                logger.warning(
                    "Job %s: failed to build MemoryEntry from "
                    "candidate %d (skipping): %s",
                    job.job_id, idx, exc,
                )
                conversion_errors.append(
                    f"candidate {idx}: {exc}"
                )

        # --- Step 5: persist via write pipeline --------------------
        created_count = 0
        persistence_decisions: List[dict] = []
        if entries:
            try:
                assert self._write_pipeline_provider is not None
                pipeline = self._write_pipeline_provider()
                scope_ref = (
                    f"{job.scope_type.value}:{job.scope_id}"
                )
                outcomes = await pipeline.save_many(
                    entries, scope_ref=scope_ref,
                )
                for outcome in outcomes:
                    if outcome.created_new_active:
                        created_count += 1
                    persistence_decisions.append({
                        "action": outcome.action.value,
                        "reason": outcome.reason or "",
                        "target_id": outcome.target_id,
                        "error": outcome.error,
                    })
            except Exception as exc:
                # Pipeline-level failure (backend unavailable,
                # settings mode changed mid-batch). The candidates
                # are lost for this job; the retry path will
                # re-extract (a new snapshot handles the fact
                # that the session may have changed).
                logger.warning(
                    "Job %s: write pipeline failed; job will "
                    "be retried: %s", job.job_id, exc,
                )
                raise

        return JobResult(
            candidates_count=len(result.candidates),
            created_memories_count=created_count,
            cost_nano_usd=result.cost_nano_usd,
            decisions=result.decisions + persistence_decisions,
        )

    # ------------------------------------------------------------------
    # Snapshot verification
    # ------------------------------------------------------------------

    async def _verify_snapshot(self, job: MemoryJob) -> bool:
        """Re-hash the session file; compare against `job.message_snapshot_hash`.

        Returns
        -------
        bool
            True when the file matches the stored hash (or when
            `job.source_session_path` is None -- meaning the job
            was enqueued by an older code path that predates
            snapshot tracking; we cannot verify, so we allow).

            False when the file is missing OR the hash differs.
            In both cases the job has ALREADY been transitioned
            to `failed` by this method; callers raise
            `_JobAlreadyTerminal` when `False` is returned.

        Why no retry
        ------------
        A snapshot mismatch means the user (or another system) is
        actively writing to the session. Retrying would:
          * re-extract from an ever-changing document
            (unbounded cost, ambiguous output),
          * mask the "your chat is still being written to" signal
            the user needs.

        Per §21.3, the correct response is a terminal
        `SESSION_CHANGED` failure. The user can retry manually
        once the session is quiet.
        """
        if not job.source_session_path:
            return True

        from omweb.memory.extraction.snapshot import (
            compute_snapshot_from_file,
        )

        current = await asyncio.to_thread(
            compute_snapshot_from_file, job.source_session_path,
        )
        if current is None:
            await self._outbox.fail(
                job.job_id,
                "SESSION_MISSING",
                f"Session file missing or unreadable: "
                f"{job.source_session_path}",
            )
            return False

        if current != job.message_snapshot_hash:
            await self._outbox.fail(
                job.job_id,
                "SESSION_CHANGED",
                f"Session changed since enqueue "
                f"({job.message_snapshot_hash[:8]} -> "
                f"{current[:8]}); manual retry required.",
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Candidate -> MemoryEntry
    # ------------------------------------------------------------------

    def _candidate_to_entry(self, candidate, job: MemoryJob):
        """Convert one `MemoryCandidate` into a `MemoryEntry`.

        The conversion is deterministic given the candidate + job:

          * scope: candidate's override if both fields set,
                   otherwise the job's scope,
          * provenance: `MemorySourceType.EXTRACTION` with
                       `source_chat_id` + `source_run_id`,
          * extraction_meta: full audit trail (job_id,
                            trigger_type, model, prompt version,
                            confidence, evidence ids).

        Raises
        ------
        ValueError
            When the candidate lacks evidence. The orchestrator
            already rejects such candidates, but this is defense
            in depth against contract drift.
        """
        from omweb.memory.extraction.prompts import PROMPT_VERSION
        from peldrun.memory.domain.entry import (
            MemoryEntry,
            MemoryProvenance,
        )
        from peldrun.memory.domain.enums import MemorySourceType
        from peldrun.memory.domain.extraction_meta import (
            ExtractionMeta,
        )

        if candidate.evidence is None:
            raise ValueError(
                "candidate has no evidence; cannot build "
                "ExtractionMeta with empty source_turn_indexes"
            )

        scope_type = job.scope_type
        scope_id = job.scope_id
        if candidate.scope_type is not None:
            scope_type = candidate.scope_type
        if candidate.scope_id:
            scope_id = candidate.scope_id

        provenance = MemoryProvenance(
            source_type=MemorySourceType.EXTRACTION,
            source_chat_id=job.chat_id,
            source_run_id=job.job_id,
        )

        meta = ExtractionMeta(
            job_id=job.job_id,
            trigger_type=job.trigger_type,
            source_chat_id=job.chat_id,
            source_turn_indexes=[candidate.evidence.turn_index],
            model_provider_id=job.model_provider_id,
            model_id=job.model_id,
            prompt_version=PROMPT_VERSION,
            confidence_at_creation=candidate.confidence,
            evidence_ids=[f"{job.job_id}:ev:0"],
        )

        return MemoryEntry.create(
            content=candidate.content,
            memory_type=candidate.memory_type,
            scope_type=scope_type,
            scope_id=scope_id,
            provenance=provenance,
            extraction_meta=meta,
            importance=candidate.importance,
            confidence=candidate.confidence,
            tags=candidate.tags,
        )

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    async def _handle_job_error(
        self, job: MemoryJob, exc: Exception
    ) -> None:
        """Decide between retry and terminal failure."""
        logger.warning(
            "Job %s failed on attempt %d/%d: %s",
            job.job_id,
            job.attempts + 1,
            job.max_attempts,
            exc,
        )

        if job.attempts + 1 >= job.max_attempts:
            try:
                await self._outbox.fail(
                    job.job_id,
                    "EXTRACTION_FAILED",
                    str(exc),
                )
            except JobNotFoundError:
                logger.warning(
                    "Job %s disappeared before fail(); ignoring.",
                    job.job_id,
                )
            return

        next_retry_at = (
            time.time()
            + self._compute_backoff_seconds(job.attempts)
        )
        try:
            await self._outbox.retry(job.job_id, next_retry_at)
        except JobNotFoundError:
            logger.warning(
                "Job %s disappeared before retry(); ignoring.",
                job.job_id,
            )

    def _compute_backoff_seconds(self, attempts: int) -> int:
        """Exponential backoff, capped at ``_MAX_BACKOFF_SECONDS``."""
        raw = self._BASE_BACKOFF_SECONDS * (2 ** max(0, attempts))
        return min(raw, self._MAX_BACKOFF_SECONDS)

    # ------------------------------------------------------------------
    # Heartbeat loop
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Renew the active job's lease + verify snapshot on every tick."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._heartbeat_interval_seconds,
                )
                break
            except asyncio.TimeoutError:
                pass

            job_id = self._current_job_id
            if job_id is None:
                continue

            # Snapshot re-verification (production mode only --
            # stub jobs have no source_session_path to verify).
            job = self._current_job
            if (
                job is not None
                and self.is_production_mode
                and job.source_session_path
            ):
                try:
                    still_valid = await self._verify_snapshot(job)
                    if not still_valid:
                        # Job is now `failed` in the DB. The
                        # processing loop's next iteration will
                        # notice via the JobNotFoundError /
                        # _JobAlreadyTerminal path -- we do NOT
                        # raise here because the heartbeat task
                        # must stay alive for subsequent jobs.
                        logger.info(
                            "Heartbeat: job %s failed snapshot "
                            "verification mid-flight; the "
                            "processing loop will notice on next "
                            "check.",
                            job_id,
                        )
                        continue
                except Exception as exc:
                    logger.debug(
                        "Heartbeat snapshot check for job %s "
                        "raised (continuing): %s", job_id, exc,
                    )
                    # Fall through to lease renewal.

            try:
                await self._outbox.update_heartbeat(job_id)
            except Exception as exc:
                logger.debug(
                    "Heartbeat for job %s failed: %s", job_id, exc,
                )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def worker_id(self) -> str:
        """Return this worker's unique identifier."""
        return self._worker_id

    @property
    def is_running(self) -> bool:
        """True when the run loop task is active."""
        return (
            self._run_task is not None
            and not self._run_task.done()
        )

    @property
    def current_job_id(self) -> Optional[str]:
        """The job currently being processed, or ``None``."""
        return self._current_job_id


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["MemoryExtractionWorker"]
