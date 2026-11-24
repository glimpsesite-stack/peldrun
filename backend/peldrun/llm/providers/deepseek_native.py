# backend/peldrun/llm/providers/deepseek_native.py
"""
PELDRUN Native DeepSeek Provider Adapter.

Talks to DeepSeek's OpenAI-compatible Chat Completions API. The
adapter uses the ``openai`` SDK because DeepSeek's wire format is
byte-compatible with OpenAI for everything except reasoning.

Why a separate adapter (vs. routing through ``openai_compat``)
-------------------------------------------------------------
DeepSeek's reasoning semantics differ from OpenAI's:

  * **Parameter surface depends on the model family.**

        ``deepseek-v3.2*``, ``deepseek-v4*``, ``deepseek-flash*``
            -> support ``reasoning_effort`` ("low"|"high"|"max",
               "none" disables thinking). This adapter forwards the
               value via ``extra_body`` when the model family
               matches.

        ``deepseek-reasoner``, ``deepseek-r1*`` (legacy)
            -> always-on reasoning, no parameter, cannot disable.
               Retired 2026-07-24; kept as ``automatic`` for any
               self-hosted pin still serving this alias.

        ``deepseek-chat``, ``deepseek-v3``, ``deepseek-v2`` (legacy)
            -> no reasoning surface at all. Retired 2026-07-24;
               kept as ``unsupported`` for any self-hosted pin.

    The request_compiler emits ``reasoning_effort`` for any profile
    that declares a ReasoningStrategy, so this adapter must gate
    the field on the model family — a blind forward would 400 on
    legacy aliases, and a blind strip would defeat the UI ladder on
    V3.2/V4.

  * **Reasoning arrives as a first-class field.** DeepSeek returns
    the chain of thought in ``message.reasoning_content`` (non-
    streaming) and ``delta.reasoning_content`` (streaming). OpenAI
    hides the same data under ``model_extra.reasoning_content``.
    This adapter reads BOTH shapes defensively.

  * **No vision.** V3 and R1 are text-only. ``PROFILE.features.vision``
    is therefore False (unlike ``openai_native``).

Capability model (see ``get_reasoning_options``)
------------------------------------------------
The UI's reasoning selector is driven by the model ID, not by any
per-request parameter:

    deepseek-reasoner*, deepseek-r1*    -> mode="automatic"
    deepseek-chat*,   deepseek-v3*,
    deepseek-v2*,     deepseek-coder*   -> mode="none" (hidden)
    anything else                        -> None (defer to metadata)

Unknown models deliberately return None ("no opinion"). The config
router then consults ``models_metadata.json``. If that is also
silent, the widget hides (plan principle P5: uncertainty is
hidden, never fabricated).

See also
--------
    * ``backend/peldrun/llm/contracts.py``        — ProviderAdapter protocol
    * ``backend/peldrun/llm/request_compiler.py`` — compiled request
    * ``backend/peldrun/llm/errors.py``           — error taxonomy
    * ``backend/peldrun/llm/client.py``           — TokenUsage / ToolCall
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import openai
from openai import AsyncOpenAI

from peldrun.llm.client import (
    LLMResponse,
    TokenUsage,
    ToolCall,
    UsageSource,
)
from peldrun.llm.contracts import (
    AdapterFeatures,
    AdapterProfile,
    ConnectionContext,
    LLMStreamEvent,
    ReasoningCapabilityDescriptor,
    ReasoningStrategy,
    StreamEventType,
    ToolCallStyle,
)
from peldrun.llm.errors import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    classify_http_status,
    is_cancellation,
    refine_by_message,
)
from peldrun.llm.request import LLMRequest
from peldrun.llm.request_compiler import CompiledRequest, compile_request

logger = logging.getLogger("peldrun.llm.providers.deepseek_native")


# ---------------------------------------------------------------------------
# Adapter profile
# ---------------------------------------------------------------------------


_PROFILE = AdapterProfile(
    provider_id="deepseek_native",
    api_surface="chat_completions",
    features=AdapterFeatures(
        model_discovery=True,
        native_streaming=True,
        buffered_streaming=True,
        tool_calling=True,
        parallel_tool_calls=True,
        forced_tool_choice=True,
        vision=False,                 # DeepSeek V3/R1 are text-only
        audio=False,
        structured_output=True,       # JSON mode ("response_format")
        usage_reporting=True,
        reasoning=True,               # via reasoning_content field
        prompt_caching=True,          # automatic on the server side
    ),
    reasoning_strategies=[
        ReasoningStrategy.OPENAI_CHAT_COMPLETIONS,
    ],
    tool_call_style=ToolCallStyle.OPENAI_FUNCTION,
    supported_param_names=[],
    notes=(
        "Native DeepSeek adapter. OpenAI-compatible Chat Completions "
        "with first-class reasoning_content. Reasoning control "
        "surface depends on the model family: V3.2/V4/flash accept "
        "reasoning_effort (low/high/max, none disables); legacy "
        "aliases are automatic (reasoner) or unsupported (chat)."
    ),
    # Native-routing markers. The dispatcher matches these against the
    # lowercased base_url to decide whether to route to this adapter
    # or fall back to openai_compat (e.g. a user proxy).
    default_base_url_markers=["deepseek.com", "deepseek.ai"],
)


#: Default DeepSeek API endpoint (the OpenAI SDK appends
#: ``/chat/completions`` under whatever base_url we hand it, so the
#: canonical base MUST end with ``/v1``).
_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class DeepSeekNativeAdapter:
    """
    Native DeepSeek adapter. Conforms to ``ProviderAdapter``.

    Instantiated by ``ProviderFactory._instantiate_v1``:

        DeepSeekNativeAdapter(context=ProviderContext(...))
        # or
        DeepSeekNativeAdapter(connection=ConnectionContext(...), profile=...)
    """

    #: Adapter protocol generation.
    ADAPTER_STYLE = "v1"

    #: Contract version — required by the registry.
    API_CONTRACT = "peldrun.llm.provider.v1"

    #: Profile consumed by the factory.
    PROFILE = _PROFILE

    #: Provider identifier (used by capability resolution and diagnostics).
    provider_id = "deepseek_native"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        context: Any = None,
        *,
        connection: Optional[ConnectionContext] = None,
        profile: Optional[AdapterProfile] = None,
    ) -> None:
        # Support both constructor styles used by the factory.
        if context is not None:
            self._connection: ConnectionContext = context.connection
            self._profile: AdapterProfile = context.profile or _PROFILE
            self._credentials: Dict[str, str] = dict(context.credentials or {})
        else:
            if connection is None:
                raise TypeError(
                    "DeepSeekNativeAdapter requires either `context=` or "
                    "`connection=` (and optionally `profile=`)."
                )
            self._connection = connection
            self._profile = profile or _PROFILE
            self._credentials = (
                {"api_key": connection.api_key}
                if connection.api_key
                else {}
            )

        self.profile = self._profile

        # Resolve base_url: explicit connection wins, else DeepSeek default.
        # The OpenAI SDK appends /chat/completions to whatever base_url
        # it receives, so we MUST normalize the suffix to /v1. Users
        # routinely paste either form (with or without /v1), so accept
        # both and heal the value here.
        resolved_base_url = (
            self._connection.base_url or _DEFAULT_BASE_URL
        ).rstrip("/")
        if not resolved_base_url.endswith("/v1"):
            resolved_base_url = f"{resolved_base_url}/v1"

        api_key = (
            self._credentials.get("api_key")
            or self._connection.api_key
            or "EMPTY"
        )

        self._client = AsyncOpenAI(
            base_url=resolved_base_url,
            api_key=api_key,
            timeout=float(self._connection.timeout or 120.0),
        )

        logger.debug(
            "DeepSeekNativeAdapter ready: base_url=%s", resolved_base_url
        )

    # ------------------------------------------------------------------
    # Reasoning capability query
    # ------------------------------------------------------------------

    @classmethod
    def get_reasoning_options(
        cls,
        model_id: str,
        metadata_entry: Optional[Dict[str, Any]] = None,
    ) -> Optional[ReasoningCapabilityDescriptor]:
        """
        Return the reasoning control surface for a DeepSeek model.

        DeepSeek's reasoning surface varies by family (see the
        Thinking Mode guide). The model ID selects the behavior:

            deepseek-v3.2* / deepseek-v4*  -> EFFORT ladder
                Levels: none, low, high, max (per Thinking Mode guide)
                Default: high
                Forwarded to the API via ``reasoning_effort`` in
                ``extra_body`` (see _build_chat_completions_kwargs).

            deepseek-reasoner* / deepseek-r1*  -> ALWAYS thinks
                Retired 2026-07-24; kept for self-hosted pins.
                No parameter accepted; cannot be disabled.

            deepseek-chat*  / deepseek-v3*,
            deepseek-v2*    / deepseek-coder*  -> NEVER thinks
                Retired 2026-07-24; kept for self-hosted pins.
                No parameter accepted; reasoning unavailable.

            anything else                       -> no opinion

        This is adapter-owned family knowledge (plan principles
        P3/P7). The frontend never sees these strings; the config
        router calls this method and expands IDs into display
        objects.

        Parameters
        ----------
        model_id : str
            The model identifier as returned by ``/v1/models``.

        metadata_entry : dict, optional
            Discovery metadata from Scan Models. Accepted for
            contract symmetry; DeepSeek's family is authoritative
            here, so it is not read. A future provider variant that
            needs per-model reasoning control can override this
            method without changing the router.

        Returns
        -------
        ReasoningCapabilityDescriptor | None
            * ``automatic``      — always-on reasoning (reasoner family).
            * ``unsupported``    — no reasoning surface (chat family).
            * ``None``           — unknown model (defer to metadata).
        """
        norm = (model_id or "").strip().lower()
        if not norm:
            return None

        # ---- Reasoner family: ALWAYS thinks, no user control ------
        #
        # ``deepseek-reasoner`` is the production alias; ``deepseek-r1*``
        # covers self-hosted or pinned versions that surface the model
        # name directly. Both behave identically at the wire level.
        if norm.startswith(("deepseek-reasoner", "deepseek-r1")):
            return ReasoningCapabilityDescriptor(
                supported=True,
                mode="automatic",
                toggle_available=False,
                default_level=None,
                level_ids=(),
            )

        # ---- Newest generations (V3.2+ / V4+) ---------------------
        #
        # V3.2 / V4 / Flash share the same reasoning_effort surface
        # per DeepSeek's Thinking Mode guide. The adapter forwards
        # the value via extra_body["reasoning_effort"], and
        # "none" disables thinking. Default is "high".
        #
        # This MUST be checked BEFORE the `deepseek-v3` /
        # `deepseek-v2` catch-all below, because `deepseek-v3.2`
        # would otherwise be misclassified as non-reasoning.
        #
        # If you want user-selectable levels for these families,
        # do TWO things together: (1) enable pass-through of
        # `reasoning_effort` in `_build_chat_completions_kwargs`,
        # and (2) change this block to `mode="effort"` with the
        # appropriate ladder. Verify against the live API first —
        # an unverified `reasoning_effort` triggers HTTP 400 and
        # silently breaks every request to these models.
        # V3.2 / V4 expose reasoning_effort per DeepSeek's official
        # docs (guides/thinking_mode). Accepted values: low / high /
        # max, plus "none" which DISABLES thinking mode entirely.
        # The API also accepts "minimal" (mapped to low) and
        # "medium"/"xhigh" (mapped to high) for compatibility, but we
        # expose only the canonical ladder so the UI stays clean.
        # Default effort is "high" (docs, footnote 1).
        # V3.2 / V4 / Flash all share the same reasoning_effort
        # surface (Thinking Mode guide). "flash" is DeepSeek's
        # low-latency GA alias and accepts the same ladder.
        if norm.startswith(
            ("deepseek-v3.2", "deepseek-v4", "deepseek-flash")
        ):
            return ReasoningCapabilityDescriptor(
                supported=True,
                mode="effort",
                toggle_available=True,
                default_level="high",
                level_ids=("none", "low", "high", "max"),
            )

        # ---- Chat family: no reasoning at all ---------------------
        #
        # ``deepseek-chat`` is the production alias for the latest V3.x
        # non-reasoning checkpoint. The other prefixes cover legacy
        # versions and self-hosted deployments. All of them reject the
        # ``reasoning`` / ``thinking`` request fields with HTTP 400.
        if norm.startswith(
            ("deepseek-chat", "deepseek-v3", "deepseek-v2", "deepseek-coder")
        ):
            return ReasoningCapabilityDescriptor.unsupported()

        # ---- Unknown model: no opinion (P5) -----------------------
        #
        # A user's custom endpoint may expose models we do not know.
        # Returning None makes the router consult
        # ``models_metadata.json``. If that is also silent, the widget
        # hides (never fabricated support).
        return None

    # ------------------------------------------------------------------
    # ProviderAdapter: generate (non-streaming)
    # ------------------------------------------------------------------

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute a non-streaming completion."""
        started_at = time.time()
        compiled = compile_request(request, self._profile)

        try:
            kwargs = self._build_chat_completions_kwargs(compiled, request=request)
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise _map_deepseek_exception(
                exc, provider=self.provider_id, model=compiled.model
            ) from exc

        completed_at = time.time()
        result = self._convert_response(response, compiled.model)
        result.latency_ms = round(
            (completed_at - started_at) * 1000.0, 2
        )
        result.ttft_ms = result.latency_ms

        # Attach compile diagnostics for the Request Inspector.
        if result.raw is None:
            result.raw = {}
        if isinstance(result.raw, dict):
            result.raw.setdefault(
                "_peldrun_diagnostics", compiled.diagnostics
            )

        return result

    # ------------------------------------------------------------------
    # ProviderAdapter: stream
    # ------------------------------------------------------------------

    async def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[LLMStreamEvent]:
        """Execute a streaming completion."""
        compiled = compile_request(request, self._profile)

        try:
            kwargs = self._build_chat_completions_kwargs(compiled, request=request)
        except Exception as exc:
            yield _error_event(
                _map_deepseek_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=1,
            )
            return

        kwargs["stream"] = True
        # Request usage in the final chunk (OpenAI-compatible contract;
        # DeepSeek honors it).
        kwargs["stream_options"] = {"include_usage": True}

        seq = 0
        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            yield _error_event(
                _map_deepseek_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=seq + 1,
            )
            return

        # Track tool-call deltas by index so we can emit
        # TOOL_CALL_COMPLETE exactly once per tool.
        active_tools: Dict[int, Dict[str, Any]] = {}
        captured_usage: Optional[TokenUsage] = None
        finish_reason: Optional[str] = None

        # [P3-01] Terminal marker tracking.
        #
        # OpenAI-compatible streaming requires a terminal chunk with a
        # non-empty ``finish_reason``. If the loop exits without one,
        # the stream was truncated (network drop, provider crash,
        # mid-SSE cut). Emitting ``"stop"`` in that case silently
        # reports truncation as success, which corrupts downstream
        # retries (ToolCallAgent.think only retries on
        # ``finish_reason="incomplete"``).
        saw_finish_reason: bool = False

        try:
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    captured_usage = _convert_usage(chunk_usage)

                if not choices:
                    continue

                first = choices[0]
                delta = getattr(first, "delta", None)
                if delta is not None:
                    # ---- Content delta ------------------------------
                    content_piece = getattr(delta, "content", None)
                    if isinstance(content_piece, str) and content_piece:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.CONTENT_DELTA,
                            content_delta=content_piece,
                            sequence=seq,
                        )

                    # ---- Reasoning delta ----------------------------
                    #
                    # DeepSeek returns ``delta.reasoning_content`` as a
                    # first-class field on the SDK object. Older or
                    # proxied endpoints may tuck it under
                    # ``model_extra``. Check both, in that order.
                    reasoning_piece: Optional[str] = None
                    direct = getattr(delta, "reasoning_content", None)
                    if isinstance(direct, str) and direct:
                        reasoning_piece = direct
                    else:
                        extra = getattr(delta, "model_extra", None) or {}
                        if isinstance(extra, dict):
                            for key in (
                                "reasoning_content",
                                "reasoning",
                                "thinking",
                            ):
                                val = extra.get(key)
                                if isinstance(val, str) and val:
                                    reasoning_piece = val
                                    break
                    if reasoning_piece:
                        seq += 1
                        yield LLMStreamEvent(
                            type=StreamEventType.REASONING_DELTA,
                            reasoning_delta=reasoning_piece,
                            sequence=seq,
                        )

                    # ---- Tool call deltas ---------------------------
                    raw_tc_deltas = (
                        getattr(delta, "tool_calls", None) or []
                    )
                    for tc_delta in raw_tc_deltas:
                        tool_index = getattr(tc_delta, "index", 0) or 0
                        fn = getattr(tc_delta, "function", None)

                        if tool_index not in active_tools:
                            tool_id = (
                                getattr(tc_delta, "id", None)
                                or f"call_{tool_index}"
                            )
                            tool_name = (
                                (getattr(fn, "name", None) if fn else None)
                                or ""
                            )
                            active_tools[tool_index] = {
                                "id": tool_id,
                                "name": tool_name,
                                "arguments": "",
                            }
                            seq += 1
                            yield LLMStreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_id=tool_id,
                                tool_name=tool_name,
                                tool_index=tool_index,
                                sequence=seq,
                            )
                        else:
                            # Preserve late-arriving ID.
                            if getattr(tc_delta, "id", None):
                                active_tools[tool_index]["id"] = (
                                    tc_delta.id
                                )

                        if fn is not None:
                            args_piece = getattr(fn, "arguments", None)
                            if isinstance(args_piece, str) and args_piece:
                                active_tools[tool_index][
                                    "arguments"
                                ] += args_piece
                                seq += 1
                                yield LLMStreamEvent(
                                    type=StreamEventType.TOOL_CALL_DELTA,
                                    tool_call_id=active_tools[
                                        tool_index
                                    ]["id"],
                                    tool_name=active_tools[
                                        tool_index
                                    ]["name"],
                                    tool_index=tool_index,
                                    tool_arguments_delta=args_piece,
                                    sequence=seq,
                                )

                chunk_finish = getattr(first, "finish_reason", None)
                if chunk_finish:
                    finish_reason = chunk_finish
                    saw_finish_reason = True

        except asyncio.CancelledError:
            try:
                await stream.close()
            except Exception:
                pass
            raise
        except Exception as exc:
            yield _error_event(
                _map_deepseek_exception(
                    exc, provider=self.provider_id, model=compiled.model
                ),
                sequence=seq + 1,
            )
            return

        # ---- Finalize accumulated tool calls ---------------------
        for tool_idx in sorted(active_tools.keys()):
            tool = active_tools[tool_idx]
            parsed_args: Any = tool["arguments"]
            if isinstance(parsed_args, str) and parsed_args:
                try:
                    parsed_args = json.loads(parsed_args)
                except Exception:
                    parsed_args = {"raw": parsed_args}
            elif not parsed_args:
                parsed_args = {}
            seq += 1
            yield LLMStreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call_id=tool["id"],
                tool_name=tool["name"],
                tool_index=tool_idx,
                tool_arguments_complete=parsed_args,
                sequence=seq,
            )

        # ---- Usage event (if the provider sent one) --------------
        if captured_usage:
            seq += 1
            yield LLMStreamEvent(
                type=StreamEventType.USAGE_UPDATE,
                usage=captured_usage,
                sequence=seq,
            )

        # ---- Honest terminal marker ------------------------------
        if saw_finish_reason:
            _effective_finish_reason = finish_reason or "stop"
        else:
            _effective_finish_reason = "incomplete"
            logger.warning(
                "[P3-01] DeepSeek stream ended WITHOUT a terminal "
                "finish_reason (model=%s). Emitting "
                "finish_reason='incomplete' so consumers can retry "
                "with a fresh attempt.",
                compiled.model,
            )

        seq += 1
        yield LLMStreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=_effective_finish_reason,
            usage=captured_usage,
            sequence=seq,
        )

    # ------------------------------------------------------------------
    # ProviderAdapter: discover_models
    # ------------------------------------------------------------------

    async def discover_models(
        self, connection: Optional[ConnectionContext] = None
    ) -> List[Dict[str, Any]]:
        """
        Return DeepSeek's model catalog via ``GET /v1/models``.

        Response shape (OpenAI-compatible):
            {"object": "list",
             "data": [
               {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
               {"id": "deepseek-reasoner", "object": "model", "owned_by": "deepseek"}
             ]}

        DeepSeek exposes no per-model capability endpoint. Reasoning
        behavior is implied by the model ID (see
        ``get_reasoning_options``); the discovery layer preserves the
        raw list without synthesizing capabilities.
        """
        try:
            response = await self._client.models.list()
        except Exception as exc:
            logger.warning("DeepSeek model discovery failed: %s", exc)
            return []

        raw_items = getattr(response, "data", []) or []
        results: List[Dict[str, Any]] = []
        for item in raw_items:
            item_id = getattr(item, "id", None)
            if not isinstance(item_id, str) or not item_id:
                continue
            results.append(
                {
                    "id": item_id,
                    "object": getattr(item, "object", "model"),
                    "created": getattr(item, "created", None),
                    "owned_by": getattr(item, "owned_by", "deepseek"),
                }
            )
        return results

    # ------------------------------------------------------------------
    # ProviderAdapter: validate_connection
    # ------------------------------------------------------------------

    async def validate_connection(
        self, connection: Optional[ConnectionContext] = None
    ) -> Dict[str, Any]:
        """Ping ``/v1/models`` and report latency/status."""
        started_at = time.time()
        try:
            await self._client.models.list()
            latency_ms = round(
                (time.time() - started_at) * 1000.0, 2
            )
            return {
                "ok": True,
                "message": "DeepSeek API reachable.",
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            latency_ms = round(
                (time.time() - started_at) * 1000.0, 2
            )
            err = _map_deepseek_exception(
                exc, provider=self.provider_id, model=""
            )
            return {
                "ok": False,
                "message": err.message,
                "latency_ms": latency_ms,
                "code": err.code,
                "status_code": err.status_code,
            }

    # ------------------------------------------------------------------
    # ProviderAdapter: get_model_metadata
    # ------------------------------------------------------------------

    async def get_model_metadata(
        self, model_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        DeepSeek exposes no per-model metadata endpoint.
        Returns ``None``. Reasoning facts come from
        ``get_reasoning_options``; token limits come from the
        pricing store / metadata vault.
        """
        return None

    # ------------------------------------------------------------------
    # ProviderAdapter: close
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Release the underlying HTTP client. Idempotent."""
        try:
            await self._client.close()
        except Exception as exc:
            logger.debug("DeepSeek client close raised: %s", exc)

    # ==================================================================
    # Request building
    # ==================================================================

    def _build_chat_completions_kwargs(
        self,
        compiled: CompiledRequest,
        request: Optional[LLMRequest] = None,
    ) -> Dict[str, Any]:
        """
        Assemble the SDK kwargs for Chat Completions.

        Three DeepSeek-specific rules apply here:

        1. **reasoning_effort is forwarded via extra_body** for
           V3.2/V4/Flash models only. Legacy aliases (chat,
           reasoner, v2, v3) do NOT accept it — sending it there
           returns HTTP 400, so the adapter gates the forward on
           the model family.

        2. **tool_choice is flattened.** Same rationale as
           ``openai_native._build_chat_completions_kwargs``: the
           compiler emits the object form (``{"type": "auto"}``) for
           plugin compatibility; the OpenAI SDK / DeepSeek expect a
           plain string for the generic modes.

        3. **response_format is forwarded as-is.** DeepSeek supports
           JSON mode (``{"type": "json_object"}``); JSON schema is
           not documented. The compiler already gates this on the
           profile's ``structured_output=True`` flag.
        """
        messages: List[Dict[str, Any]] = []
        if compiled.system_prompt:
            messages.append(
                {"role": "system", "content": compiled.system_prompt}
            )
        messages.extend(compiled.messages)

        kwargs: Dict[str, Any] = {
            "model": compiled.model,
            "messages": messages,
        }

        logger.debug("DeepSeek request model=%r", compiled.model)
        logger.debug("DeepSeek compiled.reasoning=%r", compiled.reasoning)

        # ---- Generation params ------------------------------------
        for key in (
            "max_tokens",
            "temperature",
            "top_p",
            "stop",
            "seed",
            "frequency_penalty",
            "presence_penalty",
        ):
            if (
                key in compiled.generation
                and compiled.generation[key] is not None
            ):
                kwargs[key] = compiled.generation[key]

        # ---- Tools ------------------------------------------------
        if compiled.tools:
            kwargs["tools"] = compiled.tools
            if compiled.tool_choice:
                _tc = compiled.tool_choice
                if isinstance(_tc, dict) and _tc.get("type") in (
                    "auto",
                    "none",
                    "required",
                ):
                    kwargs["tool_choice"] = _tc["type"]
                else:
                    kwargs["tool_choice"] = _tc

        # ---- Response format -------------------------------------
        if compiled.response_format:
            kwargs["response_format"] = compiled.response_format

        # ---- Reasoning --------------------------------------------
        #
        # Per DeepSeek's official Thinking Mode guide
        # (api-docs.deepseek.com/guides/thinking_mode):
        #
        #   * ``reasoning_effort`` accepts "low" | "high" | "max".
        #     Compatibility aliases "minimal" -> low and
        #     "medium"/"xhigh" -> high are also accepted server-side,
        #     but the adapter forwards the canonical values emitted
        #     by the UI ladder ("none", "low", "high", "max").
        #
        #   * Setting ``reasoning_effort="none"`` DISABLES thinking
        #     mode entirely — this is the documented toggle.
        #
        #   * Default when the field is omitted is "high".
        #
        #   * The parameter is accepted ONLY on V3.2+ / V4+ families
        #     and the newer "flash" alias. Legacy aliases
        #     (``deepseek-chat`` / ``deepseek-reasoner``) were retired
        #     on 2026-07-24 and would reject it with HTTP 400.
        #
        # The gate below therefore:
        #   1. Only forwards the field for supported model families.
        #   2. Passes the value verbatim so "none" acts as the
        #      documented disable.
        #
        # The OpenAI SDK exposes ``reasoning_effort`` as an
        # ``extra_body`` field (same pattern as openai_native.py).
        # The OpenAI SDK exposes both `reasoning_effort` and `thinking`
        # as extra_body fields. Per DeepSeek's Thinking Mode guide:
        #
        #   * `thinking: {"type": "disabled"}` is the OFFICIAL way to
        #     disable reasoning. `reasoning_effort="none"` is NOT
        #     sufficient on its own — DeepSeek ignores an effort value
        #     when the `thinking` field is absent and falls back to
        #     the default "high". This was the root cause of the
        #     "user picks Off but the model still reasons" bug.
        #
        #   * `thinking: {"type": "enabled"}` + `reasoning_effort`
        #     activates the ladder with the requested level.
        #
        #   * The `thinking` object must be a DICT, not a string —
        #     DeepSeek rejects `"thinking": "disabled"`.
        #
        #   * When `effort == "none"`, we STILL include
        #     `reasoning_effort: "none"` for belt-and-suspenders:
        #     some DeepSeek gateways validate the field's presence
        #     even when `thinking` is disabled. Redundancy is safe
        #     and produces identical wire behavior on the official
        #     API.
        _norm_model = (compiled.model or "").strip().lower()
        _supports_effort = _norm_model.startswith(
            ("deepseek-v3.2", "deepseek-v4", "deepseek-flash")
        )
        if _supports_effort:
            # Always emit a thinking object for supported families.
            #
            # Missing thinking fields allow DeepSeek to fall back to
            # the default high effort, which breaks explicit Off.
            effort: Optional[str] = None
            if compiled.reasoning:
                raw_effort = compiled.reasoning.get("reasoning_effort")
                if isinstance(raw_effort, str) and raw_effort.strip():
                    effort = raw_effort.strip().lower()

            # Fallback: the compiler may drop reasoning when capability
            # metadata is UNKNOWN. For families known to support effort,
            # honor the original request intent instead of silently
            # disabling reasoning.
            if effort is None and request is not None:
                request_reasoning = getattr(request, "reasoning", None)
                if request_reasoning is not None and getattr(
                    request_reasoning, "enabled", False
                ):
                    raw_request_effort = getattr(
                        request_reasoning, "effort", None
                    )
                    if (
                        isinstance(raw_request_effort, str)
                        and raw_request_effort.strip()
                    ):
                        effort = raw_request_effort.strip().lower()
                    else:
                        effort = "high"

            extra_body = dict(kwargs.get("extra_body") or {})
            if effort and effort != "none":
                # Explicit level requested. Send thinking enabled plus
                # effort so the provider does not use its default.
                extra_body["thinking"] = {"type": "enabled"}
                extra_body["reasoning_effort"] = effort
            else:
                # Off or dropped reasoning. Send explicit disable plus
                # none effort for gateway compatibility.
                extra_body["thinking"] = {"type": "disabled"}
                extra_body["reasoning_effort"] = "none"
            kwargs["extra_body"] = extra_body
        # ---- Provider options (escape hatch) ---------------------
        if compiled.provider_options:
            kwargs.update(compiled.provider_options)

        logger.debug("DeepSeek final extra_body=%r", kwargs.get("extra_body"))

        return kwargs

    # ==================================================================
    # Response conversion (non-streaming)
    # ==================================================================

    def _convert_response(
        self, response: Any, model: str
    ) -> LLMResponse:
        """Convert a ChatCompletion response to ``LLMResponse``."""
        choices = getattr(response, "choices", None) or []
        if not choices:
            return LLMResponse(
                content="",
                model=getattr(response, "model", model),
                raw=response,
                usage=_convert_usage(getattr(response, "usage", None)),
                finish_reason=None,
            )

        first = choices[0]
        message = getattr(first, "message", None)
        content = (
            getattr(message, "content", None)
            if message is not None
            else None
        )
        # Some proxies return content as a list of parts.
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                if isinstance(part, dict)
                else str(part)
                for part in content
            )

        # ---- Reasoning extraction --------------------------------
        #
        # DeepSeek exposes the chain of thought at the TOP LEVEL of
        # ``message`` (``message.reasoning_content``), not under
        # ``model_extra``. Check both shapes so the adapter works
        # against DeepSeek proper AND any OpenAI-compatible proxy
        # that relays the same field.
        reasoning_text: Optional[str] = None
        if message is not None:
            direct = getattr(message, "reasoning_content", None)
            if isinstance(direct, str) and direct:
                reasoning_text = direct
            else:
                extra = getattr(message, "model_extra", None) or {}
                if isinstance(extra, dict):
                    for key in (
                        "reasoning_content",
                        "reasoning",
                        "thinking",
                    ):
                        val = extra.get(key)
                        if isinstance(val, str) and val:
                            reasoning_text = val
                            break

        # ---- Tool calls ------------------------------------------
        tool_calls = self._extract_tool_calls(message)

        finish_reason = getattr(first, "finish_reason", None)

        return LLMResponse(
            content=content or "",
            reasoning=reasoning_text,
            reasoning_content=reasoning_text,
            thought=reasoning_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_convert_usage(getattr(response, "usage", None)),
            model=getattr(response, "model", model),
            raw=response,
        )

    @staticmethod
    def _extract_tool_calls(message: Any) -> List[ToolCall]:
        """Convert ``message.tool_calls`` to ``ToolCall`` list."""
        if message is None:
            return []
        raw_calls = getattr(message, "tool_calls", None) or []
        result: List[ToolCall] = []
        for idx, tc in enumerate(raw_calls):
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            name = getattr(fn, "name", None) or ""
            raw_args = getattr(fn, "arguments", None)
            parsed_args: Any
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except Exception:
                    parsed_args = {"raw": raw_args}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}
            result.append(
                ToolCall(
                    id=getattr(tc, "id", None) or f"call_{idx}",
                    name=name,
                    arguments=parsed_args,
                    type="function",
                )
            )
        return result


