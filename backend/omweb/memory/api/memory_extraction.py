# backend/omweb/memory/api/memory_extraction.py
"""
PELDRUN Memory Platform - Manual Extraction HTTP Router.

Two endpoints for user-triggered ("manual") extraction:

    POST /extract/from-session
        Extract the ENTIRE turn history of one chat.

    POST /extract/range
        Extract a SPECIFIC turn range from one chat.

Both endpoints are MANUAL-triggered. ``ExtractionTrigger.MANUAL``
is used for both because:

  * AFTER_TURN / AFTER_SESSION are automatically fired by the
    runtime (dispatcher / status watcher) -- letting a user
    impersonate them via HTTP would corrupt the trigger audit
    trail and confuse the "why did this job run?" answer in the
    jobs UI.

  * BATCH is reserved for the batch subsystem (M6), which sets
    ``batch_id`` on every job it spawns. A user calling this
    endpoint does not want a batch linkage; they want a
    one-shot job they can watch individually.

Semantics per endpoint
----------------------

**from-session**: calls ``MemoryService.enqueue_extraction_from_session``
with ``trigger_type=MANUAL``. The trigger router's
``route_on_demand`` path runs the WHOLE session (0 .. total-1),
re-extracting even turns that were already extracted (that is
the point of manual "extract everything from this chat"). The
idempotency key includes the snapshot hash, so two clicks in a
row produce one job; a second click after the first completes
produces a second job IF the session changed, or 200 with
``job: null`` if it did not.

**range**: calls ``MemoryService.enqueue_extraction`` with
``trigger_type=MANUAL``. The trigger router's ``route_explicit``
path uses the caller's [start..end] literally, clamped to the
session's actual turn count (a caller-supplied ``end`` beyond
``total-1`` is clamped DOWN, not rejected -- a caller guessing
"probably 100 turns" should succeed with whatever exists).

"Job: null" is NOT an error
---------------------------

Both endpoints can return HTTP 200 with ``job: null``. This
happens when:

  * the session is empty (0 turns),
  * the session file is missing / unreadable,
  * the proposed job is a duplicate (same idempotency key as an
    existing job),
  * the snapshot hash cannot be computed.

None of these are HTTP errors -- the operator's INTENT
(extract) was honored; the system simply had nothing to do.
The response carries a human-readable ``message`` field
explaining the likely cause. Clients treat 200+null as
"nothing to extract" and show the message; a 4xx/5xx is
reserved for genuine request/backend failures.

When the ENTIRE extraction pipeline is not running (AI
extraction disabled in settings, memory mode not writable, or
provider not SQLite), every endpoint returns 503 with
``error="extraction_unavailable"``. This is different from
"job: null": 503 means "the subsystem cannot do this at all";
job:null means "the subsystem ran and found nothing to do".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, status as http_status
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from peldrun.memory.domain.enums import ExtractionTrigger

from omweb.memory import get_memory_service


logger = logging.getLogger("omweb.memory.api.extraction")


router = APIRouter()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_MAX_TURN_INDEX: int = 10_000
"""Upper bound on ``start_turn_index`` / ``end_turn_index``.

