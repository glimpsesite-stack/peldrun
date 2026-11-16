# backend/omweb/memory/api/memory_batch.py
"""
PELDRUN Memory Platform - Batch Extraction HTTP Router.

The user-facing surface for retroactive batch extraction. Powers
the "Batch Extraction" page in the Phase 3 UI: creating a batch,
watching its progress, pausing / resuming / cancelling it, and
estimating the cost of a fresh run BEFORE any job is enqueued.

Endpoints
---------

    POST /batch                          -- create
    GET  /batch                          -- list (paginated)
    GET  /batch/estimate                 -- cost estimate (PRE-batch)
    GET  /batch/{batch_id}               -- get + live progress
    POST /batch/{batch_id}/pause         -- pause running batch
    POST /batch/{batch_id}/resume        -- resume paused batch
    POST /batch/{batch_id}/cancel        -- cancel active batch

Route-ordering contract (LOAD-BEARING)
--------------------------------------

``GET /batch/estimate`` MUST be declared BEFORE
``GET /batch/{batch_id}``. FastAPI matches routes in REGISTRATION
ORDER, not by specificity -- so a `{batch_id}` catch-all declared
first would match ``batch_id="estimate"`` and route the estimate
request to the get-by-id handler. The consequence would be a
confusing 404 (``batch_estimate_does_not_exist``) on a valid
estimate request.

This same ordering rule already governs ``memory_rtr.py``'s
top-level routing (settings / projects / import / export / usage
BEFORE the CRUD catch-all). See that file's "Route ordering --
LOAD-BEARING" comment for the canonical explanation.

Availability contract
---------------------

Every endpoint 503s with ``error="extraction_unavailable"`` when
``MemoryService.batch_service`` is None. That happens when:

  * AI extraction is disabled in settings,
  * the memory mode forbids writes,
  * the provider is not SQLite,
  * extraction initialization failed.

The check happens at the service boundary BEFORE any business
logic runs, so a disabled subsystem never produces spurious
side effects (no batch row is created, no job is enqueued).

Response envelope
-----------------

All success responses use the same ``{status: "ok",
operation: ...}`` envelope as the rest of the memory API.
Domain-specific fields (``batch``, ``progress``, ``batches``,
``estimate``) sit alongside those two keys.

Error mapping
-------------

    * ``BatchNotFoundError``             -> 404
      Body: ``{status: "error", operation, error: "batch_not_found",
                message, batch_id}``

    * ``BatchStateError``                -> 409
      Body: ``{status: "error", operation, error: "invalid_state",
                message, batch_id}``

    * ``BatchServiceUnavailableError``   -> 503
      Body: ``{status: "error", operation,
                error: "extraction_unavailable", message}``

    * Pydantic validation failure        -> 422 (default FastAPI)
      A future app-level exception handler (main.py) will
      normalize this to a 400 envelope; until then, validation
      failures use FastAPI's default shape. This is consistent
      with the rest of the memory API today.

    * Anything else                      -> 500
      Body: ``{status: "error", operation, error: <type>,
                message: "Internal error ..."}``

Pagination
----------

``GET /batch`` accepts ``limit`` (1..200, default 50) and
``offset`` (>= 0). ``total`` comes from
``MemoryBatchService.count_batches``; ``has_more`` is computed as
``offset + len(batches) < total``. As documented in the jobs
router, a concurrent create between the count and the page
fetch can make ``total`` briefly disagree -- acceptable for a UI
that refreshes periodically.

The self-heal-on-read behavior of ``MemoryBatchService`` means
every RUNNING batch in a page whose jobs are all terminal will be
transitioned to COMPLETED *during* the list call. The UI sees the
healed state without an extra round trip.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, status as http_status
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from omweb.memory.batch.models import (
    MemoryBatchStatus,
)
from omweb.memory.batch.service import (
    BatchNotFoundError,
    BatchServiceUnavailableError,
    BatchStateError,
)

from omweb.memory import get_memory_service


logger = logging.getLogger("omweb.memory.api.batch")


router = APIRouter()


# ---------------------------------------------------------------------------
# Accepted vocabularies
# ---------------------------------------------------------------------------


_ACCEPTED_STATUSES: frozenset = frozenset(
    {s.value for s in MemoryBatchStatus} | {"all"}
)

_DEFAULT_LIMIT: int = 50
_MAX_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _CreateBatchBody(BaseModel):
    """Body of ``POST /batch``.

    Every field is optional: the empty body is a valid request
    and triggers a "scan everything" batch. ``triggered_by``
    defaults to ``"manual"`` -- the UI sets it explicitly when
    the batch was triggered by a scheduler.
    """

    model_config = ConfigDict(extra="forbid")

    triggered_by: str = Field(
        default="manual",
        description=(
            "Who/what created the batch. 'manual' (default) "
            "for the UI button, 'auto_schedule' for the batch "
            "scheduler. Any other string is preserved verbatim "
            "for custom integrations."
        ),
    )
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional project scope. Stored as batch metadata; "
            "the current route_batch scans ALL projects "
            "regardless."
        ),
    )
    chat_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional explicit chat list. Stored as batch "
            "metadata; the current route_batch does not honor "
            "it. Use /extract/from-session for per-chat work."
        ),
    )
    date_from: Optional[float] = Field(
        default=None,
        description="Optional lower bound on session update time.",
    )
    date_to: Optional[float] = Field(
        default=None,
        description="Optional upper bound on session update time.",
    )
    options: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Free-form options bag for future extensions."
        ),
    )

    @field_validator("triggered_by", mode="after")
    @classmethod
    def _strip_triggered_by(cls, v: str) -> str:
        clean = v.strip() if isinstance(v, str) else ""
        return clean or "manual"

    @field_validator("project_id", mode="after")
    @classmethod
    def _strip_project_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        clean = v.strip()
        return clean or None

    @field_validator("chat_ids", mode="after")
    @classmethod
    def _clean_chat_ids(
        cls, v: Optional[List[str]],
    ) -> Optional[List[str]]:
        if v is None:
            return None
        cleaned = [
            c.strip() for c in v
            if isinstance(c, str) and c.strip()
        ]
        return cleaned or None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _raise_400(*, error: str, message: str) -> None:
    """Raise a structured 400 that matches the memory API envelope."""
    raise HTTPException(
        status_code=http_status.HTTP_400_BAD_REQUEST,
        detail={"error": error, "message": message},
    )


def _validate_status_query(
    value: Optional[str],
) -> Optional[str]:
    """Validate the ``status`` query param.

    Returns ``None`` for the ``"all"`` alias (no filter), else the
    canonical lowercase status string.
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
    return None if normalized == "all" else normalized


