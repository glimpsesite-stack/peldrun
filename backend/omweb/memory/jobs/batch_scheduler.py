# backend/omweb/memory/jobs/batch_scheduler.py
"""
PELDRUN Memory Platform - Phase 4: Batch Scheduler.

Periodically creates a ``MemoryBatch`` via
``MemoryBatchService.create_batch`` when the operator has set
``MemoryAISettings.batch_auto_schedule_hours`` to a positive
integer. Runs as a single ``asyncio.Task`` owned by the FastAPI
lifespan (started in ``main.py`` alongside the extraction worker,
stopped before it).

Behavior summary
----------------

The scheduler answers one question on every "tick":

    "Should I create a batch NOW?"

The answer is YES only when ALL of the following hold:

  1. ``memory_service.batch_service`` is not ``None`` (the
     extraction pipeline was successfully initialized),
  2. ``settings.ai.batch_enabled`` is ``True`` (the operator has
     not disabled the batch subsystem),
  3. ``settings.ai.batch_auto_schedule_hours`` is a positive int
     (the operator opted into auto-scheduling),
  4. NO existing batch is in ``RUNNING`` or ``PAUSED`` state
     (auto-scheduling must not race a manual batch or resume a
     deliberately paused one).

When all four conditions pass, the scheduler calls
``create_batch(triggered_by="auto_scheduler")`` and logs the
result. Every other case is a silent no-op.

Timing model -- "every N hours" means "wait N hours between batches"
--------------------------------------------------------------------

The loop is::

    while not shutting_down:
        hours = read_schedule_hours()
        if hours is None:
            wait 60s   # settings disabled or unconfigured
            continue

        wait hours*3600s   # in 60s slices, re-reading settings
        if settings_changed_during_wait:
            restart loop with new settings
        if shutdown_requested:
            break

        try_create_batch()

The "wait-then-batch" order is deliberate: a batch created on
boot would surprise the operator who just restarted the server
without intending to trigger extraction. The manual "Run batch
now" button exists for immediate runs.

Why 60-second slices instead of one long `wait(hours * 3600)`?
Because settings can change mid-wait: an operator switching
from ``24`` to ``1`` should see the change take effect within
60 seconds, not after the current 24-hour cycle finishes. See
``_wait_in_slices`` for the implementation.

Failure isolation
-----------------

Every layer is wrapped in its own try/except:

  * ``_read_schedule_hours`` -- a broken settings file logs
    DEBUG and returns ``None`` (scheduler idles),
  * ``_try_create_batch`` -- any exception from ``create_batch``
    logs WARNING and returns (next tick retries),
  * ``_run_loop`` -- a catastrophic exception logs and applies
    a 5-second backoff before the next iteration (avoids tight
    crash loops).

The scheduler NEVER propagates an exception to the calling
lifespan hook. A broken scheduler degrades to "no auto-batches",
which is the same observable behavior as
``batch_auto_schedule_hours=None``.

Concurrency
-----------

Only ONE ``_run_task`` exists per instance. ``start()`` is
idempotent (second call is a no-op); ``stop()`` is idempotent
and timeout-bounded. The task holds no locks -- coordination
with other schedulers (none today) is via the DB state
(``RUNNING`` / ``PAUSED`` batch existence check).

See also
--------

    * ``omweb.memory.batch.service.MemoryBatchService``
        -- the create/list API this scheduler drives.
    * ``omweb.memory.manager.memory_service``
        -- owns ``batch_service`` and ``settings``.
    * ``omweb.main.lifespan``
        -- starts/stops this scheduler alongside the extraction
           worker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from omweb.memory.batch.models import MemoryBatchStatus


if TYPE_CHECKING:
    from omweb.memory.manager.memory_service import MemoryService


logger = logging.getLogger("omweb.memory.jobs.batch_scheduler")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_DEFAULT_POLL_INTERVAL_SECONDS: float = 60.0
"""Settings re-check interval while idle, AND slice size while
waiting for a scheduled batch.

