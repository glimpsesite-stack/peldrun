# backend/omweb/memory/manager/write_pipeline.py
"""
PELDRUN Memory Platform - Memory Write Pipeline.

The REUSABLE write path for memory entries. Every caller that
persists a candidate through the memory subsystem SHOULD go
through this pipeline so that:

    * write policies are applied consistently
      (``decide_write_action`` from P1.4),
    * mode enforcement happens in one place,
    * duplicate detection sees the SAME existing pool,
    * SUPERSEDE / REACTIVATE archival semantics are IDENTICAL
      across all sources.

Relationship to the shared write executor (W3)
----------------------------------------------

Every write caller -- this pipeline, the ``memory_save`` tool,
and the import adapter (``base_adapter``) -- executes its
``WriteDecision`` through ONE shared entrypoint:
``peldrun.memory.services.write_service.execute_decision``.
The duplicated inline executors that previously lived in each
caller were removed in the W3 cleanup, and a regression test
(``test_W3_single_write_entrypoint_exists``) fails if any of
them reappears.

This pipeline remains the REUSABLE orchestration layer on top
of that executor. It owns the concerns a bare executor does
not:

    * mode enforcement (``settings.mode.can_write``),
    * candidate/scope verification before any work,
    * fetching the existing pool ONCE per call with
      ``status="all"`` so REACTIVATE can fire,
    * batch semantics (siblings dedupe against prior creations),
    * per-entry error isolation in ``save_many``,
    * background graph indexing scheduling (Phase 6).

New callers MUST go through this pipeline; no caller may
re-implement archival semantics inline.

Design invariants
-----------------

1. **Mode enforced at the pipeline.**
   ``save``/``save_many`` check ``settings.mode.can_write``
   before any work. A caller in ``READ_ONLY`` or ``DISABLED``
   gets ``MemoryUnavailableError`` from one place.

2. **Existing pool fetched ONCE per call.**
   The pool used for dedupe/conflict detection is fetched by
   the pipeline itself -- callers cannot accidentally pass a
   stale or partial pool. The ONLY exception is the optional
   ``existing_pool`` parameter on ``save``, used by
   ``save_many``'s internal batching (see invariant 3).

3. **Batched saves see their own prior creations.**
   ``save_many`` fetches the pool ONCE, then appends each
   successful creation. A batch of 10 entries where entry #7
   duplicates entry #3 correctly classifies #7 as SKIP. A naive
   per-entry loop would create the duplicate (racing the
   UNIQUE constraint -- one of the two would fail).

4. **Per-entry errors in batch mode do NOT abort.**
   A validation error on entry #4 does not prevent #5-#10 from
   being attempted. Each outcome carries its own error field.

5. **Scope match is verified up front.**
   A candidate whose scope does not match the requested
   ``scope_ref`` is a caller bug, not a data problem. In
   ``save_many``, this is verified BEFORE the batch starts so
   the error names the offending candidate index.

Cross-reference
---------------

* ``peldrun.memory.services.write_service.execute_decision``
  -- the single W3 execution path for every ``WriteDecision``.
* ``memory_save`` (P1.5) -- migrated to the shared executor.
* ``base_adapter`` (P1.6) -- migrated to the shared executor.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryPort,
    MemorySettings,
    MemoryUnavailableError,
    MemoryValidationError,
)
from peldrun.memory.services import (
    WriteAction,
    WriteDecision,
    decide_write_action,
)
from peldrun.memory.services.write_service import (
    WRITE_DEDUPE_SCAN_LIMIT,
    execute_decision,
)


logger = logging.getLogger("omweb.memory.manager.write_pipeline")


# ---------------------------------------------------------------------------
# Background graph indexing (Phase 6)
# ---------------------------------------------------------------------------
#
# Knowledge-graph indexing is dispatched as a FIRE-AND-FORGET task after
# a successful write. It must never extend the write latency: the write
# is already durable, and a graph index is a derived artifact that can be
# rebuilt.
#
# Why a MODULE-LEVEL task registry (and not an instance attribute)
# ---------------------------------------------------------------
# ``MemoryService`` builds a FRESH ``MemoryWritePipeline`` per job (see
# ``_write_pipeline_provider``), so an instance attribute would be
# discarded moments after the write -- and a task with no strong
# reference can be garbage-collected mid-flight by CPython's cyclic GC,
# silently losing the index. ``asyncio`` only keeps WEAK references to
# running tasks, so the registry below is what actually keeps them alive.
#
# The set is also the shutdown hook: ``drain_graph_tasks`` waits for
# in-flight indexing so a clean ``aclose`` does not leave "Task was
# destroyed but it is pending" warnings or half-written links.

_GRAPH_TASKS: "set[asyncio.Task]" = set()


def pending_graph_task_count() -> int:
    """Return the number of in-flight background graph-index tasks.

    Diagnostics and test hook. Returns to 0 once the tasks settle.
    """
    return len(_GRAPH_TASKS)


async def drain_graph_tasks(timeout_seconds: float = 10.0) -> int:
    """Wait for in-flight graph-index tasks to finish.

    Called by ``MemoryService.aclose`` and by tests that need a
    deterministic end state. A task that exceeds ``timeout_seconds``
    is CANCELLED (not left running against a closing database).

    Returns
    -------
    int
        Number of tasks that were still pending when the drain
        started (0 when there was nothing to wait for).
    """
    tasks = [task for task in _GRAPH_TASKS if not task.done()]
    if not tasks:
        return 0
    done, pending = await asyncio.wait(
        tasks, timeout=max(0.0, float(timeout_seconds))
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
        logger.warning(
            "drain_graph_tasks: cancelled %d task(s) that exceeded "
            "the %.1fs drain budget.",
            len(pending),
            timeout_seconds,
        )
    return len(tasks)


def _schedule_graph_indexing(
    indexer: Any,
    entry: Optional[MemoryEntry],
) -> Optional["asyncio.Task"]:
    """Dispatch ``indexer.index_memory(entry)`` in the background.

    Returns the created task, or ``None`` when nothing was scheduled
    (no indexer, no entry, or no running event loop).

    Never raises. A scheduling failure must not fail a write that
    already succeeded -- the graph would simply miss one memory, and
    the periodic backfill repairs that.
    """
    if indexer is None or entry is None:
        return None
    memory_id = getattr(entry, "memory_id", None)
    if not memory_id:
        return None
    try:
        task = asyncio.create_task(
            indexer.index_memory(entry),
            name=f"memory-graph-index:{memory_id}",
        )
    except RuntimeError as exc:
        # No running loop (a synchronous caller invoked the async
        # pipeline through asyncio.run from another thread, or the
        # loop is already closed during shutdown).
        logger.warning(
            "WritePipeline: could not schedule graph indexing for "
            "%s (no running event loop: %s).", memory_id, exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 -- never fail the write
        logger.warning(
            "WritePipeline: could not schedule graph indexing for "
            "%s (%s: %s).", memory_id, type(exc).__name__, exc,
        )
        return None

    _GRAPH_TASKS.add(task)
    task.add_done_callback(_GRAPH_TASKS.discard)
    return task



_DEFAULT_SCAN_LIMIT: int = WRITE_DEDUPE_SCAN_LIMIT
"""Bounded fetch size for the existing-entries pool.

