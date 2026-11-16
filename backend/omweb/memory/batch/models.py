# backend/omweb/memory/batch/models.py
"""
PELDRUN Memory Platform - Batch Domain Model.

Mirrors the ``memory_batches`` SQL table (Migration v3) as a
Pydantic v2 value object. Same design contract as
``omweb.memory.jobs.models.MemoryJob``:

    * 1:1 field mapping with the SQL schema.
    * Float timestamps (Unix seconds).
    * Enum-typed ``status``.
    * ``chat_ids`` and ``options`` exposed as Python native types
      (list / dict); the repository handles JSON encode/decode.

Why a separate module from ``jobs.models``
------------------------------------------

A batch is a CONTAINER of jobs, not a job itself. Grouping it
under ``omweb.memory.batch`` (rather than adding it to
``omweb.memory.jobs.models``) makes the dependency direction
explicit:

    batch.models  <-  jobs.outbox (imports MemoryBatch for the
                                   batch CRUD methods)
    batch.service ->  jobs.outbox + extraction.trigger_router
                      (orchestrates, does not extend either)

The outbox layer needs to know about ``MemoryBatch`` (to store
and return them), but the batch package does NOT need to import
anything from the jobs layer -- a clean one-way dependency.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


class MemoryBatchStatus(str, Enum):
    """Lifecycle status of a ``memory_batches`` row.

    States and transitions
    ----------------------

        pending   -> running        (auto, after first jobs enqueued)
        pending   -> completed      (auto, if 0 jobs to enqueue)
        pending   -> cancelled      (user)
        pending   -> failed         (unrecoverable error at creation)

        running   -> paused         (user)
        running   -> completed      (auto, all jobs terminal)
        running   -> cancelled      (user)
        running   -> failed         (unrecoverable error, e.g. DB lost)

        paused    -> running        (user resumes)
        paused    -> cancelled      (user)

        completed -> (terminal)
        cancelled -> (terminal)
        failed    -> (terminal)

    Only ``completed``, ``cancelled``, and ``failed`` are terminal.
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (
            MemoryBatchStatus.COMPLETED,
            MemoryBatchStatus.CANCELLED,
            MemoryBatchStatus.FAILED,
        )

    @property
    def is_active(self) -> bool:
        """True when the batch is between creation and a terminal state."""
        return not self.is_terminal


# ---------------------------------------------------------------------------
# MemoryBatch
# ---------------------------------------------------------------------------


