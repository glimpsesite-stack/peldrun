# backend/omweb/memory/jobs/embedding_worker.py
"""
PELDRUN Memory Platform - Phase 4: Embedding Worker.

Long-running worker that drains ``memory_embedding_jobs`` by
claiming, embedding, and persisting vector representations of
memories. Companion to ``MemoryExtractionWorker`` -- same lifecycle
shape (start / stop / run loop / stale recovery), simpler inner
loop.

What the worker does, per job
-----------------------------

    1. Claim the oldest pending job (``EmbeddingOutbox.claim_next``).
    2. Skip immediately when a fresh embedding already exists for
       the same ``(memory_id, model_id, model_version,
       content_version)``. This happens on requeue-after-partial-
       success (see ``EmbeddingOutbox.enqueue``'s docstring) --
       re-embedding would burn API tokens for a vector that is
       already correct.
    3. Read the memory's ``content`` from the ``memories`` table.
       A missing row means the memory was hard-deleted between
       enqueue and processing -- fail with ``MEMORY_NOT_FOUND``,
       do not retry (retrying will fail the same way).
    4. Find the resolver's snapshot entry for the job's
       ``(model_id, model_version)``. A missing entry means the
       model was removed from configuration since enqueue -- fail
       with ``MODEL_NOT_CONFIGURED``.
    5. Determine the model's vector dimension:
         * from ``ResolvedEmbeddingModel.known_dimensions`` when
           already probed and persisted, OR
         * by calling ``OpenAICompatEmbeddingProvider.probe_dimensions``
           ONCE per worker lifetime per model (cached in
           ``_dimension_cache``).
    6. Ensure the ``memory_vectors`` vec0 table is at the correct
       dimension (drops + recreates if changed -- see "Dimension
       churn" below).
    7. Embed the content via the provider.
    8. Persist the vector via ``SqliteVecRepository.upsert_embeddings``.
    9. Mark the job completed, with the provider's reported cost.

Error classification
--------------------

Transient (retry with exponential backoff, up to ``max_attempts``):
    * ``EmbeddingTransportError``       -- network / 5xx / 429
    * Generic ``EmbeddingError`` from ``probe_dimensions``
      (cannot distinguish transient from permanent at that
      layer -- retry is the safe default)

Permanent (fail immediately, no retry):
    * ``EmbeddingUnavailableError``     -- auth, model not offered
    * ``EmbeddingDimensionMismatchError``
    * ``EmbeddingBatchSizeError``
    * ``EmbeddingModelNotConfiguredError`` (resolver)
    * Empty memory content
    * Memory hard-deleted between enqueue and processing

Unknown exception:
    * Retry with backoff up to ``max_attempts``, then fail.

Why no lease / heartbeat
------------------------

Unlike ``MemoryExtractionWorker`` (whose jobs can run for minutes
of LLM streaming), an embedding job is a single round-trip that
typically completes in under a second. The fixed
``recover_stale_running`` timeout (default 300s, configurable) is
sufficient to detect a crashed worker without the overhead of a
heartbeat loop and per-connection lease tables.

Dimension churn -- documented behavior
--------------------------------------

``memory_vectors`` is a vec0 table whose dimension is fixed at
creation. When two different embedding models are configured AND
jobs for both are queued, the worker will:

    1. Process model A's jobs first (FIFO). vec0 dim = A.dim.
    2. Hit model B's first job. Call ``ensure_vec0_table(B.dim)``.
       That DROPS AND RECREATES vec0 at B.dim.
    3. Model A's vectors are still in ``memory_embeddings``
       metadata, but the vec0 index no longer contains them.
    4. Model B's jobs proceed and populate the new vec0.

The user's remedy is ``POST /api/memory/reindex`` (Task 4.10)
after making model A the active model. In Phase 4 the typical
deployment has ONE embedding model, so this rarely fires. When it
DOES fire, the worker logs a warning naming both models so the
operator sees exactly what happened.

Design invariants
-----------------

1. **One provider per job.** The provider is constructed with the
   job's resolved credentials and closed in a ``finally`` block.
   This means a settings change (new API key, new base_url) is
   picked up on the NEXT job without restarting the worker.

2. **No cross-job state on disk.** The worker holds in-memory
   caches (``_dimension_cache``) only. Every persistent effect is
   mediated by the outbox + repositories. A worker crash leaves
   a ``running`` job that ``recover_stale_running`` flips back to
   ``retry_pending`` on the next startup.

3. **Content is re-read on every attempt.** See ``EmbeddingJob``
   docstring: the job does NOT carry content, so a job that is
   retried after a content edit embeds the NEW content. A vector
   for stale text is worse than a wasted API call.

4. **Errors are classified by TYPE, not by message.** A provider
   that happens to include "network" in a permanent 401 message
   must NOT be retried. The classification lives in
   ``_handle_job_error`` -- the single point where any exception
   becomes either a retry or a fail.

5. **Shutdown is prompt.** The run loop's poll wait is
   interruptible via ``_shutdown_event``. ``stop()`` sets the
   event and awaits the tasks with a bounded timeout. The
   currently-processing job's in-flight API call is cancelled
   (matching ``MemoryExtractionWorker`` behavior); its lease
   expires naturally and ``recover_stale_running`` flips it back
   to pending on the next startup.

See also
--------

    * ``peldrun/memory/embeddings/contracts.py``
                                -- ``EmbeddingProvider`` protocol
    * ``peldrun/memory/embeddings/provider.py``
                                -- ``OpenAICompatEmbeddingProvider``
    * ``peldrun/memory/embeddings/model_resolver.py``
                                -- ``EmbeddingModelResolver``
    * ``omweb/memory/storage/vector_repository.py``
                                -- vector persistence
    * ``omweb/memory/jobs/outbox.py``
                                -- ``EmbeddingOutbox`` queue
    * ``omweb/memory/manager/snapshot_builder.py``
                                -- resolver construction
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from typing import TYPE_CHECKING, Callable, Dict, Optional, Tuple

from peldrun.memory.domain.embedding import MemoryEmbedding
from peldrun.memory.embeddings.contracts import (
    EmbeddingBatchSizeError,
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingTransportError,
    EmbeddingUnavailableError,
)
from peldrun.memory.embeddings.model_resolver import (
    EmbeddingModelNotConfiguredError,
    EmbeddingModelResolver,
    ResolvedEmbeddingModel,
)
from peldrun.memory.embeddings.provider import (
    OpenAICompatEmbeddingProvider,
)

from omweb.memory.jobs.models import EmbeddingJob
from omweb.memory.jobs.outbox import (
    EmbeddingOutbox,
    JobNotFoundError,
)
from omweb.memory.storage.repository import SqliteMemoryRepository
from omweb.memory.storage.vector_repository import (
    SqliteVecRepository,
    Vec0UnavailableError,
)


if TYPE_CHECKING:
    pass


logger = logging.getLogger(
    "omweb.memory.jobs.embedding_worker"
)


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------


#: Factory signature for constructing an ``EmbeddingProvider`` given a
#: resolved model and its dimensions. The default factory uses
#: ``OpenAICompatEmbeddingProvider``. Tests inject a fake factory that
#: returns deterministic vectors without making API calls.
EmbeddingProviderFactory = Callable[
    [ResolvedEmbeddingModel, int], EmbeddingProvider
]


# ---------------------------------------------------------------------------
# Worker id helper
# ---------------------------------------------------------------------------


def _default_worker_id() -> str:
    return (
        f"embw_{socket.gethostname()}"
        f"_{os.getpid()}"
        f"_{uuid.uuid4().hex[:8]}"
    )


# ---------------------------------------------------------------------------
# EmbeddingWorker
# ---------------------------------------------------------------------------


class EmbeddingWorker:
    """Drains ``memory_embedding_jobs`` with FIFO ordering.

    See the module docstring for the full design. In short: it
    claims one job at a time, embeds the memory's current content,
    persists the vector, and marks the job complete (or classifies
    the failure and either retries or fails).

    Lifecycle::

        worker = EmbeddingWorker(
            outbox=outbox,
            resolver=resolver,
            vector_repository=vec_repo,
            memory_repository=mem_repo,
        )
        await worker.start()
        ...
        await worker.stop()
    """

    _DEFAULT_POLL_INTERVAL_SECONDS: float = 1.0
    _BASE_BACKOFF_SECONDS: int = 60
    _MAX_BACKOFF_SECONDS: int = 15 * 60

    def __init__(
        self,
        *,
        outbox: EmbeddingOutbox,
        resolver: EmbeddingModelResolver,
        vector_repository: SqliteVecRepository,
        memory_repository: SqliteMemoryRepository,
        embedding_provider_factory: Optional[
            EmbeddingProviderFactory
        ] = None,
        worker_id: Optional[str] = None,
        poll_interval_seconds: Optional[float] = None,
    ) -> None:
        """Initialize the worker.

        Parameters
        ----------
        outbox
            Durable queue for embedding jobs.
        resolver
            Typed model registry built by
            ``build_embedding_model_snapshot``. The worker uses
            it to look up credentials by
            ``(model_id, model_version)`` and to reject jobs
            whose model is no longer configured.
        vector_repository
            Persistence for ``memory_embeddings`` + the vec0
            index. Owns the sqlite-vec extension lifecycle on its
            own connection.
        memory_repository
            Used ONLY to read ``memories.content`` for a
            ``memory_id``. The worker does not write to memories.
        embedding_provider_factory
            Override for tests. Default constructs an
            ``OpenAICompatEmbeddingProvider`` with the resolved
            model's credentials. Tests supply a fake that
            returns deterministic vectors without API calls.
        worker_id
            Unique id for logs. Auto-generated when ``None``.
        poll_interval_seconds
            Sleep between claims when the queue is empty.
        """
        self._outbox = outbox
        self._resolver = resolver
        self._vec_repo = vector_repository
        self._mem_repo = memory_repository
        self._provider_factory: EmbeddingProviderFactory = (
            embedding_provider_factory
            if embedding_provider_factory is not None
            else self._default_provider_factory
        )

        self._worker_id = worker_id or _default_worker_id()
        self._poll_interval_seconds = (
            float(poll_interval_seconds)
            if poll_interval_seconds is not None
            else self._DEFAULT_POLL_INTERVAL_SECONDS
        )
        if self._poll_interval_seconds <= 0:
            raise ValueError(
                "poll_interval_seconds must be positive"
            )

        # worker-lifetime cache: avoids re-probing dimensions for
        # every job in the same model namespace.
        self._dimension_cache: Dict[Tuple[str, str], int] = {}

        self._run_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._current_job_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Recover stale ``running`` jobs and start the run loop.

        Idempotent -- calling ``start()`` while the worker is
        already running logs a debug line and returns.
        """
        if self._run_task is not None and not self._run_task.done():
            logger.debug(
                "EmbeddingWorker %s already running; start() is a no-op.",
                self._worker_id,
            )
            return

        try:
            recovered = await self._outbox.recover_stale_running()
            if recovered > 0:
                logger.info(
                    "EmbeddingWorker %s recovered %d stale job(s) "
                    "on startup.",
                    self._worker_id,
                    recovered,
                )
        except Exception as exc:
            logger.warning(
                "EmbeddingWorker %s failed to recover stale jobs "
                "(continuing anyway): %s",
                self._worker_id,
                exc,
            )

        self._shutdown_event.clear()
        self._run_task = asyncio.create_task(
            self._run_loop(),
            name=f"EmbeddingWorker-Run-{self._worker_id}",
        )
        logger.info(
            "EmbeddingWorker started (worker_id=%s, "
            "poll_interval=%.1fs, models_in_snapshot=%d).",
            self._worker_id,
            self._poll_interval_seconds,
            len(self._resolver),
        )

    async def stop(self, *, timeout_seconds: int = 30) -> None:
        """Signal shutdown and wait for the run loop to stop."""
        if self._run_task is None:
            return

        self._shutdown_event.set()

        if not self._run_task.done():
            done, pending = await asyncio.wait(
                [self._run_task], timeout=float(timeout_seconds)
            )
            if pending:
                logger.warning(
                    "EmbeddingWorker %s did not stop within %ds; "
                    "cancelling the run loop. The in-flight job's "
                    "row will be recovered by "
                    "``recover_stale_running`` on next startup.",
                    self._worker_id,
                    timeout_seconds,
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        self._run_task = None
        self._current_job_id = None
        logger.info(
            "EmbeddingWorker stopped (worker_id=%s).",
            self._worker_id,
        )

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Claim → process → transition, until shutdown."""
        while not self._shutdown_event.is_set():
            try:
                job = await self._outbox.claim_next(self._worker_id)
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
                try:
                    await self._process_job(job)
                except JobNotFoundError:
                    logger.warning(
                        "Embedding job %s disappeared between "
                        "claim and complete (external cancel or "
                        "stale recovery). Ignoring.",
                        job.job_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._handle_job_error(job, exc)
                finally:
                    self._current_job_id = None

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "EmbeddingWorker %s run loop error; will "
                    "retry in 1s: %s",
                    self._worker_id,
                    exc,
                )
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------------
    # Job processing
    # ------------------------------------------------------------------

    async def _process_job(self, job: EmbeddingJob) -> None:
        """Embed one memory and persist its vector.

        This method either succeeds (job is completed) or raises
        an exception. The caller (``_run_loop``) is responsible
        for transitioning the job based on the exception type --
        see ``_handle_job_error``.

        Raising ``JobNotFoundError`` means the row vanished
        mid-flight; the caller logs and moves on.
        """
        # ---- 1. Find the resolver entry for this job's model ----
        resolved = self._resolve_model(job)
        if resolved is None:
            # Model not in snapshot -- config was changed since
            # enqueue. Permanent failure, no retry.
            await self._outbox.fail(
                job.job_id,
                "MODEL_NOT_CONFIGURED",
                f"Embedding model {job.model_id!r} "
                f"version {job.model_version!r} is not in the "
                f"resolver snapshot. Either the model was "
                f"removed from Settings -> Models after this job "
                f"was enqueued, or metadata discovery has not "
                f"run yet. Re-add the model and re-enqueue.",
            )
            return

        # ---- 2. Skip if a fresh embedding already exists -------
        existing = await self._vec_repo.get_embedding(
            job.memory_id,
            job.model_id,
            job.model_version,
        )
        if (
            existing is not None
            and existing.content_version == job.content_version
        ):
            logger.info(
                "Embedding job %s: vector already exists for "
                "(memory=%s, model=%s/%s, v%d); completing "
                "without an API call.",
                job.job_id,
                job.memory_id,
                job.model_id,
                job.model_version,
                job.content_version,
            )
            await self._outbox.complete(
                job.job_id, cost_nano_usd=0
            )
            return

        # ---- 3. Read the memory's content ----------------------
        content = await asyncio.to_thread(
            self._read_memory_content, job.memory_id
        )
        if content is None:
            await self._outbox.fail(
                job.job_id,
                "MEMORY_NOT_FOUND",
                f"Memory {job.memory_id!r} no longer exists "
                f"in the memories table. It may have been "
                f"hard-deleted after this job was enqueued.",
            )
            return
        stripped = content.strip()
        if not stripped:
            await self._outbox.fail(
                job.job_id,
                "MEMORY_EMPTY",
                f"Memory {job.memory_id!r} has empty content; "
                f"nothing to embed.",
            )
            return

        # ---- 4. Determine dimensions ---------------------------
        if resolved.base_url is None:
            await self._outbox.fail(
                job.job_id,
                "MODEL_UNCONFIGURED",
                f"Model {job.model_id!r} (provider "
                f"{resolved.provider_id!r}) has no base_url "
                f"configured in the vault. Add the provider "
                f"endpoint in Settings -> Models.",
            )
            return

        try:
            dims = await self._resolve_dimensions(resolved)
        except EmbeddingUnavailableError as exc:
            # Permanent -- auth failure, model not offered by
            # the provider. Retry would fail identically.
            await self._outbox.fail(
                job.job_id,
                "PROBE_UNAVAILABLE",
                f"Could not probe dimensions for "
                f"{job.model_id!r}: {exc}",
            )
            return
        except EmbeddingError as exc:
            # Generic probe error -- could be transient.
            # Propagate to _handle_job_error which will retry.
            raise EmbeddingTransportError(
                f"Dimension probe failed for {job.model_id!r}: "
                f"{exc}"
            ) from exc

        # ---- 5. Ensure vec0 table matches the dimension --------
        previous_dim = self._vec_repo.vec0_dimension
        if previous_dim != 0 and previous_dim != dims:
            logger.warning(
                "EmbeddingWorker %s: vec0 dimension change "
                "(%d -> %d) triggered by job %s "
                "(model=%s/%s). Vectors for the previous "
                "model remain in memory_embeddings but are "
                "NO LONGER in the vec0 index. Re-run "
                "POST /api/memory/reindex for the previous "
                "model to restore its index.",
                self._worker_id,
                previous_dim,
                dims,
                job.job_id,
                job.model_id,
                job.model_version,
            )
        await self._vec_repo.ensure_vec0_table(dims)

        # ---- 6. Build the provider and embed -------------------
        provider = self._provider_factory(resolved, dims)
        try:
            try:
                result = await provider.embed_batch(
                    [stripped], model_id=resolved.model_id
                )
            except (
                EmbeddingUnavailableError,
                EmbeddingDimensionMismatchError,
                EmbeddingBatchSizeError,
            ):
                # Permanent -- propagate to _handle_job_error
                # which will fail (not retry).
                raise
            except EmbeddingTransportError:
                # Transient -- propagate to _handle_job_error
                # which will retry with backoff.
                raise
            except EmbeddingError:
                # Generic embedding error -- treat as transient
                # by default. The provider's error hierarchy is
                # deliberately narrow (see
                # ``EmbeddingError``'s subclasses); an unexpected
                # subclass is retried to be safe.
                raise EmbeddingTransportError(
                    f"Unexpected embedding error: "
                    f"retrying once."
                )
        finally:
            try:
                await provider.close()
            except Exception as exc:
                logger.debug(
                    "EmbeddingWorker %s: provider.close() raised "
                    "(non-fatal): %s",
                    self._worker_id,
                    exc,
                )

        # ---- 7. Build and persist the MemoryEmbedding ---------
        if not result.vectors:
            raise EmbeddingTransportError(
                f"embed_batch returned no vectors for "
                f"memory {job.memory_id!r}"
            )

        emb = MemoryEmbedding.from_result_and_entry(
            memory_id=job.memory_id,
            model_id=resolved.model_id,
            model_version=resolved.model_version,
            vector=result.vectors[0],
            content_version=job.content_version,
        )
        await self._vec_repo.upsert_embeddings(
            [emb], dimension=dims
        )

        # ---- 8. Complete --------------------------------------
        await self._outbox.complete(
            job.job_id, cost_nano_usd=result.cost_nano_usd
        )

        logger.info(
            "EmbeddingWorker %s: completed job %s "
            "(memory=%s, model=%s/%s, dims=%d, cost=%d nano-USD).",
            self._worker_id,
            job.job_id,
            job.memory_id,
            job.model_id,
            job.model_version,
            dims,
            result.cost_nano_usd,
        )

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    async def _handle_job_error(
        self, job: EmbeddingJob, exc: Exception
    ) -> None:
        """Classify ``exc`` as transient (retry) or permanent (fail).

        See the module docstring's "Error classification" section
        for the full policy. The classification is by TYPE, not by
        message: a permanent 401 whose message happens to include
        "network" must NOT be retried.
        """
        logger.warning(
            "Embedding job %s failed on attempt %d/%d: %s: %s",
            job.job_id,
            job.attempts + 1,
            job.max_attempts,
            type(exc).__name__,
            exc,
        )

        # --- Permanent failures: fail immediately ---------------
        if isinstance(
            exc,
            (
                EmbeddingUnavailableError,
                EmbeddingDimensionMismatchError,
                EmbeddingBatchSizeError,
            ),
        ):
            try:
                await self._outbox.fail(
                    job.job_id,
                    "PERMANENT_EMBEDDING_ERROR",
                    f"{type(exc).__name__}: {exc}",
                )
            except JobNotFoundError:
                logger.warning(
                    "Embedding job %s disappeared before fail(); "
                    "ignoring.",
                    job.job_id,
                )
            return

        # --- Transient or unknown: retry until exhausted --------
        if job.attempts + 1 >= job.max_attempts:
            try:
                await self._outbox.fail(
                    job.job_id,
                    "RETRY_EXHAUSTED",
                    f"Failed after {job.attempts + 1} "
                    f"attempt(s): {type(exc).__name__}: {exc}",
                )
            except JobNotFoundError:
                logger.warning(
                    "Embedding job %s disappeared before fail(); "
                    "ignoring.",
                    job.job_id,
                )
            return

        next_retry_at = (
            time.time()
            + self._compute_backoff_seconds(job.attempts)
        )
        try:
            await self._outbox.retry(
                job.job_id,
                next_retry_at,
                error_code=type(exc).__name__.upper(),
                error_message=str(exc),
            )
        except JobNotFoundError:
            logger.warning(
                "Embedding job %s disappeared before retry(); "
                "ignoring.",
                job.job_id,
            )

    def _compute_backoff_seconds(self, attempts: int) -> int:
        """Exponential backoff, capped at ``_MAX_BACKOFF_SECONDS``."""
        raw = self._BASE_BACKOFF_SECONDS * (2 ** max(0, attempts))
        return min(raw, self._MAX_BACKOFF_SECONDS)

    # ------------------------------------------------------------------
    # Model resolution + dimension probe
    # ------------------------------------------------------------------

    def _resolve_model(
        self, job: EmbeddingJob
    ) -> Optional[ResolvedEmbeddingModel]:
        """Find the resolver entry for the job's ``(model_id,
        model_version)``.

        The resolver indexes by ``model_key`` (a composite
        ``provider::model`` string). The job only carries the
        bare ``(model_id, model_version)`` pair, so we scan the
        small list of resolved models looking for a match.

        A scan (rather than a reverse index) is intentional: the
        resolver is read-only and its content changes only via
        reconstruction, so the overhead of a scan is negligible
        (1-10 entries typically) and the code is simpler. A
        reverse index would require the resolver to expose a
        method that only this worker uses.
        """
        for candidate in self._resolver.list_models():
            if (
                candidate.model_id == job.model_id
                and candidate.model_version == job.model_version
            ):
                return candidate
        return None

    async def _resolve_dimensions(
        self, resolved: ResolvedEmbeddingModel
    ) -> int:
        """Return the model's vector dimension.

        Priority:
          1. ``resolved.known_dimensions`` (persisted by a
             prior probe),
          2. ``self._dimension_cache`` (this worker's lifetime),
          3. A live ``probe_dimensions`` call (once per worker
             lifetime per model).

        The result of a live probe is cached in
        ``_dimension_cache`` so subsequent jobs skip the API
        call. It is NOT persisted to disk -- persistence is the
        metadata cache's responsibility (a separate task), and
        the worker should not write files it does not own.
        """
        if resolved.known_dimensions is not None:
            return resolved.known_dimensions

        cache_key = (resolved.model_id, resolved.model_version)
        cached = self._dimension_cache.get(cache_key)
        if cached is not None:
            return cached

        logger.info(
            "EmbeddingWorker %s: probing dimensions for "
            "model %r at %r (first use in this worker).",
            self._worker_id,
            resolved.model_id,
            resolved.base_url,
        )
        probed = await OpenAICompatEmbeddingProvider.probe_dimensions(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            model_id=resolved.model_id,
        )
        if probed <= 0:
            raise EmbeddingError(
                f"probe_dimensions returned an invalid dimension "
                f"({probed}) for model {resolved.model_id!r}"
            )
        self._dimension_cache[cache_key] = probed
        logger.info(
            "EmbeddingWorker %s: model %r has %d dimensions "
            "(cached for the lifetime of this worker).",
            self._worker_id,
            resolved.model_id,
            probed,
        )
        return probed

    # ------------------------------------------------------------------
    # Memory content read
    # ------------------------------------------------------------------

    def _read_memory_content(
        self, memory_id: str
    ) -> Optional[str]:
        """Read ``memories.content`` for ``memory_id``.

        Uses the memory repository's connection -- same WAL
        configuration, same busy_timeout, no duplicate
        connection setup. The read is a single indexed SELECT
        (``memory_id`` is the primary lookup key), so this
        method is called via ``asyncio.to_thread`` in
        ``_process_job``.

        Returns
        -------
        Optional[str]
            The content, or ``None`` when the memory row does
            not exist. Empty strings are returned as-is; the
            caller (``_process_job``) rejects empty content
            before embedding.
        """
        # Private-method access: ``_get_conn`` is the
        # documented-in-code way to obtain the repository's
        # underlying connection. Both classes live in the same
        # ``omweb`` package tree; the alternative -- a new
        # public method on ``SqliteMemoryRepository`` -- would
        # touch a file that Phase 4 is not supposed to modify.
        conn = self._mem_repo._get_conn()
        row = conn.execute(
            "SELECT content FROM memories WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        return row["content"]

    # ------------------------------------------------------------------
    # Provider factory
    # ------------------------------------------------------------------

    @staticmethod
    def _default_provider_factory(
        resolved: ResolvedEmbeddingModel,
        dimensions: int,
    ) -> EmbeddingProvider:
        """Build a real ``OpenAICompatEmbeddingProvider``.

        Overridable in tests via ``embedding_provider_factory``.
        The construction deliberately happens HERE (per job)
        rather than once in ``__init__`` so that a settings
        change (rotated API key, new base_url) is picked up on
        the next job.
        """
        return OpenAICompatEmbeddingProvider(
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            model_id=resolved.model_id,
            model_version=resolved.model_version,
            dimensions=dimensions,
            extra_headers=resolved.extra_headers or None,
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


__all__ = [
    "EmbeddingProviderFactory",
    "EmbeddingWorker",
]