# ---------------------------------------------------------------------------
# Usage conversion
# ---------------------------------------------------------------------------


def _convert_usage(raw_usage: Any) -> Optional[TokenUsage]:
    """
    DeepSeek / OpenAI-shaped usage → ``TokenUsage``.

    DeepSeek enriches the standard OpenAI usage object with caching
    fields:

        prompt_cache_hit_tokens   — tokens served from cache
        prompt_cache_miss_tokens  — tokens billed at full rate

    We surface the hit count as ``cached_input_tokens`` (the canonical
    field). The miss count has no equivalent in ``TokenUsage`` and is
    already reflected in ``input_tokens``.
    """
    if raw_usage is None:
        return None

    inp = getattr(raw_usage, "prompt_tokens", None)
    out = getattr(raw_usage, "completion_tokens", None)
    total = getattr(raw_usage, "total_tokens", None)

    # DeepSeek-specific caching breakdown.
    cache_hit = getattr(raw_usage, "prompt_cache_hit_tokens", None)

    # Reasoning-token count (present on reasoner responses).
    reasoning: Optional[int] = None
    comp_details = getattr(raw_usage, "completion_tokens_details", None)
    if comp_details is not None:
        reasoning = getattr(comp_details, "reasoning_tokens", None)

    # Also honor the OpenAI-shape ``prompt_tokens_details.cached_tokens``
    # in case a proxy normalizes the caching fields.
    if cache_hit is None:
        prompt_details = getattr(raw_usage, "prompt_tokens_details", None)
        if prompt_details is not None:
            cache_hit = getattr(prompt_details, "cached_tokens", None)

    if inp is None and out is None and total is None:
        return None

    return TokenUsage(
        input_tokens=int(inp) if inp is not None else None,
        output_tokens=int(out) if out is not None else None,
        total_tokens=int(total) if total is not None else (
            int(inp or 0) + int(out or 0)
        ),
        cached_input_tokens=(
            int(cache_hit) if cache_hit is not None else None
        ),
        reasoning_output_tokens=(
            int(reasoning) if reasoning is not None else None
        ),
        source=UsageSource.PROVIDER,
        estimated=False,
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _map_deepseek_exception(
    exc: BaseException, *, provider: str, model: str
) -> ProviderError:
    """
    Translate an SDK exception into the canonical taxonomy.

    DeepSeek uses the ``openai`` SDK, so the exception types are
    identical to ``openai_native._map_openai_exception``. The mapping
    is duplicated here (rather than imported) to keep this adapter
    self-contained — a change to OpenAI's exception surface must not
    silently break DeepSeek.
    """
    if is_cancellation(exc):
        # Caller should have caught this earlier; guard anyway.
        raise exc

    if isinstance(exc, openai.AuthenticationError):
        return ProviderAuthError(
            str(exc), provider=provider, model=model, status_code=401
        )
    if isinstance(exc, openai.PermissionDeniedError):
        return ProviderAuthError(
            str(exc), provider=provider, model=model, status_code=403
        )
    if isinstance(exc, openai.RateLimitError):
        retry_after: Optional[float] = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                hdr = resp.headers.get("retry-after")
            except Exception:
                hdr = None
            if hdr:
                try:
                    retry_after = float(hdr)
                except (TypeError, ValueError):
                    pass
        err = ProviderRateLimitError(
            str(exc),
            provider=provider,
            model=model,
            status_code=429,
            retry_after_seconds=retry_after,
        )
        return refine_by_message(err, str(exc))
    if isinstance(exc, openai.APITimeoutError):
        return ProviderTimeoutError(
            str(exc), provider=provider, model=model, phase="request"
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderConnectionError(
            str(exc), provider=provider, model=model
        )
    if isinstance(exc, openai.BadRequestError):
        status = getattr(exc, "status_code", 400) or 400
        err = classify_http_status(
            status, provider=provider, model=model, message=str(exc)
        )
        return refine_by_message(err, str(exc))
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 500) or 500
        err = classify_http_status(
            status, provider=provider, model=model, message=str(exc)
        )
        return refine_by_message(err, str(exc))
    if isinstance(exc, openai.APIError):
        return ProviderUnavailableError(
            str(exc), provider=provider, model=model
        )

    # Generic fallback.
    return ProviderError(
        f"{type(exc).__name__}: {exc}", provider=provider, model=model
    )


def _error_event(err: ProviderError, *, sequence: int) -> LLMStreamEvent:
    """Wrap a ``ProviderError`` in a ``PROVIDER_ERROR`` stream event."""
    return LLMStreamEvent(
        type=StreamEventType.PROVIDER_ERROR,
        error=err.to_dict(),
        sequence=sequence,
    )


__all__ = ["DeepSeekNativeAdapter"]