class MemoryBatch(BaseModel):
    """One row of ``memory_batches`` -- one batch operation.

    Constructed by ``MemoryOutbox.create_batch`` (via
    ``MemoryBatchService.create_batch``); updated in place by
    ``update_batch`` as the batch progresses. The class is
    treated as immutable-by-convention: updates go through
    ``model_copy(update=...)`` so Pydantic re-runs validators
    on every transition.

    Counters (``completed_jobs`` etc.) are HISTORICAL: they
    record what was true when the batch was last written. The
    authoritative live view is always computed by
    ``MemoryOutbox.aggregate_batch_progress`` from the
    ``memory_jobs`` table, which cannot drift. The API merges
    both (config + live aggregate) so callers see accurate
    progress regardless of how stale the row is.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- identity ------------------------------------

    batch_id: str = Field(
        default_factory=lambda: f"batch_{uuid4().hex[:16]}",
        description=(
            "Stable batch identifier. 'batch_' prefix + 16 hex "
            "chars. Never reused across batches."
        ),
    )

    # ------------------------- lifecycle -----------------------------------

    status: MemoryBatchStatus = Field(
        default=MemoryBatchStatus.PENDING,
        description="Current lifecycle status (see enum).",
    )
    triggered_by: str = Field(
        default="manual",
        description=(
            "Who / what created this batch. 'manual' for the UI "
            "button; 'auto_schedule' for the batch scheduler; "
            "any other string is preserved verbatim for custom "
            "integrations."
        ),
    )

    # ------------------------- scope (job spec) ----------------------------

    project_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional project scope. When set, the batch UI "
            "shows only this project's sessions. NOTE: the "
            "current ``route_batch`` scans ALL projects -- this "
            "field is metadata, not a filter. A future "
            "extension will thread it into ``scan_all_sessions``."
        ),
    )
    chat_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Optional explicit chat list. Same caveat as "
            "``project_id``: recorded as metadata; the current "
            "route does not honor it. Callers that need a "
            "per-chat batch use the from-session endpoint "
            "instead."
        ),
    )
    date_from: Optional[float] = Field(
        default=None,
        description=(
            "Optional lower bound on session updated_at. Metadata "
            "only (see ``project_id``)."
        ),
    )
    date_to: Optional[float] = Field(
        default=None,
        description="Optional upper bound (metadata only).",
    )

    # ------------------------- work counters -------------------------------

    session_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of sessions the batch targeted. Set at "
            "creation from ``len(jobs)`` of the initial "
            "``route_batch`` call. Historical."
        ),
    )
    job_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of jobs the batch enqueued at creation. "
            "Historical; live count is from aggregate."
        ),
    )
    completed_jobs: int = Field(default=0, ge=0)
    failed_jobs: int = Field(default=0, ge=0)
    cancelled_jobs: int = Field(default=0, ge=0)
    total_candidates: int = Field(
        default=0,
        ge=0,
        description="Sum of ``memory_jobs.candidates_count``.",
    )
    total_memories_created: int = Field(
        default=0,
        ge=0,
        description="Sum of ``memory_jobs.created_memories_count``.",
    )

    # ------------------------- cost ----------------------------------------

    estimated_cost_nano_usd: int = Field(
        default=0,
        ge=0,
        description=(
            "Rough cost estimate made at creation time. See "
            "``MemoryBatchService.estimate_cost`` for the "
            "heuristic."
        ),
    )
    actual_cost_nano_usd: int = Field(
        default=0,
        ge=0,
        description="Live sum of ``memory_jobs.cost_nano_usd``.",
    )

    # ------------------------- model snapshot ------------------------------

    model_provider_id: Optional[str] = Field(
        default=None,
        description=(
            "Provider id of the extraction model used by the "
            "batch's jobs. Snapshotted at creation so the UI "
            "shows which model ran the batch even after the "
            "operator changes the global setting."
        ),
    )
    model_id: Optional[str] = Field(
        default=None,
        description="Model id used by the batch's jobs.",
    )
    min_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "min_confidence setting in effect at creation time "
            "(audit trail)."
        ),
    )

    # ------------------------- timings -------------------------------------

    created_at: float = Field(
        default_factory=time.time,
        description="Batch creation timestamp (Unix seconds).",
    )
    started_at: Optional[float] = Field(
        default=None,
        description="Timestamp when status first became 'running'.",
    )
    paused_at: Optional[float] = Field(
        default=None,
        description="Timestamp of the most recent pause.",
    )
    completed_at: Optional[float] = Field(
        default=None,
        description="Timestamp when status became 'completed'.",
    )
    cancelled_at: Optional[float] = Field(
        default=None,
        description="Timestamp when status became 'cancelled'.",
    )
    last_progress_at: Optional[float] = Field(
        default=None,
        description=(
            "Last time the batch's aggregate progress changed. "
            "Used by the UI to distinguish a 'stuck' batch from "
            "one that has simply not started."
        ),
    )

    # ------------------------- error / options -----------------------------

    error_message: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable failure detail when status='failed'. "
            "NULL for every other state."
        ),
    )
    options: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form options bag for future extensions "
            "(e.g. per-batch model overrides, scope filters). "
            "Mirrors ``options_json`` in SQL."
        ),
    )

    # ------------------------- validators ----------------------------------

    @field_validator("batch_id", mode="after")
    @classmethod
    def _validate_batch_id(cls, v: str) -> str:
        if not isinstance(v, str):
            raise ValueError(
                f"batch_id must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("batch_id must not be empty")
        return stripped

    @field_validator("chat_ids", mode="after")
    @classmethod
    def _clean_chat_ids(cls, v: List[str]) -> List[str]:
        """Trim each entry, drop empties, preserve order."""
        cleaned: List[str] = []
        for entry in v:
            if not isinstance(entry, str):
                continue
            s = entry.strip()
            if s:
                cleaned.append(s)
        return cleaned

    # ------------------------- derived --------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def is_active(self) -> bool:
        return self.status.is_active

    # ------------------------- serialization --------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe dict for API responses."""
        return {
            "batch_id": self.batch_id,
            "status": self.status.value,
            "triggered_by": self.triggered_by,
            "project_id": self.project_id,
            "chat_ids": list(self.chat_ids),
            "date_from": self.date_from,
            "date_to": self.date_to,
            "session_count": self.session_count,
            "job_count": self.job_count,
            "completed_jobs": self.completed_jobs,
            "failed_jobs": self.failed_jobs,
            "cancelled_jobs": self.cancelled_jobs,
            "total_candidates": self.total_candidates,
            "total_memories_created": self.total_memories_created,
            "estimated_cost_nano_usd": self.estimated_cost_nano_usd,
            "actual_cost_nano_usd": self.actual_cost_nano_usd,
            "model_provider_id": self.model_provider_id,
            "model_id": self.model_id,
            "min_confidence": self.min_confidence,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "paused_at": self.paused_at,
            "completed_at": self.completed_at,
            "cancelled_at": self.cancelled_at,
            "last_progress_at": self.last_progress_at,
            "error_message": self.error_message,
            "options": dict(self.options),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemoryBatch",
    "MemoryBatchStatus",
]
