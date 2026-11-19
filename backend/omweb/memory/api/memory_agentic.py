# backend/omweb/memory/api/memory_agentic.py
"""
PELDRUN Memory Platform - Agentic Memory Search API.

Exposes Phase 6's agentic retrieval over HTTP:

    POST /api/memory/search/agentic

Why a SEPARATE endpoint (and a separate module)
-----------------------------------------------
``POST /api/memory/search`` is the UI's per-keystroke preview and
``MemoryService.retrieve`` is the dispatcher's per-turn hook. Both
must stay cheap. An agentic run costs one LLM call to rewrite the
question, up to ``agentic_max_hops`` gap analyses, and one synthesis
call -- several seconds and real money.

Folding that into the existing endpoint would put an opt-in,
expensive behaviour behind a URL that is called constantly. A
dedicated endpoint makes the cost explicit at the call site, lets
the UI put it behind its own button, and keeps the fast path's
behaviour untouched.

Response shape
--------------
Always HTTP 200 unless the request itself is malformed or the
service is not running. A DISABLED or FAILED agentic run is NOT an
error: the response carries ``is_available=false``, an ``error``
code, and the standard retrieval result under ``fallback``. The
caller therefore always receives the best answer available, which is
the same contract ``MemoryService.retrieve`` follows.

``search_mode`` in the response is:
  * ``"agentic"``            -- the pipeline ran to completion,
  * ``"agentic+fallback"``   -- the standard path answered instead.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from peldrun.memory.domain import MemoryEntry


logger = logging.getLogger("omweb.memory.api.agentic")

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class _AgenticSearchBody(BaseModel):
    """Request body for the agentic search endpoint.

    Scope is expressed the same way as ``/search``: either explicit
    ``scope_type``/``scope_id``, or the ambient ``chat_id`` /
    ``project_id`` / ``user_id`` which the service resolves into
    canonical scope refs.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "The complex question to answer. This endpoint is for "
            "questions whose resolution needs several searches "
            "('what did my manager say about the project?'), not "
            "for keyword lookups -- use /search for those."
        ),
    )
    chat_id: Optional[str] = Field(
        default=None,
        description="Current chat id, if any.",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="Current project id, if any.",
    )
    user_id: Optional[str] = Field(
        default=None,
        description=(
            "Explicit user id. Defaults to 'user:default' in "
            "single-user deployments."
        ),
    )
    # NOTE: no ``scope_type`` / ``scope_id`` pair here, on purpose.
    # ``retrieve_agentic`` resolves scope exactly like ``retrieve``:
    # ambient chat + project + user. Offering an arbitrary scope would
    # mean the agentic path could read a scope the caller's normal
    # retrieval cannot -- a privilege widening disguised as a
    # convenience parameter. An explicit cross-scope agentic search
    # must be added to BOTH paths together, with its own authorization
    # story.
    max_hops: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description=(
            "Per-request override for retrieval.agentic_max_hops. "
            "Lower it for latency-sensitive callers; do NOT raise it "
            "above the configured value without expecting a "
            "proportional cost increase."
        ),
    )
    include_evidence: bool = Field(
        default=True,
        description=(
            "When false, ``evidence`` is returned as an empty list. "
            "Useful for a UI that only wants the synthesized answer "
            "and keeps the payload small."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_service():
    """Return the live MemoryService or raise 503.

    Lazy import: keeps this module importable without triggering the
    memory package's singletons (which read the filesystem at import
    time in some deployments).
    """
    from omweb.memory import get_memory_service

    service = get_memory_service()
    if service is None or not service.is_available:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Memory service is not available. Check the server "
                "startup log for memory initialization errors."
            ),
        )
    return service


def _entry_to_dict(entry: MemoryEntry, score: Optional[float]) -> Dict[str, Any]:
    """Serialize one evidence entry for the response.

    Deliberately compact: the UI needs the text, where it came from,
    and how relevant it was. Full entry serialization lives in
    ``memory_crud`` for the CRUD endpoints.
    """
    payload: Dict[str, Any] = {
        "memory_id": entry.memory_id,
        "content": entry.content,
        "memory_type": entry.memory_type.value,
        "scope_type": entry.scope_type.value,
        "scope_id": entry.scope_id,
        "status": entry.status.value,
        "importance": entry.importance,
        "created_at": (
            entry.created_at.isoformat()
            if entry.created_at is not None
            else None
        ),
    }
    if score is not None:
        payload["score"] = round(float(score), 6)
    return payload


