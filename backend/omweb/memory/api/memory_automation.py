# backend/omweb/memory/api/memory_automation.py
"""
PELDRUN Memory Platform - AI Extraction Automation HTTP Router.

Focused HTTP surface for the AI extraction subsystem's OWN
configuration (``MemoryAISettings``), separated from the general
memory settings router. Powers the "Memory Automation" panel in
the UI: enabling / disabling extraction, selecting the extraction
model, and validating a model configuration BEFORE it is armed.

Why a separate router
---------------------

``memory_settings.py`` already covers the FULL ``MemorySettings``
surface (mode, provider, retrieval tuning, budget, and the ``ai``
sub-object via ``PATCH /api/memory/settings``). That is the
"advanced / full config" path. This router is the "guided
automation" path with three focused operations the UI needs:

    * a dedicated GET that returns ONLY the AI subtree (no
      mode / provider / retrieval noise),
    * a PATCH whose body shape is exactly ``MemoryAISettings``,
      so the frontend form maps 1:1 to the model,
    * a test-model endpoint that exercises the extraction
      gateway end-to-end against a chosen provider+model
      before the operator enables extraction.

The separation also keeps the test-model endpoint's lifecycle
(short per-call timeout, cheap probe prompt, no job creation)
out of ``memory_settings.py``, which is about persistence
semantics.

Extraction pipeline activation is unchanged
-------------------------------------------

Both this router and ``memory_settings.py`` route every successful
update through:

    service._settings_manager.update(**kwargs)
      -> persist + validate
    service.reload_settings()
      -> re-evaluate the extraction pipeline (rebuild if the
         AI enable state changed -- see
         ``MemoryService.reload_settings`` [PHASE 3 FIX])

That is the ONLY path that brings extraction up. This router
adds no side effects: enabling extraction here is the same as
enabling it via ``PATCH /api/memory/settings {"ai": {"enabled":
true, ...}}``. ``MemoryService.reload_settings`` handles the
"configured but not started" -> "started" transition atomically.

Endpoints
---------

    GET   /api/memory/automation/settings
        Return the current ``MemoryAISettings`` snapshot.

    PATCH /api/memory/automation/settings
        Partial update. Body shape: a partial ``MemoryAISettings``.
        Nested ``extraction`` / ``consolidation`` objects are
        deep-merged with the current values, so a caller sending
        only ``{extraction: {provider_id: "x"}}`` keeps every
        other extraction field.

    POST  /api/memory/automation/test-model
        Run a single cheap extraction-shaped call against a
        chosen provider+model. Returns latency, token counts,
        cost, and the parsed JSON. Does NOT enqueue a job and
        does NOT touch the session cursor.

PATCH semantics -- null is meaningful
-------------------------------------

Unlike ``PATCH /api/memory/settings`` (which treats ``null`` as
"leave unchanged"), this endpoint treats an EXPLICIT ``null`` as
a directive to SET the field to ``None``. This is required by
``batch_auto_schedule_hours``, the only nullable field on
``MemoryAISettings`` -- clearing it disables the auto-scheduler.
The distinction is encoded via ``model_dump(exclude_unset=True)``
(no ``exclude_none``), so:

  * field omitted          -> left unchanged,
  * field sent as null     -> set to None (if the field allows it),
  * field sent with value  -> set to that value.

Sending ``null`` for a non-nullable field (e.g. ``enabled``)
raises a Pydantic ``ValidationError`` which this router maps to
a structured 400.

Deep-merge, not shallow
-----------------------

``MemorySettingsManager.update`` merges kwargs shallowly.
Sending ``{"ai": {"extraction": {"provider_id": "x"}}}`` would
otherwise replace the ENTIRE ``extraction`` sub-object and
reset every other field to its default. This router fixes that
by deep-merging the patch into the CURRENT ``ai`` dict before
handing it to the manager -- same semantics as
``_deep_merge_ai`` in ``memory_settings.py``. The helper is
duplicated locally (not imported) so this router has no coupling
to another router's internals.

test-model isolation
--------------------

The test-model endpoint builds a fresh
``MemoryModelGateway(force_openai_compat=True, ...)`` per call
and drives it through ONE ``generate_structured`` round-trip
against a fixed JSON-producing probe prompt. It never:

    * reads or writes ``memory_jobs``,
    * touches the extraction cursor,
    * acquires the extraction worker's lease,
    * starts or stops the worker.

The gateway's cost tracker WILL record the token spend (via
``default_tracker``), which is intentional: the operator sees
the test cost in ``/api/telemetry`` alongside real extraction
cost, and there is no hidden spend.

Response shapes
---------------

GET / PATCH success::

    {
      "status": "ok",
      "operation": "ai_settings_get" | "ai_settings_patch",
      "ai": { ... full MemoryAISettings ... }
    }

PATCH additionally includes ``"applied_fields": [...]`` (the
top-level keys the caller explicitly sent).

test-model success (HTTP 200)::

    {
      "status": "ok",
      "operation": "test_model",
      "provider_id": "...",
      "model_id": "...",
      "latency_ms": 342,
      "parsed": {"ok": true},
      "raw_text_preview": "...",
      "input_tokens": 12,
      "output_tokens": 5,
      "cost_nano_usd": 0
    }

test-model transport / parse failure (HTTP 200 -- the HTTP
call succeeded; only the UNDERLYING model call failed)::

    {
      "status": "error",
      "operation": "test_model",
      "error": "LLMTransportError" | "LLMParseError",
      "provider_id": "...",
      "model_id": "...",
      "latency_ms": 5000,
      "message": "...",
      "raw_text_preview": "..."  # only for LLMParseError
    }

The HTTP layer always returns 200 for a successfully-executed
handler. The UI distinguishes "reachable" from "configured
correctly" via the ``status`` field, not the HTTP status code.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, status as http_status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from omweb.memory import get_memory_service


logger = logging.getLogger("omweb.memory.api.automation")


router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class _AISettingsPatch(BaseModel):
    """Body of ``PATCH /api/memory/automation/settings``.

    A partial ``MemoryAISettings``. See the module docstring for
    the null-semantics (explicit null = clear; omitted = leave
    unchanged). Nested ``extraction`` / ``consolidation`` objects
    are partial dicts that deep-merge with current state.
    """

    model_config = ConfigDict(extra="forbid")

    # --- toggles ---
    enabled: Optional[bool] = None
    extraction_enabled: Optional[bool] = None
    consolidation_enabled: Optional[bool] = None

    # --- trigger policy ---
    extraction_trigger: Optional[str] = Field(
        default=None,
        description="after_turn | after_session | both",
    )
    after_turn_batching: Optional[bool] = None

    # --- per-task LLM config (partial deep-merge) ---
    extraction: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Partial MemoryAIExtractionSettings for the "
            "extraction task. Deep-merged with the current "
            "values (fields not present are inherited)."
        ),
    )
    consolidation: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Partial MemoryAIExtractionSettings for the "
            "consolidation task. Same deep-merge semantics."
        ),
    )

    # --- size / confidence bounds ---
    max_messages_per_job: Optional[int] = Field(
        default=None, ge=1, le=200,
    )
    max_input_tokens: Optional[int] = Field(
        default=None, ge=512, le=128_000,
    )
    max_candidates_per_job: Optional[int] = Field(
        default=None, ge=1, le=100,
    )
    min_confidence: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
    )

    # --- cost ceiling ---
    max_cost_nano_usd_per_day: Optional[int] = Field(
        default=None, ge=0, le=10_000_000_000,
    )

    # --- batch scheduler ---
    batch_enabled: Optional[bool] = None
    batch_max_concurrent_jobs: Optional[int] = Field(
        default=None, ge=1, le=4,
    )
    batch_auto_schedule_hours: Optional[int] = Field(
        default=None, ge=1, le=168,
        description=(
            "Auto-schedule a batch every N hours. Send explicit "
            "null to DISABLE auto-scheduling (the field is "
            "nullable). Omitting the field leaves it unchanged."
        ),
    )

    # --- worker heartbeat ---
    snapshot_heartbeat_interval_seconds: Optional[float] = Field(
        default=None, ge=1.0, le=60.0,
    )

    def to_update_dict(self) -> Dict[str, Any]:
        """Return only the fields the caller EXPLICITLY sent.

        Uses ``model_dump(exclude_unset=True)`` -- NO
        ``exclude_none`` -- so an explicit ``null`` survives into
        the returned dict. This is required to CLEAR the nullable
        ``batch_auto_schedule_hours`` field. Omitted fields do
        not appear at all.
        """
        return self.model_dump(exclude_unset=True)


class _TestModelRequest(BaseModel):
    """Body of ``POST /api/memory/automation/test-model``.

    The endpoint probes ONE provider+model pair with a fixed
    cheap JSON-producing prompt. All other parameters
    (extra_body, temperature) are read from the CURRENT
    ``ai.extraction`` settings, so the test exercises the exact
    same wire configuration a real job would use.
    """

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(
        ...,
        min_length=1,
        description="Provider id from the model vault.",
    )
    model_id: str = Field(
        ...,
        min_length=1,
        description="Model id understood by the provider.",
    )
    timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=180,
        description=(
            "Wall-clock timeout for the probe call. Kept short "
            "by default (30s) so a misconfigured model does "
            "not block the UI for the full extraction timeout."
        ),
    )


# ---------------------------------------------------------------------------
# Error helpers (same envelope as memory_settings.py / usage)
# ---------------------------------------------------------------------------


def _error_response(
    exc: BaseException,
    *,
    operation: str,
) -> JSONResponse:
    """Map an automation-layer exception to an HTTP response."""
    logger.exception(
        "Memory automation %s failed: %s",
        operation,
        exc,
    )
    return JSONResponse(
        status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "operation": operation,
            "error": type(exc).__name__,
            "message": (
                "Internal error during AI automation operation."
            ),
        },
    )


def _get_service_or_500():
    """Return the ``MemoryService`` singleton or raise HTTP 500.

    The service is initialized by the FastAPI ``lifespan`` hook
    (P1.7). When a request lands before that hook has run, the
    exception is a startup ordering bug -- 500, not 503,
    because the fix is "fix the startup sequence", not "retry".
    """
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


def _pydantic_error_to_400(
    exc: BaseException,
) -> HTTPException:
    """Convert a Pydantic validation error into a structured 400."""
    try:
        errors = exc.errors()  # type: ignore[attr-defined]
    except Exception:
        errors = []

    fields: List[Dict[str, Any]] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        fields.append(
            {
                "loc": [str(x) for x in err.get("loc", [])],
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )

    return HTTPException(
        status_code=http_status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "invalid_settings",
            "message": "AI settings validation failed.",
            "fields": fields,
        },
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _deep_merge_ai(
    current_ai: Dict[str, Any],
    patch_ai: Dict[str, Any],
) -> Dict[str, Any]:
    """Deep-merge a partial AI settings patch into current state.

    Same semantics as ``memory_settings.py``'s helper of the
    same name. Duplicated here rather than imported because both
    files are routers -- coupling them via a private helper
    would make a future refactor of one silently break the
    other. The function is 15 lines of pure dict manipulation;
    the duplication cost is lower than the coupling cost.

    The nested ``extraction`` and ``consolidation`` dicts are
    merged field-by-field, so a caller who sends only
    ``{extraction: {provider_id: "x"}}`` keeps every other
    extraction field (model_id, max_output_tokens,
    timeout_seconds, extraction_extra_body, ...). All other
    top-level fields are scalars / booleans, so a simple
    ``dict.update()`` on the top level handles them correctly.

    Returns a NEW dict; neither ``current_ai`` nor ``patch_ai``
    is mutated.
    """
    merged = dict(current_ai)
    for key, value in patch_ai.items():
        if isinstance(value, dict) and isinstance(
            merged.get(key), dict
        ):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _ai_to_dict(ai_settings: Any) -> Dict[str, Any]:
    """Serialize a ``MemoryAISettings`` for a response body."""
    return ai_settings.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/settings",
    summary="Read AI extraction settings",
    response_model=None,
)
async def get_ai_settings() -> JSONResponse:
    """Return the current ``MemoryAISettings`` snapshot.

    Only the ``ai`` subtree is returned -- no mode / provider /
    retrieval tuning. The frontend renders the Memory Automation
    panel from this payload directly.
    """
    try:
        service = _get_service_or_500()
    except HTTPException:
        raise

    try:
        ai = service.settings.ai
    except Exception as exc:
        return _error_response(exc, operation="ai_settings_get")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "ai_settings_get",
            "ai": _ai_to_dict(ai),
        },
    )


@router.patch(
    "/settings",
    summary="Partially update AI extraction settings",
    response_model=None,
)
async def patch_ai_settings(body: _AISettingsPatch) -> JSONResponse:
    """Apply a partial update to ``MemoryAISettings``.

    Flow:

      1. Serialize the request to a partial dict via
         ``to_update_dict`` (explicit-null survives, omitted
         fields disappear).
      2. Deep-merge the nested ``extraction`` / ``consolidation``
         dicts onto the CURRENT ``ai`` object (see
         ``_deep_merge_ai``).
      3. Persist + revalidate via
         ``MemoryService._settings_manager.update(ai=merged)``.
         The manager holds the ONLY lock on the settings file,
         so concurrent PATCHes serialize cleanly.
      4. Hot-reload via ``MemoryService.reload_settings()`` --
         this is what starts / stops the extraction pipeline
         when the enable flags flip.

    Response carries the FULL new ``ai`` subtree -- not just
    the diff -- so the UI can replace its local snapshot
    atomically.
    """
    update_dict = body.to_update_dict()
    if not update_dict:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "empty_update",
                "message": (
                    "PATCH body must include at least one field. "
                    "Use GET to read the current AI settings."
                ),
            },
        )

    try:
        service = _get_service_or_500()
    except HTTPException:
        raise

    # ----- Deep-merge the AI patch into the current state -----
    try:
        current_ai = service.settings.ai.model_dump(mode="json")
    except Exception as exc:
        return _error_response(exc, operation="ai_settings_patch")

    merged_ai = _deep_merge_ai(current_ai, update_dict)

    # ----- Persist + validate -----
    #
    # ``_settings_manager.update`` validates the merged object
    # through ``MemorySettings.model_validate`` BEFORE any disk
    # I/O. A ValidationError there covers both the "null for a
    # non-nullable field" case and any MemoryAISettings-level
    # constraint (e.g. min_confidence > 1.0). The manager's
    # validation is the single source of truth -- no pre-
    # validation here.
    try:
        new_settings = await service._settings_manager.update(  # noqa: SLF001
            ai=merged_ai,
        )
    except Exception as exc:
        try:
            from pydantic import ValidationError

            if isinstance(exc, ValidationError):
                raise _pydantic_error_to_400(exc)
        except ImportError:
            pass
        return _error_response(exc, operation="ai_settings_patch")

    # ----- Hot-reload onto the running service -----
    #
    # If the enable flags flipped, ``reload_settings`` tears down
    # or rebuilds the extraction pipeline atomically. On a
    # provider failure the new settings file is on disk but
    # in-memory state is stale -- 503 so the operator knows to
    # retry.
    try:
        await service.reload_settings()
    except Exception as exc:
        logger.error(
            "AI settings persisted but reload failed: %s", exc,
        )
        return _error_response(exc, operation="ai_settings_patch")

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "ai_settings_patch",
            "applied_fields": sorted(update_dict.keys()),
            "ai": _ai_to_dict(new_settings.ai),
        },
    )


@router.post(
    "/test-model",
    summary="Probe a provider+model pair end-to-end",
    response_model=None,
)
async def test_model(body: _TestModelRequest) -> JSONResponse:
    """Run one cheap extraction-shaped call against a model.

    Purpose: let the operator confirm that a chosen
    provider+model is reachable, respects the extraction
    reasoning-disablement settings, and returns parseable JSON
    BEFORE flipping ``extraction_enabled``. Same gateway
    configuration as a real extraction job, so a passing test
    is a strong signal that the real pipeline will work.

    The test does NOT:

      * create a job,
      * touch the session cursor,
      * acquire the worker's lease,
      * start or stop the worker.

    It DOES record the invocation in ``token_ledger`` (via
    ``default_tracker.complete`` inside the gateway transport),
    so the cost of the test is visible in telemetry -- no hidden
    spend.

    See the module docstring for the full response shapes. The
    HTTP layer returns 200 for every successfully-executed
    handler; the ``status`` field distinguishes "reachable +
    parseable" from "reachable but not parseable" from
    "unreachable".
    """
    try:
        service = _get_service_or_500()
    except HTTPException:
        raise

    # Read the current extraction_extra_body so the test matches
    # the exact wire configuration a real job would use
    # (including any reasoning-disablement payload the operator
    # has configured).
    try:
        extraction_cfg = service.settings.ai.extraction
        extra_body = dict(
            getattr(extraction_cfg, "extraction_extra_body", {}) or {}
        )
    except Exception as exc:
        return _error_response(exc, operation="test_model")

    # Build a fresh gateway per call. No caching: the gateway is
    # stateless, and a fresh instance guarantees the test cannot
    # accidentally reuse a transport from a prior call.
    from omweb.memory.extraction.gateway import (
        LLMParseError,
        LLMTransportError,
        MemoryModelGateway,
    )

    gateway = MemoryModelGateway(
        force_openai_compat=True,
        extraction_extra_body=extra_body,
    )

    # Fixed probe prompt. Asks the model to emit valid JSON so
    # the ENTIRE gateway pipeline (transport + parse_llm_json)
    # is exercised -- not just network reachability. A model
    # that returns valid JSON for this passes the strongest
    # precondition a "test-model" check can offer without
    # actually running extraction.
    probe_user_prompt = (
        'Respond with only this exact JSON and nothing else: '
        '{"ok": true}'
    )
    probe_system_prompt = (
        "You are a test harness. Follow instructions exactly."
    )

    start_ts = time.monotonic()
    try:
        response = await gateway.generate_structured(
            system_prompt=probe_system_prompt,
            user_prompt=probe_user_prompt,
            provider_id=body.provider_id,
            model_id=body.model_id,
            max_output_tokens=50,
            timeout_seconds=body.timeout_seconds,
            connection_id="memory_automation_test",
        )
    except LLMParseError as exc:
        # MUST be caught BEFORE LLMTransportError (subclass).
        latency_ms = int((time.monotonic() - start_ts) * 1000)
        logger.info(
            "test-model parse failure: provider=%r model=%r "
            "latency=%dms raw=%r",
            body.provider_id,
            body.model_id,
            latency_ms,
            exc.raw_text[:200] if exc.raw_text else "",
        )
        return JSONResponse(
            status_code=http_status.HTTP_200_OK,
            content={
                "status": "error",
                "operation": "test_model",
                "error": "LLMParseError",
                "provider_id": body.provider_id,
                "model_id": body.model_id,
                "latency_ms": latency_ms,
                "message": str(exc),
                "raw_text_preview": exc.raw_text[:500],
            },
        )
    except LLMTransportError as exc:
        latency_ms = int((time.monotonic() - start_ts) * 1000)
        logger.info(
            "test-model transport failure: provider=%r model=%r "
            "latency=%dms err=%s",
            body.provider_id,
            body.model_id,
            latency_ms,
            exc,
        )
        return JSONResponse(
            status_code=http_status.HTTP_200_OK,
            content={
                "status": "error",
                "operation": "test_model",
                "error": "LLMTransportError",
                "provider_id": body.provider_id,
                "model_id": body.model_id,
                "latency_ms": latency_ms,
                "message": str(exc),
            },
        )
    except Exception as exc:
        logger.error(
            "test-model unexpected failure: provider=%r model=%r "
            "err=%s",
            body.provider_id,
            body.model_id,
            exc,
            exc_info=True,
        )
        return _error_response(exc, operation="test_model")

    latency_ms = int((time.monotonic() - start_ts) * 1000)
    logger.info(
        "test-model success: provider=%r model=%r latency=%dms "
        "input=%d output=%d cost=%d",
        body.provider_id,
        body.model_id,
        latency_ms,
        response.input_tokens,
        response.output_tokens,
        response.cost_nano_usd,
    )

    return JSONResponse(
        status_code=http_status.HTTP_200_OK,
        content={
            "status": "ok",
            "operation": "test_model",
            "provider_id": body.provider_id,
            "model_id": body.model_id,
            "latency_ms": latency_ms,
            "parsed": dict(response.parsed),
            "raw_text_preview": (response.raw_text or "")[:500],
            "input_tokens": int(response.input_tokens),
            "output_tokens": int(response.output_tokens),
            "cost_nano_usd": int(response.cost_nano_usd),
        },
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["router"]
