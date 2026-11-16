# backend/omweb/memory/batch/service.py
"""
PELDRUN Memory Platform - Batch Orchestration Service.

The ``MemoryBatchService`` composes three subsystems into the
batch operation:

    * ``MemoryOutbox``          -- batch-row + job-row persistence,
    * ``MemorySessionScanner``  -- session discovery for cost
                                    estimation,
    * ``TriggerRouter``         -- job enqueueing.

Design invariants
-----------------

1. **Composition, not inheritance.**
   Every primitive the service needs is injected. The service
   never touches SQL, never scans the filesystem, never calls
   the LLM. It only coordinates.

2. **Self-healing on read.**
   ``get_batch`` recomputes live progress from
   ``memory_jobs WHERE batch_id = ?`` and, when a RUNNING batch
   has no pending work left, transitions it to COMPLETED with a
   single UPDATE. This keeps the "batch state" and the "batch
   reality" always in sync without background workers.
   PAUSED batches are NOT auto-completed -- the pause is a
   deliberate user action, and its state must survive job
   completion.

3. **Pause leaves running jobs alone.**
   Pausing cancels only ``pending`` and ``retry_pending`` jobs
   with ``error_code='BATCH_PAUSED'``. A ``running`` job keeps
   its lease; it finishes naturally. This is the only safe
   semantics: cancelling an in-flight LLM call wastes the tokens
   already spent and races the worker's write pipeline.

4. **Resume is requeue-of-paused-jobs.**
   Resume finds every job in the batch whose ``error_code`` is
   ``BATCH_PAUSED`` and requeues it. ``requeue`` (already
   defined in ``MemoryOutbox``) resets attempts, clears error
   fields, and preserves ``batch_id`` + ``idempotency_key`` --
   exactly the state a fresh worker attempt needs.

5. **Cancel is comprehensive.**
   Cancel stops every non-terminal job (pending, retry_pending,
   running) with ``error_code='BATCH_CANCELLED'``. There is no
   "graceful" variant -- the user asked for it to stop.

6. **Cost estimation is a heuristic, not a quote.**
   ``estimate_cost`` returns a rough upper bound derived from
   (a) ``pending_turns`` per session and (b) a conservative
   per-turn + per-job token budget. The exact figure depends on
   the model's pricing, which we do not fetch (see
   ``_PROMPT_OVERHEAD_TOKENS`` docstring).

7. **Settings are read fresh, not snapshotted.**
   ``settings_provider`` is called on every ``create_batch`` --
   the AI enabled/disabled state and the extraction model can
   change between batches. A ``MemoryBatch`` row snapshots the
   model it used at creation time (``model_provider_id`` /
   ``model_id``) for the audit trail.

Exceptions
----------

The service raises three specific exceptions that the API
layer (M6c) maps to HTTP status codes:

    * ``BatchNotFoundError``             -> 404
    * ``BatchStateError``                -> 409
    * ``BatchServiceUnavailableError``   -> 503

Every unexpected exception from a lower layer propagates
unchanged; the API layer maps it to 500.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from omweb.memory.batch.models import (
    MemoryBatch,
    MemoryBatchStatus,
)
from omweb.memory.extraction.session_scanner import (
    MemorySessionScanner,
)
from omweb.memory.extraction.trigger_router import TriggerRouter
from omweb.memory.jobs.outbox import (
    JobNotFoundError,
    MemoryOutbox,
)


logger = logging.getLogger("omweb.memory.batch.service")


# ---------------------------------------------------------------------------
# Heuristic constants -- cost estimation only
# ---------------------------------------------------------------------------


_TOKENS_PER_TURN: int = 500
"""Rough input-token cost per conversation turn.

A typical turn is one user message + one assistant reply. Half
a kilotoken is conservative enough for English + common
languages, slightly low for long Arabic replies (the platform's
primary use case). The estimate is an UPPER bound guidance, not
a quote -- actual cost shows up in ``token_ledger`` after the
job runs.
"""

_PROMPT_OVERHEAD_TOKENS: int = 800
"""Fixed prompt + schema overhead per extraction job.

Covers the system prompt (v6, ~300 tokens), the JSON schema
representation, and per-job scaffolding.
"""

_MAX_COMPLETION_TOKENS: int = 1200
"""Upper bound on completion tokens per job.