Real chats have far fewer than 10,000 turns; the cap exists to
catch typos like ``end_turn_index=1000000`` before the request
reaches the scanner (which would clamp silently, masking the
caller's bug). A turn index that large almost always indicates
a client-side error, not a legitimate request.
"""


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _ExtractFromSessionBody(BaseModel):
    """Body of ``POST /extract/from-session``."""

    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(
        ...,
        min_length=1,
        description="Chat identifier to extract from.",
    )
    project_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional project override. When None, the project "
            "id is derived from the session file itself. The "
            "override exists for callers whose session file may "
            "have moved between projects."
        ),
    )

    @field_validator("chat_id", mode="after")
    @classmethod
    def _strip_chat_id(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError(
                "chat_id must not be empty or whitespace-only"
            )
        return clean

    @field_validator("project_id", mode="after")
    @classmethod
    def _strip_project_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        clean = v.strip()
        return clean or None


class _ExtractRangeBody(BaseModel):
    """Body of ``POST /extract/range``."""

    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(
        ...,
        min_length=1,
        description="Chat identifier to extract from.",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="Optional project override (see from-session).",
    )
    start_turn_index: int = Field(
        ...,
        ge=0,
        le=_MAX_TURN_INDEX,
        description="Inclusive start of the turn range (0-based).",
    )
    end_turn_index: int = Field(
        ...,
        ge=0,
        le=_MAX_TURN_INDEX,
        description="Inclusive end of the turn range (0-based).",
    )

    @field_validator("chat_id", mode="after")
    @classmethod
    def _strip_chat_id(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError(
                "chat_id must not be empty or whitespace-only"
            )
        return clean

    @field_validator("project_id", mode="after")
    @classmethod
    def _strip_project_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        clean = v.strip()
        return clean or None

    @model_validator(mode="after")
    def _check_range_order(self) -> "_ExtractRangeBody":
        """Reject ``start > end`` at validation time, not at enqueue."""
        if self.start_turn_index > self.end_turn_index:
            raise ValueError(
                f"start_turn_index ({self.start_turn_index}) must "
                f"be <= end_turn_index ({self.end_turn_index})"
            )
        return self


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------


def _error_response(
    exc: BaseException,
    *,
    operation: str,
) -> JSONResponse:
    """Map an extraction-layer exception to an HTTP response."""
    logger.exception(
        "Memory extraction %s failed: %s", operation, exc,
    )
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": (
                "Internal error during manual extraction."
            ),
        },
    )


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
                "starting a manual extraction."
            ),
        },
    )


def _no_job_message(*, operation: str) -> str:
    """Human-readable reason for ``job: null`` responses.

    Shared between both endpoints so the two responses cannot
    drift. The message intentionally lists every possible cause
    because the underlying service collapses them all into
    ``None`` (see ``TriggerRouter``'s route docstrings).
    """
    if operation == "extract_from_session":
        return (
            "No extraction job was created. Possible causes: "
            "the chat has zero turns, the session file is "
            "missing or unreadable, or an identical job already "
            "exists (same chat / trigger / range / snapshot)."
        )
    return (
        "No extraction job was created. Possible causes: the "
        "requested range is outside the session's actual turn "
        "count (clamped to empty), the session file is missing "
        "or unreadable, or an identical job already exists."
    )


def _job_to_dict(job: Any) -> Dict[str, Any]:
    """Serialize a ``MemoryJob`` via its canonical ``to_dict``."""
    return job.to_dict()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/extract/from-session",
    summary="Extract the whole session via a MANUAL job",
    response_model=None,
)
async def extract_from_session_endpoint(
    body: _ExtractFromSessionBody,
) -> JSONResponse:
    """Enqueue a MANUAL extraction job covering the whole session.

    Powers the "Extract from this chat" button. See the module
    docstring for the 200+null contract and the idempotency
    guarantee (a double-click produces one job).
    """
    service = _get_service_or_500()

    if not service.extraction_enabled:
        return _extraction_unavailable_response(
            operation="extract_from_session"
        )

    try:
        job = await service.enqueue_extraction_from_session(
            trigger_type=ExtractionTrigger.MANUAL,
            chat_id=body.chat_id,
            project_id=body.project_id,
        )
    except Exception as exc:
        return _error_response(
            exc, operation="extract_from_session"
        )

    if job is None:
        return JSONResponse(
            status_code=http_status.HTTP_200_OK,
            content={
                "status": "ok",
                "operation": "extract_from_session",
                "chat_id": body.chat_id,
                "job": None,
                "message": _no_job_message(
                    operation="extract_from_session"
                ),
            },
        )

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "extract_from_session",
            "chat_id": body.chat_id,
            "job": _job_to_dict(job),
        },
    )


@router.post(
    "/extract/range",
    summary="Extract a specific turn range via a MANUAL job",
    response_model=None,
)
async def extract_range_endpoint(
    body: _ExtractRangeBody,
) -> JSONResponse:
    """Enqueue a MANUAL extraction job for a specific turn range.

    Useful for re-extracting a specific portion of a session
    (e.g. after editing an earlier turn) without paying for a
    full-session re-run. See the module docstring for the
    200+null contract and the clamping behavior (an ``end``
    beyond the session's actual turn count is clamped DOWN,
    not rejected).
    """
    service = _get_service_or_500()

    if not service.extraction_enabled:
        return _extraction_unavailable_response(
            operation="extract_range"
        )

    try:
        job = await service.enqueue_extraction(
            trigger_type=ExtractionTrigger.MANUAL,
            chat_id=body.chat_id,
            project_id=body.project_id,
            start_turn_index=body.start_turn_index,
            end_turn_index=body.end_turn_index,
        )
    except Exception as exc:
        return _error_response(exc, operation="extract_range")

    if job is None:
        return JSONResponse(
            status_code=http_status.HTTP_200_OK,
            content={
                "status": "ok",
                "operation": "extract_range",
                "chat_id": body.chat_id,
                "start_turn_index": body.start_turn_index,
                "end_turn_index": body.end_turn_index,
                "job": None,
                "message": _no_job_message(
                    operation="extract_range"
                ),
            },
        )

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "extract_range",
            "chat_id": body.chat_id,
            "start_turn_index": body.start_turn_index,
            "end_turn_index": body.end_turn_index,
            "job": _job_to_dict(job),
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
