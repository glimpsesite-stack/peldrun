# backend/omweb/memory/api/memory_jobs.py
"""
PELDRUN Memory Platform - Extraction Jobs HTTP Router.

Read / retry / cancel surface for ``memory_jobs``. Powers the
"Extraction Jobs" panel in the Phase 3 UI: an operator-visible
list of every extraction job with its lifecycle status, attempts,
cost, and error, plus the two operator actions the plan defines
(retry a terminal job, cancel an active one).

Endpoints
---------

    GET  /jobs                   -- paginated, filtered list
    GET  /jobs/{job_id}          -- single job by id
    POST /jobs/{job_id}/retry    -- requeue failed / cancelled
    POST /jobs/{job_id}/cancel   -- cancel pending / retry_pending / running

State contracts
---------------

**Retry** accepts only ``failed`` and ``cancelled``. Any other
state returns 409 Conflict with ``error="invalid_state"``.
Rationale:

  * ``pending`` / ``retry_pending`` / ``running`` already have
    work in flight -- requeue would duplicate the worker's
    effort and race the existing transition.
  * ``completed`` is a terminal success -- re-executing would
    double-extract (and, on a v2 database, would defeat the
    cursor invariant that ``complete()`` maintains).
  * A retry IS the sanctioned remediation for ``SESSION_CHANGED``
    / ``EXTRACTION_FAILED`` (plan §19.3); for those two states
    it preserves the ``message_snapshot_hash`` so the worker
    can detect if the underlying session changed again.

This matches ``MemoryOutbox.requeue``'s contract exactly.

**Cancel** accepts only ``pending``, ``retry_pending``, and
``running``. Terminal states (``completed``, ``failed``,
``cancelled``) return 409. Rationale:

  * Cancelling a ``completed`` job would silently erase the
    cursor advance; the audit trail must be preserved.
  * Cancelling a ``failed`` job would hide the diagnostic.
  * A ``running`` job CAN be cancelled: the worker will
    notice on its next heartbeat (``update_heartbeat`` is a
    no-op for a non-running job) and the current attempt's
    ``complete()`` / ``fail()`` / ``retry()`` will raise
    ``JobNotFoundError``, which the worker already handles
    gracefully.

This matches ``MemoryOutbox.cancel_if_active``'s contract.

Availability
------------

Both the list and the actions require the extraction pipeline
to be initialized on the running service. When
``service.outbox`` is ``None`` -- AI extraction disabled in
settings, memory mode forbids writes, or provider is not
SQLite -- all four endpoints return 503 with
``error="extraction_unavailable"``. This is the correct
behavior: the operator cannot manage jobs for a pipeline that
is not running.

Response shapes
---------------

List (HTTP 200)::

    {
      "status": "ok",
      "operation": "jobs_list",
      "jobs": [ {...MemoryJob.to_dict()...}, ... ],
      "total": 42,
      "limit": 50,
      "offset": 0,
      "has_more": false
    }

Single (HTTP 200)::

    {
      "status": "ok",
      "operation": "job_get",
      "job": {...}
    }

Retry / Cancel (HTTP 200)::

    {
      "status": "ok",
      "operation": "job_retry" | "job_cancel",
      "job": {...post-transition...}
    }

Errors use the same envelope as the rest of the memory API
(``{status, operation, error, message, [fields]}``).

Pagination + total race
-----------------------

``list_jobs`` computes ``total`` (via ``count_jobs``) and the
page itself (via ``list_jobs``) in two separate SQL calls. A
concurrent enqueue between the two can make ``total`` briefly
disagree with what the caller would get by paging to the end.
This is acceptable for a UI listing that refreshes
periodically; the alternative (``SELECT COUNT(*) OVER()``
window function) would require SQLite >= 3.25 AND couples
this file to the query planner for negligible benefit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status as http_status
from fastapi.responses import JSONResponse

from peldrun.memory.domain.enums import (
    EXTRACTION_TRIGGERS,
    MEMORY_SCOPES,
)
from omweb.memory.jobs.models import MemoryJobStatus
from omweb.memory.jobs.outbox import JobNotFoundError

from omweb.memory import get_memory_service


logger = logging.getLogger("omweb.memory.api.jobs")


router = APIRouter()


# ---------------------------------------------------------------------------
# Accepted vocabularies
# ---------------------------------------------------------------------------


_ACCEPTED_STATUSES: frozenset = frozenset(
    {s.value for s in MemoryJobStatus} | {"all"}
)

_ACCEPTED_TRIGGERS: frozenset = frozenset(
    {t.value for t in EXTRACTION_TRIGGERS} | {"all"}
)

_DEFAULT_LIMIT: int = 50
_MAX_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _raise_400(*, error: str, message: str) -> None:
    """Raise a structured 400 that matches the memory API envelope."""
    raise HTTPException(
        status_code=http_status.HTTP_400_BAD_REQUEST,
        detail={"error": error, "message": message},
    )


def _validate_status(value: Optional[str]) -> Optional[str]:
    """Validate and normalize the ``status`` query param.

    Returns ``None`` for the ``"all"`` alias (meaning "no
    filter"). Returns the canonical lowercase status string
    otherwise. Any other value raises 400 with the structured
    envelope (never FastAPI's default 422).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        _raise_400(
            error="invalid_status",
            message=(
                f"'status' must be one of "
                f"{sorted(_ACCEPTED_STATUSES)}"
            ),
        )
    normalized = value.strip().lower()
    if normalized not in _ACCEPTED_STATUSES:
        _raise_400(
            error="invalid_status",
            message=(
                f"unknown status {value!r}; expected one of "
                f"{sorted(_ACCEPTED_STATUSES)}"
            ),
        )
    if normalized == "all":
        return None
    return normalized


def _validate_trigger(value: Optional[str]) -> Optional[str]:
    """Validate and normalize the ``trigger_type`` query param.

    Same aliasing contract as ``_validate_status``.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        _raise_400(
            error="invalid_trigger",
            message=(
                f"'trigger_type' must be one of "
                f"{sorted(_ACCEPTED_TRIGGERS)}"
            ),
        )
    normalized = value.strip().lower()
    if normalized not in _ACCEPTED_TRIGGERS:
        _raise_400(
            error="invalid_trigger",
            message=(
                f"unknown trigger_type {value!r}; expected one "
                f"of {sorted(_ACCEPTED_TRIGGERS)}"
            ),
        )
    if normalized == "all":
        return None
    return normalized


def _validate_pagination(
    limit: int, offset: int
) -> tuple[int, int]:
    """Clamp limit and offset to their accepted ranges."""
    if not isinstance(limit, int):
        _raise_400(
            error="invalid_limit",
            message="'limit' must be an integer",
        )
    if not isinstance(offset, int):
        _raise_400(
            error="invalid_offset",
            message="'offset' must be an integer",
        )
    if offset < 0:
        _raise_400(
            error="invalid_offset",
            message="'offset' must be >= 0",
        )
    # Clamp limit; the outbox also clamps defensively, but
    # clamping here keeps the echoed response honest about the
    # effective value.
    clamped = max(1, min(limit, _MAX_LIMIT))
    return clamped, offset


def _clean_chat_id(value: Optional[str]) -> Optional[str]:
    """Trim and validate the ``chat_id`` query param."""
    if value is None:
        return None
    clean = value.strip()
    return clean or None


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _error_response(
    exc: BaseException,
    *,
    operation: str,
) -> JSONResponse:
    """Map a job-layer exception to an HTTP response.

    Only unexpected exceptions reach here; the specific cases
    (``JobNotFoundError``, ``ValueError`` for state conflicts)
    are handled inline in each endpoint because each maps to a
    different status code with a specific field on the detail
    object.
    """
    logger.exception(
        "Memory jobs %s failed: %s", operation, exc,
    )
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": "Internal error during jobs operation.",
        },
    )


def _not_found_response(*, operation: str, job_id: str) -> JSONResponse:
    """404 envelope for an unknown ``job_id``."""
    return JSONResponse(
        status_code=http_status.HTTP_404_NOT_FOUND,
        content={
            "status": "error",
            "operation": operation,
            "error": "job_not_found",
            "message": f"No job with id {job_id!r}.",
            "job_id": job_id,
        },
    )


def _state_conflict_response(
    *, operation: str, job_id: str, message: str
) -> JSONResponse:
    """409 envelope for an illegal state transition."""
    return JSONResponse(
        status_code=http_status.HTTP_409_CONFLICT,
        content={
            "status": "error",
            "operation": operation,
            "error": "invalid_state",
            "message": message,
            "job_id": job_id,
        },
    )


def _extraction_unavailable_response(*, operation: str) -> JSONResponse:
    """503 envelope when the extraction pipeline is not running."""
    return JSONResponse(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "error",
            "operation": operation,
            "error": "extraction_unavailable",
            "message": (
                "AI extraction is not enabled. Enable it via "
                "PATCH /api/memory/automation/settings before "
                "managing jobs."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Service access
# ---------------------------------------------------------------------------


def _get_service_or_500():
    """Return the ``MemoryService`` singleton or raise HTTP 500."""
    try:
        return get_memory_service()
    except RuntimeError as exc:
        logger.error(
            "MemoryService is not initialized: %s", exc
        )
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "service_not_initialized",
                "message": (
                    "Memory service is not initialized. This is a "
                    "startup ordering bug; contact the operator."
                ),
            },
        ) from exc


def _get_outbox_or_none():
    """Return the outbox, or None when extraction is not running."""
    service = _get_service_or_500()
    return service.outbox


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _job_to_dict(job: Any) -> Dict[str, Any]:
    """Serialize a ``MemoryJob`` via its canonical ``to_dict``.

    Every response embeds this dict directly. ``MemoryJob.to_dict``
    already emits JSON-safe primitives (enums as strings,
    ``None`` where appropriate, floats for timestamps).
    """
    return job.to_dict()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/jobs",
    summary="List extraction jobs",
    response_model=None,
)
async def list_jobs_endpoint(
    status: Optional[str] = Query(
        None,
        description=(
            "Filter by status: pending | running | retry_pending "
            "| completed | failed | cancelled | all"
        ),
    ),
    chat_id: Optional[str] = Query(
        None,
        description="Filter by chat id",
    ),
    trigger_type: Optional[str] = Query(
        None,
        description=(
            "Filter by trigger: after_turn | after_session | "
            "batch | manual | all"
        ),
    ),
    limit: int = Query(
        _DEFAULT_LIMIT,
        ge=1,
        le=_MAX_LIMIT,
        description="Page size (1..200)",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Page offset (>= 0)",
    ),
) -> JSONResponse:
    """Paginated, filtered list of ``memory_jobs``.

    See the module docstring for the pagination + total race
    caveat. Filters are AND-combined; each may be ``all`` or
    omitted to disable it.
    """
    clean_status = _validate_status(status)
    clean_trigger = _validate_trigger(trigger_type)
    clean_chat = _clean_chat_id(chat_id)
    clean_limit, clean_offset = _validate_pagination(limit, offset)

    outbox = _get_outbox_or_none()
    if outbox is None:
        return _extraction_unavailable_response(operation="jobs_list")

    try:
        jobs = await outbox.list_jobs(
            status=clean_status,
            chat_id=clean_chat,
            trigger_type=clean_trigger,
            limit=clean_limit,
            offset=clean_offset,
        )
        total = await outbox.count_jobs(
            status=clean_status,
            chat_id=clean_chat,
            trigger_type=clean_trigger,
        )
    except Exception as exc:
        return _error_response(exc, operation="jobs_list")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "jobs_list",
            "jobs": [_job_to_dict(j) for j in jobs],
            "total": int(total),
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": clean_offset + len(jobs) < int(total),
        },
    )


@router.get(
    "/jobs/{job_id}",
    summary="Get a single extraction job",
    response_model=None,
)
async def get_job_endpoint(job_id: str) -> JSONResponse:
    """Return one job by id.

    404 when the id is unknown. This is the ONLY endpoint that
    distinguishes "job exists but is old" from "job does not
    exist" -- and it does so purely by id presence, with no
    scope filter (the jobs table is a global audit trail; the
    API is trusted with the whole table).
    """
    if not isinstance(job_id, str) or not job_id.strip():
        _raise_400(
            error="invalid_job_id",
            message="job_id must be a non-empty string",
        )
    clean_id = job_id.strip()

    outbox = _get_outbox_or_none()
    if outbox is None:
        return _extraction_unavailable_response(operation="job_get")

    try:
        job = await outbox.get_by_id(clean_id)
    except Exception as exc:
        return _error_response(exc, operation="job_get")

    if job is None:
        return _not_found_response(
            operation="job_get", job_id=clean_id,
        )

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "job_get",
            "job": _job_to_dict(job),
        },
    )


@router.post(
    "/jobs/{job_id}/retry",
    summary="Requeue a failed or cancelled job",
    response_model=None,
)
async def retry_job_endpoint(job_id: str) -> JSONResponse:
    """Re-queue a terminal-failure job for fresh execution.

    Calls ``outbox.requeue``, which enforces the failed /
    cancelled precondition (409 on any other state). The
    response carries the post-transition job with
    ``status="pending"`` and ``attempts=0``.

    The preserved ``message_snapshot_hash`` is intentional: if
    the underlying session changed since the original enqueue,
    the worker's snapshot check will fail the requeued job with
    ``SESSION_CHANGED`` -- forcing the user to explicitly
    accept the new session state rather than silently
    re-extracting from a different conversation.
    """
    if not isinstance(job_id, str) or not job_id.strip():
        _raise_400(
            error="invalid_job_id",
            message="job_id must be a non-empty string",
        )
    clean_id = job_id.strip()

    outbox = _get_outbox_or_none()
    if outbox is None:
        return _extraction_unavailable_response(operation="job_retry")

    try:
        requeued = await outbox.requeue(clean_id)
    except JobNotFoundError:
        return _not_found_response(
            operation="job_retry", job_id=clean_id,
        )
    except ValueError as exc:
        return _state_conflict_response(
            operation="job_retry",
            job_id=clean_id,
            message=str(exc),
        )
    except Exception as exc:
        return _error_response(exc, operation="job_retry")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "job_retry",
            "job": _job_to_dict(requeued),
        },
    )


@router.post(
    "/jobs/{job_id}/cancel",
    summary="Cancel a pending, retry_pending, or running job",
    response_model=None,
)
async def cancel_job_endpoint(job_id: str) -> JSONResponse:
    """Cancel an active job.

    Calls ``outbox.cancel_if_active``, which enforces the
    non-terminal precondition (409 on any terminal state) and
    guards against mid-operation races. The response carries
    the post-transition job with ``status="cancelled"``.

    A ``running`` job CAN be cancelled. The worker's
    ``update_heartbeat`` becomes a no-op on the next tick, and
    the current attempt's ``complete()`` / ``fail()`` /
    ``retry()`` will raise ``JobNotFoundError`` (which the
    worker already catches and logs as "job disappeared; done").
    """
    if not isinstance(job_id, str) or not job_id.strip():
        _raise_400(
            error="invalid_job_id",
            message="job_id must be a non-empty string",
        )
    clean_id = job_id.strip()

    outbox = _get_outbox_or_none()
    if outbox is None:
        return _extraction_unavailable_response(operation="job_cancel")

    try:
        cancelled = await outbox.cancel_if_active(clean_id)
    except JobNotFoundError:
        return _not_found_response(
            operation="job_cancel", job_id=clean_id,
        )
    except ValueError as exc:
        return _state_conflict_response(
            operation="job_cancel",
            job_id=clean_id,
            message=str(exc),
        )
    except Exception as exc:
        return _error_response(exc, operation="job_cancel")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "job_cancel",
            "job": _job_to_dict(cancelled),
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