Matches ``MemoryAIExtractionSettings.max_output_tokens`` default.
The setting could be higher (up to 32k); the estimator uses the
DEFAULT deliberately -- it is what most deployments configure,
and inflating the estimate based on the cap would make the
"estimated cost" card uselessly pessimistic.
"""

_COST_NANO_USD_PER_MILLION_TOKENS: int = 500_000_000
"""Conservative blended cost rate, in nano-USD per million tokens.

$0.50 per million tokens is a mid-range conservative blended
input+output rate for cloud models circa 2026. Local models are
much cheaper (often zero); using $0.50 gives a safe upper bound
that prevents the UI from under-promising. The actual cost is
whatever the provider reports via ``token_ledger``.

If the operator needs a precise estimate, the correct workflow
is to run a small test-model first (via ``/test-model``) and
scale the observed cost by the session count.
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BatchNotFoundError(Exception):
    """Raised when a ``batch_id`` does not exist.

    Maps to HTTP 404 in the API layer.
    """

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        super().__init__(f"Batch not found: {batch_id}")


class BatchStateError(Exception):
    """Raised on an illegal state transition.

    Examples:

      * pausing a batch that is not RUNNING,
      * resuming a batch that is not PAUSED,
      * cancelling a batch that is already terminal.

    Maps to HTTP 409 in the API layer. Distinct from
    ``BatchNotFoundError`` because the correct client response is
    "refresh and pick a different action", not "the batch is
    gone".
    """

    def __init__(self, batch_id: str, reason: str) -> None:
        self.batch_id = batch_id
        self.reason = reason
        super().__init__(
            f"Cannot operate on batch {batch_id!r}: {reason}"
        )


class BatchServiceUnavailableError(Exception):
    """Raised when the service cannot create a batch because
    the AI extraction subsystem is not ready.

    Two conditions trigger this:

      * AI extraction is disabled in settings (``ai.enabled`` is
        False OR ``ai.extraction_enabled`` is False),
      * no extraction model is configured (provider_id or
        model_id missing on ``ai.extraction``).

    Maps to HTTP 503 in the API layer. Distinct from
    ``BatchStateError`` (which is a runtime conflict) --
    this is a "configure the subsystem first" pre-condition.
    """


# ---------------------------------------------------------------------------
# MemoryBatchService
# ---------------------------------------------------------------------------