def _validate_pagination(
    limit: int, offset: int,
) -> tuple[int, int]:
    """Clamp limit and offset to their accepted ranges."""
    if not isinstance(limit, int) or not isinstance(offset, int):
        _raise_400(
            error="invalid_pagination",
            message="'limit' and 'offset' must be integers",
        )
    if offset < 0:
        _raise_400(
            error="invalid_offset",
            message="'offset' must be >= 0",
        )
    return max(1, min(limit, _MAX_LIMIT)), offset


def _clean_batch_id(value: str) -> str:
    """Validate a ``batch_id`` path param."""
    if not isinstance(value, str):
        _raise_400(
            error="invalid_batch_id",
            message="batch_id must be a non-empty string",
        )
    clean = value.strip()
    if not clean:
        _raise_400(
            error="invalid_batch_id",
            message="batch_id must be a non-empty string",
        )
    return clean


# ---------------------------------------------------------------------------
# Error responses
# ---------------------------------------------------------------------------


def _error_response(
    exc: BaseException,
    *,
    operation: str,
) -> JSONResponse:
    """Map an unexpected exception to a 500."""
    logger.exception(
        "Memory batch %s failed: %s", operation, exc,
    )
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": "Internal error during batch operation.",
        },
    )


def _not_found_response(
    *, operation: str, batch_id: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status.HTTP_404_NOT_FOUND,
        content={
            "status": "error",
            "operation": operation,
            "error": "batch_not_found",
            "message": f"No batch with id {batch_id!r}.",
            "batch_id": batch_id,
        },
    )