def _error_response(
    exc: BaseException, *, operation: str
) -> JSONResponse:
    """Map an unexpected exception to a structured 500."""
    logger.error(
        "memory_agentic.%s failed: %s", operation, exc, exc_info=True
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": str(exc),
        },
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/search/agentic",
    summary="Agentic memory search (synthesized answer + evidence)",
    response_model=None,
)
async def search_agentic_endpoint(
    body: _AgenticSearchBody,
) -> JSONResponse:
    """Run agentic retrieval and return a synthesized answer.

    Pipeline: rewrite the question into sub-queries, retrieve
    (graph-aware when enabled), analyse gaps, hop again if useful,
    then synthesize an answer grounded in the collected evidence.

    The response ALWAYS includes either a synthesized answer with its
    evidence, or the standard retrieval result under ``fallback`` --
    never an error page for a disablement or a model outage.
    """
    explicit_refs = None

    try:
        service = _get_service()
        result = await service.retrieve_agentic(
            query=body.query,
            chat_id=body.chat_id,
            project_id=body.project_id,
            user_id=body.user_id,
            max_hops=body.max_hops,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 -- structured 500
        return _error_response(exc, operation="search/agentic")

    evidence: List[Dict[str, Any]] = []
    if body.include_evidence:
        evidence = [
            _entry_to_dict(entry, score)
            for entry, score in result.evidence
        ]

    answer = result.synthesized_answer
    fallback: Optional[Dict[str, Any]] = None
    if result.fallback_context is not None:
        fallback = {
            "search_mode": service.last_search_mode,
            "memories": [
                _entry_to_dict(entry, None)
                for entry in result.fallback_context.memories
            ],
            "is_empty": result.fallback_context.is_empty,
        }

    content: Dict[str, Any] = {
        "status": "ok",
        "operation": "search/agentic",
        # ``ok`` mirrors ``status`` for callers that only check a
        # boolean. It is True even on the fallback path: the request
        # succeeded, it just got the cheaper answer.
        "ok": True,
        "search_mode": result.search_mode,
        "is_available": result.is_available,
        "error": result.error,
        "query": result.query,
        "synthesized_answer": answer.answer if answer else "",
        "confidence": result.confidence,
        "confidence_threshold_met": (
            result.confidence_threshold_met
        ),
        "citations": list(answer.citations) if answer else [],
        "hops_used": result.hops_used,
        "sub_queries_used": list(result.sub_queries_used),
        "evidence": evidence,
        "evidence_count": len(result.evidence),
        "elapsed_ms": result.elapsed_ms,
        "fallback": fallback,
    }
    return JSONResponse(status_code=200, content=content)


@router.get(
    "/search/agentic/status",
    summary="Agentic search availability",
    response_model=None,
)
async def agentic_status_endpoint() -> JSONResponse:
    """Report whether agentic retrieval is configured and enabled.

    Lets the UI decide whether to show the "deep search" affordance
    at all, instead of offering a button that always falls back.
    Read-only and cheap: no LLM call, no search.
    """
    try:
        service = _get_service()
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "status": "error",
                "operation": "search/agentic/status",
                "available": False,
                "message": exc.detail,
            },
        )

    try:
        settings = service.settings
        retrieval = settings.retrieval
        ai_settings = getattr(settings, "ai", None)
        enabled = bool(
            getattr(retrieval, "agentic_enabled", False)
        )
        configured = bool(service.agentic_enabled)
        # Separate the TWO independent reasons the pipeline can be
        # missing. Without this, a UI can only say "no model
        # resolved", which is wrong when `ai.enabled` is off -- and
        # sends the operator to the wrong screen.
        #
        # `_resolve_agentic_model` is a private helper on the
        # service; the settings router already reaches into
        # ``service._settings_manager`` for the same kind of
        # read-only introspection, so this follows that precedent.
        ai_enabled = bool(
            ai_settings is not None
            and getattr(ai_settings, "enabled", False)
        )
        try:
            model_resolved = (
                service._resolve_agentic_model() is not None  # noqa: SLF001
            )
        except Exception:
            model_resolved = False

        payload = {
            "status": "ok",
            "operation": "search/agentic/status",
            "enabled": enabled,
            "configured": configured,
            "available": enabled and configured,
            "ai_enabled": ai_enabled,
            "model_resolved": model_resolved,
            "max_hops": int(
                getattr(retrieval, "agentic_max_hops", 3) or 3
            ),
            "timeout_seconds": float(
                getattr(
                    retrieval, "agentic_timeout_seconds", 30.0
                )
                or 30.0
            ),
            "confidence_threshold": float(
                getattr(
                    retrieval,
                    "agentic_confidence_threshold",
                    0.6,
                )
                or 0.0
            ),
            "graph_search_enabled": bool(
                getattr(retrieval, "graph_search_enabled", False)
            ),
        }
    except Exception as exc:  # noqa: BLE001 -- never 500 on status
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "operation": "search/agentic/status",
                "available": False,
                "configuration_error": str(exc),
            },
        )

    return JSONResponse(status_code=200, content=payload)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