class MemoryBatchService:
    """Coordination layer for the retroactive batch extraction subsystem.

    See the module docstring for the design contract. This class
    holds NO state of its own beyond the three collaborator
    references; all persistence lives in ``MemoryOutbox``.
    """

    def __init__(
        self,
        *,
        outbox: MemoryOutbox,
        scanner: MemorySessionScanner,
        trigger_router: TriggerRouter,
        settings_provider: Callable[[], Optional[Any]],
    ) -> None:
        """
        Parameters
        ----------
        outbox
            Job + batch persistence layer.
        scanner
            Session discovery (used by ``estimate_cost``).
        trigger_router
            Job enqueueing (used by ``create_batch``).
        settings_provider
            Zero-argument callable returning the CURRENT
            ``MemoryAISettings`` object, or None when the AI
            subsystem is not configured. Called fresh on every
            ``create_batch`` -- the settings provider is the
            single source of truth, so a settings change
            between batches is observed automatically.
        """
        self._outbox = outbox
        self._scanner = scanner
        self._trigger_router = trigger_router
        self._settings_provider = settings_provider

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    async def create_batch(
        self,
        *,
        triggered_by: str = "manual",
        project_id: Optional[str] = None,
        chat_ids: Optional[List[str]] = None,
        date_from: Optional[float] = None,
        date_to: Optional[float] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> MemoryBatch:
        """Create a batch and enqueue its jobs.

        Flow
        ----
          1. Validate that AI extraction is enabled and a model
             is configured (via ``settings_provider``). Fail
             fast with ``BatchServiceUnavailableError``.
          2. Generate a fresh ``batch_id``.
          3. Persist a PENDING batch row (so the batch is
             discoverable even if step 5 fails).
          4. Call ``trigger_router.route_batch(batch_id=...)``,
             which scans sessions and enqueues one job per chat
             with pending turns.
          5. Transition the batch row to RUNNING (jobs > 0) or
             COMPLETED (jobs == 0), stamping the live counts
             and the cost estimate.

        Failure handling
        ----------------
        If step 4 raises, the batch row is updated to FAILED
        with ``error_message`` populated and the exception is
        re-raised. The caller (API) sees the exception and
        returns 500; the operator sees the FAILED batch in the
        UI and can diagnose.

        Returns
        -------
        MemoryBatch
            The batch in its post-transition state
            (``RUNNING`` or ``COMPLETED``).

        Raises
        ------
        BatchServiceUnavailableError
            AI extraction disabled or model not configured.
        Exception
            Any exception from ``trigger_router.route_batch``
            (propagated after marking the batch FAILED).
        """
        # --- 1. Settings gate --------------------------------------
        ai_settings = self._settings_provider()
        if ai_settings is None:
            raise BatchServiceUnavailableError(
                "AI extraction settings are not available."
            )
        if not getattr(ai_settings, "enabled", False):
            raise BatchServiceUnavailableError(
                "AI extraction is disabled (ai.enabled is false)."
            )
        if not getattr(ai_settings, "extraction_enabled", False):
            raise BatchServiceUnavailableError(
                "Extraction is not enabled "
                "(ai.extraction_enabled is false)."
            )
        extraction = getattr(ai_settings, "extraction", None)
        if extraction is None:
            raise BatchServiceUnavailableError(
                "Extraction model configuration missing."
            )
        provider_id = getattr(extraction, "provider_id", None)
        model_id = getattr(extraction, "model_id", None)

        # Only ``model_id`` is REQUIRED. ``provider_id`` is
        # OPTIONAL because the extraction worker's
        # ``_model_config_provider`` closure infers it from the
        # model vault at dispatch time (see
        # ``MemoryService._resolve_model_provider``). The
        # inference path is exactly what allowed the worker to
        # process the existing 4 successful jobs that were
        # queued before this gate was introduced.
        #
        # Rejecting a batch here for a missing ``provider_id``
        # would contradict the worker's own behavior -- the
        # worker WOULD process the resulting jobs successfully
        # via inference, so the gate would be a false negative.
        if not model_id:
            raise BatchServiceUnavailableError(
                "Extraction model not configured: model_id is "
                "required. (provider_id may be null -- the "
                "worker infers it from the model vault.)"
            )

        # --- 2. Generate batch id ----------------------------------
        batch_id = f"batch_{uuid4().hex[:16]}"

        # --- 3. Persist PENDING row first -------------------------
        #
        # Order matters: the row exists BEFORE any job is
        # enqueued. If route_batch raises (disk full, scanner
        # crash), the operator still sees a FAILED batch with
        # an error message instead of an invisible dangling
        # job set.
        # ``model_provider_id`` is a SNAPSHOT for the UI's audit
        # trail. When ``provider_id`` is None (the operator
        # picked a model without an explicit provider), we
        # store null rather than ``str(None) == "None"`` -- the
        # latter would be a confusing string that looks like a
        # legitimate provider name and could mislead the UI.
        # The worker still resolves the real provider via
        # ``_resolve_model_provider`` at dispatch time.
        initial_batch = MemoryBatch(
            batch_id=batch_id,
            status=MemoryBatchStatus.PENDING,
            triggered_by=triggered_by,
            project_id=project_id,
            chat_ids=chat_ids or [],
            date_from=date_from,
            date_to=date_to,
            model_provider_id=(
                str(provider_id) if provider_id else None
            ),
            model_id=str(model_id),
            min_confidence=getattr(
                ai_settings, "min_confidence", None
            ),
            options=dict(options or {}),
        )
        await self._outbox.create_batch(initial_batch)

        # --- 4. Route jobs ----------------------------------------
        try:
            jobs = await self._trigger_router.route_batch(
                batch_id=batch_id,
            )
        except Exception as exc:
            logger.exception(
                "create_batch %s: route_batch failed: %s",
                batch_id, exc,
            )
            now = time.time()
            await self._outbox.update_batch(
                batch_id,
                status="failed",
                error_message=(
                    f"route_batch failed: {exc}"
                ),
                started_at=now,
                completed_at=now,
            )
            raise

        # --- 5. Finalize batch state ------------------------------
        job_count = len(jobs)
        unique_chats = {j.chat_id for j in jobs}
        session_count = len(unique_chats)

        total_pending_turns = sum(
            (j.end_turn_index or 0) - (j.start_turn_index or 0) + 1
            for j in jobs
        )
        _est_tokens, est_cost = self._estimate_tokens_and_cost(
            job_count, total_pending_turns
        )

        now = time.time()
        if job_count == 0:
            # Nothing to do -- batch is immediately complete.
            # This is the normal case when every session has
            # already been extracted (cursor is caught up) or
            # when the operator creates a batch on an empty
            # workspace.
            return await self._outbox.update_batch(
                batch_id,
                status="completed",
                session_count=0,
                job_count=0,
                estimated_cost_nano_usd=0,
                started_at=now,
                completed_at=now,
            )

        return await self._outbox.update_batch(
            batch_id,
            status="running",
            session_count=session_count,
            job_count=job_count,
            estimated_cost_nano_usd=est_cost,
            started_at=now,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get_batch(
        self, batch_id: str
    ) -> Optional[MemoryBatch]:
        """Return a batch, self-healing RUNNING -> COMPLETED.

        When a RUNNING batch has no pending work (all jobs are
        terminal), the batch is transitioned to COMPLETED in
        this call. The returned instance reflects the
        post-heal state.

        PAUSED batches are NOT auto-completed: pause is a
        deliberate user action, and completing the batch would
        silently defeat it. The user must explicitly resume or
        cancel.

        Returns None when ``batch_id`` does not exist.
        """
        batch = await self._outbox.get_batch(batch_id)
        if batch is None:
            return None

        if batch.status == MemoryBatchStatus.RUNNING:
            agg = await self._outbox.aggregate_batch_progress(
                batch_id
            )
            if agg is not None and agg["pending_work"] == 0:
                now = time.time()
                logger.info(
                    "get_batch %s: self-healing RUNNING -> "
                    "COMPLETED (pending_work=0).",
                    batch_id,
                )
                batch = await self._outbox.update_batch(
                    batch_id,
                    status="completed",
                    completed_jobs=agg["completed"],
                    failed_jobs=agg["failed"],
                    cancelled_jobs=agg["cancelled"],
                    total_candidates=agg["total_candidates"],
                    total_memories_created=(
                        agg["total_memories_created"]
                    ),
                    actual_cost_nano_usd=(
                        agg["actual_cost_nano_usd"]
                    ),
                    completed_at=now,
                    last_progress_at=now,
                )

        return batch

    async def get_progress(
        self, batch_id: str
    ) -> Optional[Dict[str, Any]]:
        """Return the live aggregate + batch metadata.

        Combines ``MemoryOutbox.aggregate_batch_progress`` with
        the batch row's own fields into a single dict the API
        can serialize directly. Returns None for unknown
        batches.
        """
        batch = await self.get_batch(batch_id)
        if batch is None:
            return None
        agg = await self._outbox.aggregate_batch_progress(batch_id)
        if agg is None:
            # Race: batch was deleted between get_batch and
            # aggregate. Treat as not found.
            return None
        return {
            "batch": batch.to_dict(),
            "progress": agg,
        }

    async def list_batches(
        self,
        *,
        status: Optional[MemoryBatchStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[MemoryBatch]:
        """Return batches, newest first.

        Self-heals every RUNNING batch in the page -- consistent
        with ``get_batch``. Runs sequentially because each heal
        is a small UPDATE; the aggregate query is fast.
        """
        batches = await self._outbox.list_batches(
            status=status, limit=limit, offset=offset,
        )
        healed: List[MemoryBatch] = []
        for b in batches:
            if b.status == MemoryBatchStatus.RUNNING:
                healed_b = await self.get_batch(b.batch_id)
                healed.append(healed_b if healed_b else b)
            else:
                healed.append(b)
        return healed

    async def count_batches(
        self,
        *,
        status: Optional[MemoryBatchStatus] = None,
    ) -> int:
        """Return the number of batches matching ``status``."""
        return await self._outbox.count_batches(status=status)

    # ------------------------------------------------------------------
    # Pause / Resume / Cancel
    # ------------------------------------------------------------------

    async def pause_batch(self, batch_id: str) -> MemoryBatch:
        """Cancel pending / retry_pending jobs; keep running ones.

        See the module docstring for why running jobs are
        exempted. The pause is recorded as
        ``error_code='BATCH_PAUSED'`` on each cancelled job, so
        ``resume_batch`` can identify exactly which jobs to
        requeue later.

        Raises
        ------
        BatchNotFoundError
            Unknown batch.
        BatchStateError
            Batch is not RUNNING.
        """
        batch = await self._outbox.get_batch(batch_id)
        if batch is None:
            raise BatchNotFoundError(batch_id)
        if batch.status != MemoryBatchStatus.RUNNING:
            raise BatchStateError(
                batch_id,
                f"cannot pause: current status is "
                f"{batch.status.value!r}; only 'running' "
                f"can be paused.",
            )

        job_ids = await self._collect_job_ids(
            batch_id,
            statuses=("pending", "retry_pending"),
        )
        cancelled_count = 0
        for job_id in job_ids:
            try:
                await self._outbox.cancel_with_code(
                    job_id,
                    error_code="BATCH_PAUSED",
                    error_message=f"Paused batch {batch_id}",
                    allowed_statuses=(
                        "pending", "retry_pending",
                    ),
                )
                cancelled_count += 1
            except (ValueError, JobNotFoundError):
                # Job changed state between fetch and cancel.
                # This is expected when a worker claims a
                # pending job mid-loop; the claimed job is now
                # 'running' and stays alone, which is fine.
                pass

        logger.info(
            "pause_batch %s: cancelled %d/%d pending jobs.",
            batch_id, cancelled_count, len(job_ids),
        )

        now = time.time()
        return await self._outbox.update_batch(
            batch_id,
            status="paused",
            paused_at=now,
        )

    async def resume_batch(self, batch_id: str) -> MemoryBatch:
        """Requeue every job paused by ``pause_batch``.

        Only jobs whose ``error_code`` is ``BATCH_PAUSED`` are
        requeued. A job that failed for another reason (e.g.
        ``SESSION_CHANGED``) is left untouched -- the user can
        retry it individually via the jobs API, but automatic
        resume does not silently re-run known-broken jobs.

        Raises
        ------
        BatchNotFoundError
            Unknown batch.
        BatchStateError
            Batch is not PAUSED.
        """
        batch = await self._outbox.get_batch(batch_id)
        if batch is None:
            raise BatchNotFoundError(batch_id)
        if batch.status != MemoryBatchStatus.PAUSED:
            raise BatchStateError(
                batch_id,
                f"cannot resume: current status is "
                f"{batch.status.value!r}; only 'paused' "
                f"can be resumed.",
            )

        cancelled_ids = await self._collect_job_ids(
            batch_id, statuses=("cancelled",),
        )
        requeued_count = 0
        for job_id in cancelled_ids:
            # Filter by error_code so we only requeue pause-
            # induced cancellations. Requires reading the job
            # to check -- small N, acceptable.
            job = await self._outbox.get_by_id(job_id)
            if job is None or job.error_code != "BATCH_PAUSED":
                continue
            try:
                await self._outbox.requeue(job_id)
                requeued_count += 1
            except (ValueError, JobNotFoundError):
                pass

        logger.info(
            "resume_batch %s: requeued %d paused jobs.",
            batch_id, requeued_count,
        )

        return await self._outbox.update_batch(
            batch_id,
            status="running",
            paused_at=None,
        )

    async def cancel_batch(self, batch_id: str) -> MemoryBatch:
        """Cancel every non-terminal job in the batch.

        No exemption: pending, retry_pending, AND running jobs
        are all cancelled. The running job's worker will see
        the transition on its next heartbeat and skip the
        ``complete()`` call (``JobNotFoundError`` is already
        caught by the worker and logged as "job disappeared").

        Raises
        ------
        BatchNotFoundError
            Unknown batch.
        BatchStateError
            Batch is already terminal.
        """
        batch = await self._outbox.get_batch(batch_id)
        if batch is None:
            raise BatchNotFoundError(batch_id)
        if batch.status.is_terminal:
            raise BatchStateError(
                batch_id,
                f"cannot cancel: batch is already "
                f"{batch.status.value!r}.",
            )

        job_ids = await self._collect_job_ids(
            batch_id,
            statuses=("pending", "retry_pending", "running"),
        )
        cancelled_count = 0
        for job_id in job_ids:
            try:
                await self._outbox.cancel_with_code(
                    job_id,
                    error_code="BATCH_CANCELLED",
                    error_message=f"Cancelled batch {batch_id}",
                    allowed_statuses=(
                        "pending", "retry_pending", "running",
                    ),
                )
                cancelled_count += 1
            except (ValueError, JobNotFoundError):
                pass

        logger.info(
            "cancel_batch %s: cancelled %d/%d jobs.",
            batch_id, cancelled_count, len(job_ids),
        )

        now = time.time()
        return await self._outbox.update_batch(
            batch_id,
            status="cancelled",
            cancelled_at=now,
        )

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    async def estimate_cost(self) -> Dict[str, Any]:
        """Estimate the cost of a fresh batch run now.

        Scans all sessions with pending turns (same scan
        ``route_batch`` performs) and returns a dict with the
        session count, estimated job count, estimated total
        tokens, and estimated cost in nano-USD.

        The estimate is a HEURISTIC upper bound. See the
        ``_TOKENS_PER_TURN`` / ``_PROMPT_OVERHEAD_TOKENS`` /
        ``_COST_NANO_USD_PER_MILLION_TOKENS`` constants for the
        derivation.

        Returns a well-formed dict with zero counts when there
        is nothing to extract (empty workspace or all caught up).
        """
        candidates = await self._scanner.scan_all_sessions(
            only_unprocessed=True
        )
        if not candidates:
            return {
                "session_count": 0,
                "estimated_jobs": 0,
                "total_pending_turns": 0,
                "estimated_tokens": 0,
                "estimated_cost_nano_usd": 0,
            }

        total_pending_turns = sum(
            c.pending_turns for c in candidates
        )
        est_tokens, est_cost = self._estimate_tokens_and_cost(
            len(candidates), total_pending_turns,
        )
        return {
            "session_count": len(candidates),
            "estimated_jobs": len(candidates),
            "total_pending_turns": total_pending_turns,
            "estimated_tokens": est_tokens,
            "estimated_cost_nano_usd": est_cost,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _collect_job_ids(
        self,
        batch_id: str,
        *,
        statuses: tuple,
    ) -> List[str]:
        """Fetch job ids in the given statuses, paginated.

        Collect-then-act is deliberate: cancelling jobs while
        iterating a paginated query would shift the result set
        and skip rows. Fetching every id first guarantees
        complete coverage regardless of how many jobs the batch
        spans.
        """
        job_ids: List[str] = []
        for status_filter in statuses:
            offset = 0
            while True:
                chunk = await self._outbox.list_jobs(
                    status=status_filter,
                    batch_id=batch_id,
                    limit=200,
                    offset=offset,
                )
                if not chunk:
                    break
                job_ids.extend(j.job_id for j in chunk)
                offset += len(chunk)
                if len(chunk) < 200:
                    break
        return job_ids

    @staticmethod
    def _estimate_tokens_and_cost(
        job_count: int, total_pending_turns: int
    ) -> tuple[int, int]:
        """Return ``(estimated_tokens, estimated_cost_nano_usd)``.

        See the module-level heuristic constants for the
        derivation. The two-value return keeps both
        ``create_batch`` and ``estimate_cost`` in sync without
        duplicating the formula.
        """
        tokens = (
            job_count
            * (_PROMPT_OVERHEAD_TOKENS + _MAX_COMPLETION_TOKENS)
            + total_pending_turns * _TOKENS_PER_TURN
        )
        cost = (
            tokens * _COST_NANO_USD_PER_MILLION_TOKENS
        ) // 1_000_000
        return tokens, cost


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "BatchNotFoundError",
    "BatchServiceUnavailableError",
    "BatchStateError",
    "MemoryBatchService",
]