def _state_conflict_response(
    *, operation: str, batch_id: str, message: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=http_status.HTTP_409_CONFLICT,
        content={
            "status": "error",
            "operation": operation,
            "error": "invalid_state",
            "message": message,
            "batch_id": batch_id,
        },
    )


def _unavailable_response(*, operation: str) -> JSONResponse:
    return JSONResponse(
        status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "error",
            "operation": operation,
            "error": "extraction_unavailable",
            "message": (
                "AI extraction is not enabled. Enable it via "
                "PATCH /api/memory/automation/settings before "
                "using batch extraction."
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


def _get_batch_service_or_503(*, operation: str):
    """Return the batch service, or None when extraction is off.

    The caller must handle ``None`` by returning
    ``_unavailable_response(operation=...)``.
    """
    service = _get_service_or_500()
    return service.batch_service


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _serialize_batch_and_progress(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract ``batch`` and ``progress`` sub-dicts from a
    ``MemoryBatchService.get_progress`` result.

    The service returns ``{"batch": {...}, "progress": {...}}``;
    this helper keeps the API response shape stable if the
    service ever adds sibling keys.
    """
    return {
        "batch": payload.get("batch", {}),
        "progress": payload.get("progress", {}),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/batch",
    summary="Create a batch extraction run",
    response_model=None,
)
async def create_batch_endpoint(
    body: _CreateBatchBody,
) -> JSONResponse:
    """Create a batch and enqueue its jobs.

    The batch is created and its initial jobs are enqueued
    SYNCHRONOUSLY -- the response returns only after every job
    has been inserted into ``memory_jobs``. For a workspace with
    hundreds of sessions this can take a few seconds; the UI
    should show a spinner.

    When the workspace has nothing to extract (all sessions
    caught up), the batch is created with ``status="completed"``
    immediately. The response still carries the ``batch_id`` so
    the client can render a "nothing to do" state.

    Error semantics:

      * 503 when extraction is not running (checked BEFORE
        creating the batch row).
      * 503 when AI is enabled but no model is configured
        (``BatchServiceUnavailableError`` from the service).
      * 500 for any other failure -- the batch row is marked
        FAILED with ``error_message`` before the exception
        propagates.
    """
    batch_service = _get_batch_service_or_503(
        operation="batch_create"
    )
    if batch_service is None:
        return _unavailable_response(operation="batch_create")

    try:
        batch = await batch_service.create_batch(
            triggered_by=body.triggered_by,
            project_id=body.project_id,
            chat_ids=body.chat_ids,
            date_from=body.date_from,
            date_to=body.date_to,
            options=body.options,
        )
    except BatchServiceUnavailableError as exc:
        # Business-level gate (AI enabled but model missing).
        # Surfaces as 503 with the same envelope shape as the
        # "extraction not running" case -- the fix is the same
        # for the operator either way.
        logger.info(
            "batch_create: service unavailable: %s", exc,
        )
        return _unavailable_response(operation="batch_create")
    except Exception as exc:
        return _error_response(exc, operation="batch_create")

    # Enrich the response with live progress. This is a
    # second DB round trip, but the cost is tiny (aggregate
    # is one COUNT + one SUM over an indexed column), and
    # giving the UI both records in one response keeps the
    # create flow atomic from the client's perspective.
    try:
        progress = await batch_service.get_progress(batch.batch_id)
    except Exception as exc:
        # Creating succeeded; enrichment failed. Report the
        # creation as successful with an empty progress dict
        # rather than a 500 -- the batch is real and the UI
        # can poll for progress separately.
        logger.warning(
            "batch_create: progress enrichment failed for %s: %s",
            batch.batch_id, exc,
        )
        progress = {
            "batch": batch.to_dict(),
            "progress": {},
        }

    if progress is None:
        # Race: batch was deleted between create and get.
        # Practically impossible; return the batch alone.
        progress = {"batch": batch.to_dict(), "progress": {}}

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "batch_create",
            **_serialize_batch_and_progress(progress),
        },
    )


@router.get(
    "/batch",
    summary="List batch runs",
    response_model=None,
)
async def list_batches_endpoint(
    status: Optional[str] = Query(
        None,
        description=(
            "Filter by status: pending | running | paused | "
            "completed | cancelled | failed | all"
        ),
    ),
    limit: int = Query(
        _DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT,
        description="Page size (1..200)",
    ),
    offset: int = Query(
        0, ge=0, description="Page offset (>= 0)",
    ),
) -> JSONResponse:
    """Paginated list of batches, newest first.

    Self-healing behavior: any RUNNING batch in the page whose
    jobs are all terminal is transitioned to COMPLETED during
    the list call (via ``MemoryBatchService.list_batches``).
    The client always sees the fresh state.
    """
    clean_status = _validate_status_query(status)
    clean_limit, clean_offset = _validate_pagination(
        limit, offset
    )

    batch_service = _get_batch_service_or_503(
        operation="batch_list"
    )
    if batch_service is None:
        return _unavailable_response(operation="batch_list")

    try:
        status_enum = (
            MemoryBatchStatus(clean_status)
            if clean_status else None
        )
        batches = await batch_service.list_batches(
            status=status_enum,
            limit=clean_limit,
            offset=clean_offset,
        )
        total = await batch_service.count_batches(
            status=status_enum,
        )
    except Exception as exc:
        return _error_response(exc, operation="batch_list")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "batch_list",
            "batches": [b.to_dict() for b in batches],
            "total": int(total),
            "limit": clean_limit,
            "offset": clean_offset,
            "has_more": (
                clean_offset + len(batches) < int(total)
            ),
        },
    )


# NOTE: /batch/estimate MUST be declared BEFORE /batch/{batch_id}.
# See the module docstring's "Route-ordering contract".
@router.get(
    "/batch/estimate",
    summary="Estimate the cost of a fresh batch run",
    response_model=None,
)
async def estimate_batch_endpoint() -> JSONResponse:
    """Return a cost estimate for a fresh batch created NOW.

    Scans every session with pending turns (the same scan
    ``POST /batch`` would perform) and returns a heuristic
    estimate. No batch is created; no job is enqueued. This is a
    pure read for the "Start Batch" dialog's cost preview.

    The estimate is a conservative UPPER BOUND (see
    ``MemoryBatchService.estimate_cost`` and the heuristic
    constants in ``batch/service.py``). The actual cost is
    reported per-job in ``token_ledger`` after execution.

    An empty workspace (nothing to extract) returns zeros -- a
    well-formed response, not an error.
    """
    batch_service = _get_batch_service_or_503(
        operation="batch_estimate"
    )
    if batch_service is None:
        return _unavailable_response(operation="batch_estimate")

    try:
        estimate = await batch_service.estimate_cost()
    except Exception as exc:
        return _error_response(exc, operation="batch_estimate")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "batch_estimate",
            "estimate": dict(estimate),
        },
    )


@router.get(
    "/batch/{batch_id}",
    summary="Get a single batch with live progress",
    response_model=None,
)
async def get_batch_endpoint(batch_id: str) -> JSONResponse:
    """Return a batch and its live aggregate progress.

    The ``progress`` block is computed from ``memory_jobs WHERE
    batch_id = ?`` on every call -- it is the authoritative live
    view, not the (possibly stale) counters stored on the batch
    row. The batch row itself self-heals RUNNING -> COMPLETED
    when there is no pending work left.

    404 when the id is unknown.
    """
    clean_id = _clean_batch_id(batch_id)

    batch_service = _get_batch_service_or_503(
        operation="batch_get"
    )
    if batch_service is None:
        return _unavailable_response(operation="batch_get")

    try:
        progress = await batch_service.get_progress(clean_id)
    except Exception as exc:
        return _error_response(exc, operation="batch_get")

    if progress is None:
        return _not_found_response(
            operation="batch_get", batch_id=clean_id,
        )

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "batch_get",
            **_serialize_batch_and_progress(progress),
        },
    )


@router.post(
    "/batch/{batch_id}/pause",
    summary="Pause a running batch",
    response_model=None,
)
async def pause_batch_endpoint(batch_id: str) -> JSONResponse:
    """Pause a RUNNING batch.

    Cancels every ``pending`` / ``retry_pending`` job with
    ``error_code='BATCH_PAUSED'``. RUNNING jobs are left alone --
    the current attempt finishes naturally, and its success is
    recorded.

    Error semantics:

      * 404 when the batch is unknown,
      * 409 when the batch is not in ``running`` state,
      * 500 for unexpected failures.
    """
    clean_id = _clean_batch_id(batch_id)

    batch_service = _get_batch_service_or_503(
        operation="batch_pause"
    )
    if batch_service is None:
        return _unavailable_response(operation="batch_pause")

    try:
        await batch_service.pause_batch(clean_id)
        progress = await batch_service.get_progress(clean_id)
    except BatchNotFoundError:
        return _not_found_response(
            operation="batch_pause", batch_id=clean_id,
        )
    except BatchStateError as exc:
        return _state_conflict_response(
            operation="batch_pause",
            batch_id=clean_id,
            message=exc.reason,
        )
    except Exception as exc:
        return _error_response(exc, operation="batch_pause")

    if progress is None:
        return _not_found_response(
            operation="batch_pause", batch_id=clean_id,
        )

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "batch_pause",
            **_serialize_batch_and_progress(progress),
        },
    )


@router.post(
    "/batch/{batch_id}/resume",
    summary="Resume a paused batch",
    response_model=None,
)
async def resume_batch_endpoint(batch_id: str) -> JSONResponse:
    """Resume a PAUSED batch.

    Requeues every job whose ``error_code`` is ``BATCH_PAUSED``.
    Jobs cancelled for ANY OTHER reason (e.g. a manual cancel, or
    a ``SESSION_CHANGED`` failure) are left untouched -- the user
    must retry them individually via the jobs API. This is the
    "resume from where we paused" contract, not a
    "re-run-everything" button.

    Error semantics: identical to pause (404 / 409 / 500).
    """
    clean_id = _clean_batch_id(batch_id)

    batch_service = _get_batch_service_or_503(
        operation="batch_resume"
    )
    if batch_service is None:
        return _unavailable_response(operation="batch_resume")

    try:
        await batch_service.resume_batch(clean_id)
        progress = await batch_service.get_progress(clean_id)
    except BatchNotFoundError:
        return _not_found_response(
            operation="batch_resume", batch_id=clean_id,
        )
    except BatchStateError as exc:
        return _state_conflict_response(
            operation="batch_resume",
            batch_id=clean_id,
            message=exc.reason,
        )
    except Exception as exc:
        return _error_response(exc, operation="batch_resume")

    if progress is None:
        return _not_found_response(
            operation="batch_resume", batch_id=clean_id,
        )

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "batch_resume",
            **_serialize_batch_and_progress(progress),
        },
    )


@router.post(
    "/batch/{batch_id}/cancel",
    summary="Cancel an active batch",
    response_model=None,
)
async def cancel_batch_endpoint(batch_id: str) -> JSONResponse:
    """Cancel a PENDING / RUNNING / PAUSED batch.

    Cancels every non-terminal job (pending, retry_pending,
    running) with ``error_code='BATCH_CANCELLED'``. There is no
    graceful variant: the user asked for it to stop.

    A RUNNING job's worker will see the cancellation on its next
    heartbeat (``update_heartbeat`` becomes a no-op) and its
    subsequent ``complete()`` / ``fail()`` / ``retry()`` call
    will raise ``JobNotFoundError``, which the worker already
    handles gracefully.

    Error semantics: identical to pause (404 / 409 / 500).
    """
    clean_id = _clean_batch_id(batch_id)

    batch_service = _get_batch_service_or_503(
        operation="batch_cancel"
    )
    if batch_service is None:
        return _unavailable_response(operation="batch_cancel")

    try:
        await batch_service.cancel_batch(clean_id)
        progress = await batch_service.get_progress(clean_id)
    except BatchNotFoundError:
        return _not_found_response(
            operation="batch_cancel", batch_id=clean_id,
        )
    except BatchStateError as exc:
        return _state_conflict_response(
            operation="batch_cancel",
            batch_id=clean_id,
            message=exc.reason,
        )
    except Exception as exc:
        return _error_response(exc, operation="batch_cancel")

    if progress is None:
        return _not_found_response(
            operation="batch_cancel", batch_id=clean_id,
        )

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "batch_cancel",
            **_serialize_batch_and_progress(progress),
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