60 seconds is a balance: frequent enough to detect settings
changes promptly (operator changes ``None -> 24`` and sees the
first batch within one cycle plus a small delay), infrequent
enough to avoid hammering the settings file.

The same value doubles as the slice size during a scheduled
wait, so switching ``24 -> 1`` mid-cycle takes effect within
60s rather than after the full 24 hours.
"""


_BATCH_MAX_SESSIONS: int = 100
"""Upper bound on sessions per auto-created batch.

Recorded in the batch row's ``options`` dict so future versions
of ``route_batch`` can enforce it. Current implementation does
NOT enforce (the option is stored verbatim, no downstream
consumer reads it yet), but recording the intent now means a
future enforcement change does not require re-reading this
scheduler.
"""


# ---------------------------------------------------------------------------
# BatchScheduler
# ---------------------------------------------------------------------------


class BatchScheduler:
    """Background scheduler for periodic auto-batches.

    See the module docstring for the full timing model, failure
    isolation contract, and concurrency guarantees.

    Lifecycle::

        scheduler = BatchScheduler(memory_service)
        await scheduler.start()
        ...
        await scheduler.stop(timeout_seconds=10)
    """

    _DEFAULT_POLL_INTERVAL_SECONDS: float = _DEFAULT_POLL_INTERVAL_SECONDS
    _BATCH_MAX_SESSIONS: int = _BATCH_MAX_SESSIONS

    def __init__(self, memory_service: "MemoryService") -> None:
        """Construct a stopped scheduler.

        Parameters
        ----------
        memory_service
            The process-wide ``MemoryService``. Used to read
            ``settings.ai`` (for the schedule config) and
            ``batch_service`` (for the create/list API). The
            reference is retained, NOT duplicated -- a settings
            reload via ``PATCH /api/memory/settings`` is
            observed on the next read, no restart needed.
        """
        self._memory_service: "MemoryService" = memory_service
        self._shutdown_event: asyncio.Event = asyncio.Event()
        self._run_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the scheduler's run loop.

        Idempotent: calling on an already-running instance logs
        at DEBUG and returns. Safe to call unconditionally from
        the FastAPI lifespan.
        """
        if self._run_task is not None and not self._run_task.done():
            logger.debug(
                "BatchScheduler already running; start() is a no-op."
            )
            return
        self._shutdown_event.clear()
        self._run_task = asyncio.create_task(
            self._run_loop(),
            name="BatchScheduler-RunLoop",
        )
        logger.info(
            "BatchScheduler started (poll_interval=%.1fs).",
            self._DEFAULT_POLL_INTERVAL_SECONDS,
        )

    async def stop(self, *, timeout_seconds: int = 30) -> None:
        """Signal shutdown and wait for the run loop to stop.

        Idempotent. If the loop does not stop within
        ``timeout_seconds`` (pathological -- only possible if a
        ``create_batch`` call hangs against a wedged DB), the
        task is cancelled. The cancel is safe: ``_try_create_batch``
        does not hold any partial state that would leak on
        cancellation.

        Never raises. A broken shutdown path must not prevent
        the calling lifespan hook from continuing its own
        cleanup sequence.
        """
        if self._run_task is None:
            return

        self._shutdown_event.set()

        if not self._run_task.done():
            done, pending = await asyncio.wait(
                [self._run_task], timeout=float(timeout_seconds)
            )
            if pending:
                logger.warning(
                    "BatchScheduler did not stop within %ds; "
                    "cancelling the run loop.",
                    timeout_seconds,
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(
                    *pending, return_exceptions=True
                )

        self._run_task = None
        logger.info("BatchScheduler stopped.")

    @property
    def is_running(self) -> bool:
        """True when the run loop task is active."""
        return (
            self._run_task is not None
            and not self._run_task.done()
        )

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main loop. See the module docstring for the timing model."""
        while not self._shutdown_event.is_set():
            try:
                hours = self._read_schedule_hours()
                if hours is None:
                    # Disabled or unconfigured -- poll again soon.
                    if await self._wait(self._DEFAULT_POLL_INTERVAL_SECONDS):
                        break
                    continue

                # Wait `hours` worth of seconds in 60s slices so
                # settings changes (or shutdown) are detected
                # within POLL_INTERVAL rather than after the full
                # wait. A negative return means the cycle completed
                # without interruption -- safe to try a batch.
                target_seconds = max(float(hours) * 3600.0, 60.0)
                interrupted = await self._wait_in_slices(
                    target_seconds, hours
                )
                if interrupted:
                    if self._shutdown_event.is_set():
                        break
                    # Settings changed -- restart the loop with
                    # whatever the new value is.
                    continue

                await self._try_create_batch()

            except asyncio.CancelledError:
                # Propagated from stop() -- re-raise so the task
                # ends cleanly instead of being silently swallowed.
                raise
            except Exception as exc:
                logger.exception(
                    "BatchScheduler loop iteration failed; "
                    "backing off 5s before retrying: %s",
                    exc,
                )
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    pass

    # ------------------------------------------------------------------
    # Interruptible waits
    # ------------------------------------------------------------------

    async def _wait_in_slices(
        self, total_seconds: float, hours_at_start: Optional[int]
    ) -> bool:
        """Wait ``total_seconds``, re-reading settings between slices.

        Parameters
        ----------
        total_seconds
            Total time to wait (``hours * 3600``).
        hours_at_start
            Snapshot of the setting at the moment the wait began.
            Re-read between slices; a difference means the
            operator changed the setting and the cycle must
            restart.

        Returns
        -------
        bool
            ``True`` if the wait was interrupted (shutdown
            requested OR settings changed). ``False`` if the
            full duration elapsed without interruption -- the
            caller should then try creating a batch.
        """
        elapsed = 0.0
        while elapsed < total_seconds:
            slice_size = min(
                self._DEFAULT_POLL_INTERVAL_SECONDS,
                total_seconds - elapsed,
            )
            if await self._wait(slice_size):
                return True
            elapsed += slice_size

            current = self._read_schedule_hours()
            if current != hours_at_start:
                logger.info(
                    "BatchScheduler: settings changed during wait "
                    "(%r -> %r); restarting cycle.",
                    hours_at_start,
                    current,
                )
                return True
        return False

    async def _wait(self, seconds: float) -> bool:
        """Sleep for ``seconds``, interruptible by ``_shutdown_event``.

        Returns
        -------
        bool
            ``True`` when shutdown was requested during the
            wait; ``False`` when the timeout elapsed normally.
        """
        if seconds <= 0:
            # Defensive: a non-positive wait would busy-loop.
            seconds = 1.0
        try:
            await asyncio.wait_for(
                self._shutdown_event.wait(), timeout=seconds
            )
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------------
    # Settings read
    # ------------------------------------------------------------------

    def _read_schedule_hours(self) -> Optional[int]:
        """Return the effective schedule interval, or ``None``.

        Returns ``None`` when auto-scheduling is inactive for ANY
        of these reasons:

          * the extraction pipeline is not initialized
            (``batch_service`` is ``None``),
          * ``settings.ai.batch_enabled`` is ``False``,
          * ``settings.ai.batch_auto_schedule_hours`` is ``None``
            or not a valid positive integer,
          * reading settings raised (broken file, closed service).

        The caller (``_run_loop``) cannot distinguish these cases
        from the return value alone -- each individual failure
        mode is logged at DEBUG by the layer that detected it, so
        an operator debugging "why isn't my scheduler running?"
        can grep for the specific gate in the server log.
        """
        # Gate 1: extraction pipeline present?
        try:
            if self._memory_service.batch_service is None:
                return None
        except Exception:
            return None

        # Gate 2: settings readable?
        try:
            ai = self._memory_service.settings.ai
        except Exception:
            return None

        # Gate 3: batch subsystem enabled?
        if not getattr(ai, "batch_enabled", False):
            return None

        # Gate 4: schedule configured?
        hours = getattr(ai, "batch_auto_schedule_hours", None)
        if not isinstance(hours, int) or isinstance(hours, bool):
            return None
        if hours < 1:
            return None

        return hours

    # ------------------------------------------------------------------
    # Batch creation
    # ------------------------------------------------------------------

    async def _try_create_batch(self) -> None:
        """Attempt one auto-batch creation. Never raises.

        Guards (any of these aborts the tick silently):

          * ``batch_service`` disappeared between the last read
            and this call (settings reload mid-wait),
          * ``batch_enabled`` was flipped to False,
          * an existing batch is in ``RUNNING`` or ``PAUSED``
            state.

        The "existing batch" guard is what prevents two
        overlapping runs. ``RUNNING`` means the previous batch is
        still working; ``PAUSED`` means the operator explicitly
        paused it and must resume or cancel before the scheduler
        takes over again.

        If the existence check itself fails (DB error), the tick
        is SKIPPED (not retried) -- creating a batch while unsure
        whether another is running would violate the invariant
        for no benefit. The next tick (``hours`` away) will
        re-attempt.
        """
        service = self._memory_service

        # Gate 1: pipeline present?
        batch_service = service.batch_service
        if batch_service is None:
            logger.debug(
                "BatchScheduler: batch_service unavailable; skipping tick."
            )
            return

        # Gate 2: subsystem enabled?
        try:
            ai = service.settings.ai
        except Exception as exc:
            logger.debug(
                "BatchScheduler: cannot read settings; skipping tick: %s",
                exc,
            )
            return
        if not getattr(ai, "batch_enabled", False):
            logger.debug(
                "BatchScheduler: batch_enabled=False; skipping tick."
            )
            return

        # Gate 3: existing non-terminal batch?
        try:
            running = await batch_service.list_batches(
                status=MemoryBatchStatus.RUNNING, limit=1
            )
            if running:
                logger.info(
                    "BatchScheduler: RUNNING batch %r exists; "
                    "skipping this tick.",
                    running[0].batch_id,
                )
                return
            paused = await batch_service.list_batches(
                status=MemoryBatchStatus.PAUSED, limit=1
            )
            if paused:
                logger.info(
                    "BatchScheduler: PAUSED batch %r exists; "
                    "skipping this tick (operator action pending).",
                    paused[0].batch_id,
                )
                return
        except Exception as exc:
            logger.warning(
                "BatchScheduler: list_batches failed; skipping tick "
                "to avoid racing a possible in-flight batch: %s",
                exc,
            )
            return

        # Attempt creation
        try:
            batch = await batch_service.create_batch(
                triggered_by="auto_scheduler",
                options={
                    # Recorded in the batch row for future
                    # enforcement. Current create_batch stores
                    # the options verbatim and does not enforce
                    # max_sessions; a future route_batch change
                    # can read this field without touching the
                    # scheduler.
                    "max_sessions": self._BATCH_MAX_SESSIONS,
                },
            )
            logger.info(
                "BatchScheduler: auto-batch %s created "
                "(status=%s, jobs=%d).",
                batch.batch_id,
                batch.status.value,
                int(getattr(batch, "job_count", 0) or 0),
            )
        except Exception as exc:
            # Includes BatchServiceUnavailableError (settings
            # disabled between gate checks) and any lower-layer
            # failure. Logged at WARNING -- a failure here is
            # NOT fatal to the app, just to this tick.
            logger.warning(
                "BatchScheduler: auto-batch creation failed "
                "(will retry next tick): %s",
                exc,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["BatchScheduler"]
