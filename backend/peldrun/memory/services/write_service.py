# backend/peldrun/memory/services/write_service.py
"""Single write-policy execution path (audit W3).

Before this module the write policy was executed twice: inline in the
``memory_save`` tool and again in ``omweb``'s ``MemoryWritePipeline``.
The two copies also scanned DIFFERENT amounts of existing entries
(``settings.default_limit`` = 5 vs 500), so the tool silently missed
duplicates older than the five most recent entries.

This module is the ONE implementation of:

  * the dedupe/conflict scan (``fetch_existing_pool`` + the shared
    ``WRITE_DEDUPE_SCAN_LIMIT``),
  * the four-branch decision execution (``execute_decision``).

Layer placement
---------------
It lives in ``peldrun.memory.services`` on purpose. The ``memory_save``
tool is part of ``peldrun`` and MUST NOT import ``omweb`` (the package
boundary the whole split depends on), while ``omweb``'s pipeline and
import adapter may freely import ``peldrun``. Placing the shared path
here lets every caller converge on it without breaking that rule.

Contracts preserved
-------------------
``execute_decision`` PROPAGATES ``MemoryValidationError`` and
``MemoryUnavailableError`` exactly as the two original implementations
did, so each caller keeps its own error translation (structured tool
dict vs ``WriteOutcome.error``). SUPERSEDE archives the target BEFORE
creating the replacement -- the ordering that prevents two active
contradictory facts if the create fails.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, List, Optional

from peldrun.memory.domain import (
    MemoryEntry,
    MemoryPort,
    MemoryStatus,
    MemoryUnavailableError,
)
from peldrun.memory.services.policies import (
    WriteAction,
    WriteDecision,
    decide_write_action,
)

logger = logging.getLogger("peldrun.memory.services.write_service")

WRITE_DEDUPE_SCAN_LIMIT: Final[int] = 500
"""Bounded size of the existing-entries pool used for write decisions.

Single source of truth for EVERY write path. 500 matches the import
adapter's historical scan cap and is cheap relative to the cost of
creating duplicates. Scanning only ``settings.default_limit`` (5) was
the W3 defect: a re-save of an older fact looked new.
"""


@dataclass(frozen=True)
class WriteExecution:
    """Neutral result of executing one ``WriteDecision``.

    Callers map this to their own public shape (the tool's structured
    dict, the pipeline's ``WriteOutcome``). ``memory`` is the entry that
    is now active in the store; ``target_id`` is the entry the action
    targeted (SKIP / SUPERSEDE / REACTIVATE).
    """

    action: WriteAction
    memory: Optional[MemoryEntry] = None
    target_id: Optional[str] = None
    reason: str = ""


async def fetch_existing_pool(
    port: MemoryPort,
    scope_ref: str,
    *,
    limit: int = WRITE_DEDUPE_SCAN_LIMIT,
) -> List[MemoryEntry]:
    """Fetch the pool every write decision is made against.

    ``status="all"`` is REQUIRED: without ARCHIVED / SUPERSEDED rows
    ``REACTIVATE`` can never fire and a delete-then-re-save hits the
    UNIQUE constraint instead of reviving the entry.
    """
    try:
        return await port.search(
            query="",
            scope_refs=[scope_ref],
            limit=limit,
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


async def execute_decision(
    port: MemoryPort,
    decision: WriteDecision,
    scope_ref: str,
) -> WriteExecution:
    """Execute one decision against the port. Errors PROPAGATE."""
    action = decision.action

    if action == WriteAction.CREATE:
        created = await port.create(decision.candidate)
        return WriteExecution(
            action=WriteAction.CREATE,
            memory=created,
            reason=decision.reason,
        )

    if action == WriteAction.SKIP:
        target = decision.target_entry
        return WriteExecution(
            action=WriteAction.SKIP,
            memory=target,
            target_id=target.memory_id if target else None,
            reason=decision.reason,
        )

    if action == WriteAction.SUPERSEDE:
        target = decision.target_entry
        if target is None:
            created = await port.create(decision.candidate)
            return WriteExecution(
                action=WriteAction.CREATE,
                memory=created,
                reason="supersede_without_target_fallback_create",
            )
        # Archive FIRST, then create (see module docstring).
        await port.delete(target.memory_id, scope_ref)
        created = await port.create(decision.candidate)
        return WriteExecution(
            action=WriteAction.SUPERSEDE,
            memory=created,
            target_id=target.memory_id,
            reason=decision.reason,
        )

    if action == WriteAction.REACTIVATE:
        target = decision.target_entry
        if target is None:
            created = await port.create(decision.candidate)
            return WriteExecution(
                action=WriteAction.CREATE,
                memory=created,
                reason="reactivate_without_target_fallback_create",
            )
        revived = target.model_copy(
            update={
                "status": MemoryStatus.ACTIVE,
                "content": decision.candidate.content,
                "normalized_content": decision.candidate.normalized_content,
                "content_hash": decision.candidate.content_hash,
                "importance": decision.candidate.importance,
                "tags": decision.candidate.tags,
                "provenance": decision.candidate.provenance,
            }
        )
        updated = await port.update(revived)
        return WriteExecution(
            action=WriteAction.REACTIVATE,
            memory=updated,
            target_id=target.memory_id,
            reason=decision.reason,
        )

    raise MemoryUnavailableError(
        f"Unsupported write action: {action!r}",
        context={"action": str(action)},
    )


class WriteService:
    """The single write entrypoint: pool -> policy -> execution.

    Stateless between calls; holds only the immutable port reference.
    ``omweb`` callers (pipeline, import adapter) and ``peldrun`` tools
    converge here so the policy can never diverge per caller.
    """

    def __init__(
        self,
        port: MemoryPort,
        *,
        scan_limit: int = WRITE_DEDUPE_SCAN_LIMIT,
    ) -> None:
        if port is None:
            raise TypeError("port must be a MemoryPort instance, got None")
        self._port = port
        self._scan_limit = max(1, int(scan_limit))

    @property
    def scan_limit(self) -> int:
        """Pool size in use (diagnostics / settings-contract test)."""
        return self._scan_limit

    async def apply(
        self, candidate: MemoryEntry, scope_ref: str
    ) -> WriteExecution:
        pool = await fetch_existing_pool(
            self._port, scope_ref, limit=self._scan_limit
        )
        decision = decide_write_action(candidate, pool)
        return await execute_decision(self._port, decision, scope_ref)


__all__ = [
    "WRITE_DEDUPE_SCAN_LIMIT",
    "WriteExecution",
    "WriteService",
    "execute_decision",
    "fetch_existing_pool",
]