Aliases the single source of truth in
``peldrun.memory.services.write_service`` (500). Every write
caller -- pipeline, ``memory_save``, import adapter -- scans the
same capped pool. The historical divergence (``memory_save``
scanning only ``settings.default_limit`` = 5) was the W3 defect
that let a re-save of an older fact slip through as new.

For most scopes this fetches every entry; for larger scopes it
fetches the top-500 by recency/importance. Neighbor-based
candidate retrieval (plan G8) replaces the recency ordering in
a later step; the cap itself stays shared.
"""


@dataclass(frozen=True)
class WriteOutcome:
    """Result of one ``save`` or one element of ``save_many``.

    Attributes
    ----------
    action
        The ``WriteAction`` that was applied (or INTENDED to be
        applied, when ``error`` is set).
    memory
        The entry that is now active in the store:

          * CREATE     -> the newly persisted entry,
          * SKIP       -> the pre-existing entry (the target),
          * SUPERSEDE  -> the newly created replacement,
          * REACTIVATE -> the updated (revived) entry.

        ``None`` only on error.
    target_id
        For SKIP/SUPERSEDE/REACTIVATE: the ``memory_id`` of the
        entry the action targeted. ``None`` for CREATE.
    reason
        Diagnostic string, matching ``WriteDecision.reason``.
    error
        When non-None, the action FAILED and this describes why.
        ``action`` and ``reason`` are still populated with the
        INTENDED action so callers can report "tried to
        supersede, failed".
    """

    action: WriteAction
    memory: Optional[MemoryEntry] = None
    target_id: Optional[str] = None
    reason: str = ""
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.error is None

    @property
    def created_new_active(self) -> bool:
        """True when a NEW active entry resulted from the call.

        SKIP returns ``False`` -- the store's state did not change.
        """
        return (
            self.succeeded
            and self.action
            in (
                WriteAction.CREATE,
                WriteAction.SUPERSEDE,
                WriteAction.REACTIVATE,
            )
        )


class MemoryWritePipeline:
    """Reusable write path applying ``decide_write_action`` + archival semantics.

    See the module docstring for the design rationale, the
    relationship to ``memory_save``, and the invariants.

    Lifecycle
    ---------
    Construct one pipeline per ``MemoryService`` instance (or per
    test). The pipeline is STATELESS between calls -- it holds
    only the immutable ``port`` and ``settings`` references. A
    settings reload between calls is observed automatically
    because ``save`` reads ``self._settings.mode`` on every
    call.

    Concurrency
    -----------
    Concurrent ``save`` calls against the same pipeline serialize
    at the SQLite level (single writer). No lock is needed at the
    Python layer -- the pipeline holds no mutable state.
    """

    def __init__(
        self,
        port: MemoryPort,
        settings: MemorySettings,
        *,
        graph_indexer: Optional[Any] = None,
    ) -> None:
        """Construct the pipeline.

        Parameters
        ----------
        port
            The live ``MemoryPort`` (the provider).
        settings
            The current settings snapshot.
        graph_indexer
            Optional Phase 6 ``GraphIndexer``. When supplied, every
            SUCCESSFUL write schedules a background
            ``index_memory(entry)`` task -- never awaited, so write
            latency is unchanged. ``None`` (the default, and the
            behavior of every pre-Phase-6 caller) means graph
            indexing is off and the write path is byte-for-byte
            identical to Phase 5.

            Typed as ``Any`` to keep this module decoupled from
            ``omweb.memory.graph.indexer``; anything exposing an
            async ``index_memory(entry)`` coroutine works.

        Raises
        ------
        TypeError
            When ``port`` is ``None`` or ``settings`` is not a
            ``MemorySettings``.
        """
        if port is None:
            raise TypeError("port must be a MemoryPort instance, got None")
        if not isinstance(settings, MemorySettings):
            raise TypeError(
                f"settings must be MemorySettings, got "
                f"{type(settings).__name__}"
            )
        self._port: MemoryPort = port
        self._settings: MemorySettings = settings
        #: Phase 6: optional background graph indexer. ``None``
        #: disables graph indexing for this pipeline instance.
        self._graph_indexer: Optional[Any] = graph_indexer

    # ------------------------------------------------------------------
    # Single save
    # ------------------------------------------------------------------

    async def save(
        self,
        candidate: MemoryEntry,
        *,
        scope_ref: str,
        existing_pool: Optional[List[MemoryEntry]] = None,
    ) -> WriteOutcome:
        """Apply the write policy to ``candidate`` and persist the result.

        Parameters
        ----------
        candidate
            The ``MemoryEntry`` to save. Its scope MUST match
            ``scope_ref`` -- mismatch raises
            ``MemoryValidationError``.
        scope_ref
            The canonical scope reference string
            (``"<type>:<id>"``).
        existing_pool
            Optional pre-fetched pool of existing entries. When
            ``None`` (the default), the pipeline fetches it.
            Only pass a non-None value when you are confident
            it is complete enough for dedupe detection --
            ``save_many`` uses this internally.

        Returns
        -------
        WriteOutcome
            Never raises for write-time errors (backend failure,
            validation failure). The ONE exception that DOES
            propagate is ``MemoryUnavailableError`` when
            ``mode=DISABLED`` or the pool fetch itself fails --
            at that point NO write was attempted, so a per-entry
            error field would be misleading.
        """
        self._enforce_can_write()
        self._verify_scope_matches(candidate, scope_ref)

        pool = existing_pool
        if pool is None:
            pool = await self._fetch_existing_pool(scope_ref)

        try:
            decision = decide_write_action(candidate, pool)
        except Exception as exc:
            # ``decide_write_action`` is documented as never
            # raising; this is defense in depth against a
            # future regression.
            logger.exception(
                "WritePipeline: decide_write_action raised for "
                "scope %s: %s", scope_ref, exc,
            )
            return WriteOutcome(
                action=WriteAction.CREATE,  # safe default
                error=f"decision_failed: {exc}",
            )

        outcome = await self._execute_decision_shared(
            decision=decision,
            candidate=candidate,
            scope_ref=scope_ref,
        )
        # Phase 6: derived graph artifact, scheduled in the
        # background. ``save`` does not wait for it.
        self._schedule_graph_indexing_for([outcome])
        return outcome

    # ------------------------------------------------------------------
    # Batch save
    # ------------------------------------------------------------------

    async def save_many(
        self,
        candidates: List[MemoryEntry],
        *,
        scope_ref: str,
    ) -> List[WriteOutcome]:
        """Apply the write policy to each candidate.

        Returns a list of the SAME length as ``candidates`` -- one
        outcome per input, in the same order. Callers can zip
        them together for per-candidate status.

        See the module docstring invariants 3 and 4 for the
        batch semantics.
        """
        self._enforce_can_write()

        if not candidates:
            return []

        # Verify scope for every candidate UP FRONT. A mismatch on
        # candidate #17 is cheaper to catch here than mid-batch.
        for idx, candidate in enumerate(candidates):
            try:
                self._verify_scope_matches(candidate, scope_ref)
            except MemoryValidationError as exc:
                raise MemoryValidationError(
                    f"candidate {idx + 1}: {exc.message}",
                    field_name=exc.field_name,
                    context={**exc.context, "candidate_index": idx},
                    cause=exc,
                ) from exc

        pool = await self._fetch_existing_pool(scope_ref)

        outcomes: List[WriteOutcome] = []
        for idx, candidate in enumerate(candidates):
            try:
                decision = decide_write_action(candidate, pool)
            except Exception as exc:
                outcomes.append(
                    WriteOutcome(
                        action=WriteAction.CREATE,
                        error=(
                            f"decision_failed: {exc} "
                            f"(candidate {idx + 1})"
                        ),
                    )
                )
                continue

            outcome = await self._execute_decision_shared(
                decision=decision,
                candidate=candidate,
                scope_ref=scope_ref,
            )
            outcomes.append(outcome)

            # Update the pool so subsequent candidates in the
            # SAME batch dedupe correctly against their siblings.
            if outcome.succeeded:
                if outcome.action == WriteAction.CREATE and outcome.memory is not None:
                    pool.append(outcome.memory)
                elif outcome.action == WriteAction.SUPERSEDE and outcome.memory is not None:
                    if decision.target_entry is not None:
                        try:
                            pool.remove(decision.target_entry)
                        except ValueError:
                            pass
                    pool.append(outcome.memory)
                elif outcome.action == WriteAction.REACTIVATE and outcome.memory is not None:
                    if decision.target_entry is not None:
                        try:
                            old_idx = pool.index(decision.target_entry)
                            pool[old_idx] = outcome.memory
                        except ValueError:
                            pool.append(outcome.memory)

        # Phase 6: schedule graph indexing for every write that
        # actually changed the stored content. Dispatched AFTER the
        # whole batch so a slow LLM cannot interleave with the
        # write loop, and never awaited -- the writes are already
        # durable.
        self._schedule_graph_indexing_for(outcomes)

        logger.info(
            "WritePipeline.save_many: %d candidate(s) in scope %s "
            "-> create=%d, skip=%d, supersede=%d, reactivate=%d, "
            "failed=%d",
            len(candidates),
            scope_ref,
            sum(1 for o in outcomes if o.action == WriteAction.CREATE and o.succeeded),
            sum(1 for o in outcomes if o.action == WriteAction.SKIP and o.succeeded),
            sum(1 for o in outcomes if o.action == WriteAction.SUPERSEDE and o.succeeded),
            sum(1 for o in outcomes if o.action == WriteAction.REACTIVATE and o.succeeded),
            sum(1 for o in outcomes if not o.succeeded),
        )
        return outcomes

    # ------------------------------------------------------------------
    # Internal: Phase 6 background graph indexing
    # ------------------------------------------------------------------

    def _schedule_graph_indexing_for(
        self, outcomes: List["WriteOutcome"]
    ) -> None:
        """Schedule background indexing for successful writes.

        Scheduling rules:

          * ``SKIP`` is excluded -- the entry already existed with
            identical content, so the graph already holds exactly
            these entities. Re-indexing would burn an LLM call for a
            no-op.
          * ``SUPERSEDE`` and ``REACTIVATE`` ARE indexed -- their
            ``memory`` is the NEW row, whose content may differ from
            the row it replaced.
          * ``CREATE`` is indexed.

        Never raises: a scheduling problem is logged inside
        ``_schedule_graph_indexing`` and the write still succeeds.
        """
        if self._graph_indexer is None:
            return
        for outcome in outcomes:
            if not outcome.succeeded or outcome.memory is None:
                continue
            if outcome.action == WriteAction.SKIP:
                continue
            _schedule_graph_indexing(
                self._graph_indexer, outcome.memory
            )

    @property
    def graph_indexer(self) -> Optional[Any]:
        """The configured background graph indexer, or ``None``."""
        return self._graph_indexer

    # ------------------------------------------------------------------
    # Internal: enforcement
    # ------------------------------------------------------------------

    def _enforce_can_write(self) -> None:
        if not self._settings.mode.can_write:
            raise MemoryUnavailableError(
                f"Memory writes are disabled "
                f"(mode={self._settings.mode.value}). Set mode to "
                f"'read_write' to enable writes.",
                context={"mode": self._settings.mode.value},
            )

    @staticmethod
    def _verify_scope_matches(
        candidate: MemoryEntry, scope_ref: str
    ) -> None:
        """Ensure ``candidate``'s scope matches ``scope_ref``."""
        expected = f"{candidate.scope_type.value}:{candidate.scope_id}"
        if expected != scope_ref:
            raise MemoryValidationError(
                f"candidate scope ({expected!r}) does not match "
                f"the requested write scope ({scope_ref!r})",
                field_name="scope_ref",
                context={
                    "candidate_scope": expected,
                    "scope_ref": scope_ref,
                },
            )

    async def _fetch_existing_pool(
        self, scope_ref: str
    ) -> List[MemoryEntry]:
        """Fetch the existing-entries pool for the given scope.

        Uses ``status="all"`` (CRITICAL) so ARCHIVED and SUPERSEDED
        entries are visible. Without them, ``REACTIVATE`` could
        never fire and the "user deletes then re-saves" flow
        would hit the UNIQUE constraint instead of reactivating.
        """
        try:
            return await self._port.search(
                query="",
                scope_refs=[scope_ref],
                limit=_DEFAULT_SCAN_LIMIT,
                status="all",
            )
        except MemoryUnavailableError:
            raise
        except Exception as exc:
            raise MemoryUnavailableError(
                f"Failed to fetch existing entries for scope "
                f"{scope_ref!r}: {exc}",
                context={"scope_ref": scope_ref},
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Internal: execute decision
    # ------------------------------------------------------------------

    async def _execute_decision_shared(
        self,
        *,
        decision: WriteDecision,
        candidate: MemoryEntry,
        scope_ref: str,
    ) -> WriteOutcome:
        """Execute via the shared write path (W3), map to WriteOutcome.

        Error translation is IDENTICAL to the legacy inline branches:
        validation -> ``validation_error: ...``, backend ->
        ``backend_unavailable: ...``, both preserving the INTENDED
        action. The legacy inline executor was removed in the
        W3 cleanup; this is the pipeline's ONLY execution path.
        """
        try:
            result = await execute_decision(self._port, decision, scope_ref)
            return WriteOutcome(
                action=result.action,
                memory=result.memory,
                target_id=result.target_id,
                reason=result.reason,
            )
        except MemoryValidationError as exc:
            logger.warning(
                "WritePipeline: validation failure during %s "
                "(scope=%s): %s",
                decision.action.value,
                scope_ref,
                exc.message,
            )
            return WriteOutcome(
                action=decision.action,
                target_id=(
                    decision.target_entry.memory_id
                    if decision.target_entry
                    else None
                ),
                reason=decision.reason,
                error=f"validation_error: {exc.message}",
            )
        except MemoryUnavailableError as exc:
            logger.error(
                "WritePipeline: backend failure during %s "
                "(scope=%s): %s",
                decision.action.value,
                scope_ref,
                exc.message,
            )
            return WriteOutcome(
                action=decision.action,
                target_id=(
                    decision.target_entry.memory_id
                    if decision.target_entry
                    else None
                ),
                reason=decision.reason,
                error=f"backend_unavailable: {exc.message}",
            )


__all__ = ["MemoryWritePipeline", "WriteOutcome"]
