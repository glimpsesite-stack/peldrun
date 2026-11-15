# backend/omweb/memory/extraction/gateway.py
"""
PELDRUN Memory Platform - Phase 1-B: LLM Gateway.

Bridge between the extraction orchestrator and a real LLM provider.
The gateway handles three concerns:

  1. **Transport** -- how to actually invoke the LLM (provider
     factory + credentials from the model vault).
  2. **Parsing** -- turning the LLM's raw text output into a
     Python dict, robust to markdown fences and stray prose.
  3. **Cost tracking** -- capturing the nano-USD cost that the
     provider reported so it can be recorded on the job.

Design invariants
-----------------

1. **Transport is injectable.**
   `MemoryModelGateway(transport=...)` accepts any callable with
   the same signature as `_default_transport`. This is what lets
   Phase 1-B tests exercise the gateway's full logic (prompt
   construction, response parsing, error mapping) WITHOUT a
   real network call or real credentials. Phase 1-C injects the
   production transport; test code injects a mock.

2. **JSON parsing is a pure function.**
   `parse_llm_json(raw)` takes a string and returns a dict, with
   no side effects. Every tolerance rule (markdown fences,
   stray prose, trailing whitespace) is tested in isolation.

3. **Provider-agnostic.**
   The gateway never imports `openai`, `anthropic`, or any other
   SDK directly. It calls a transport function that the caller
   supplies. Swapping providers means swapping transports, not
   editing this file.

4. **No retries in the gateway.**
   Retries are the WORKER's job (`MemoryExtractionWorker._handle_job_error`,
   which uses exponential backoff). The gateway raises a
   specific exception on failure and returns; the caller
   decides whether to retry. This keeps the gateway simple and
   makes retry behavior testable in exactly one place.

5. **Prompt content is the orchestrator's responsibility.**
   The gateway accepts `system_prompt` and `user_prompt` as
   plain strings. It does NOT build extraction prompts --
   `prompts.py` does, and the orchestrator assembles the
   per-job user prompt from the session turns. This separation
   means changing the prompt never touches the gateway.

Why `_default_transport` is currently a stub
--------------------------------------------

Wiring the real transport requires:
  * resolving credentials from the model vault,
  * resolving connection fields (base_url, api_key,
    extra_params) from the provider registry,
  * building a `ConnectionContext`, `LLMRequest`, and
    `GenerationConfig` in the shapes that
    `peldrun.llm.client.AsyncLLMClient` expects,
  * running the streaming call via `collect_response`,
  * reading the cost from `default_tracker`.

That is a substantial amount of integration code that belongs
to Phase 1-C (where the orchestrator is wired end-to-end). For
Phase 1-B, the stub raises `NotImplementedError` with a clear
message pointing to where the wiring lives. Every Phase 1-B
acceptance test (T5, T6) uses an injected mock transport; the
stub is intentionally unreachable in tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional


logger = logging.getLogger("omweb.memory.extraction.gateway")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMTransportError(Exception):
    """The transport failed to deliver an LLM response.

    Raised for network errors, timeouts, provider 4xx/5xx, and
    any other failure that occurred BEFORE the LLM produced
    output. Distinct from `LLMParseError` (the LLM responded, but
    the response could not be parsed).

    The worker's retry logic treats both as retryable; the
    distinction matters only for the decisions log and for the
    operator's diagnosis.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.cause = cause
        super().__init__(message)


class LLMParseError(LLMTransportError):
    """The LLM returned text that could not be parsed as a JSON object.

    Carries the raw text (truncated to 2000 chars) so the
    operator can see what the model actually returned. This is
    the single most useful diagnostic for prompt-tuning: if the
    model produces valid JSON for real sessions but fails on a
    specific edge case, the raw text tells you exactly how.

    Retryable: yes. A different sampling attempt often
    produces parseable output even when the first attempt did
    not (especially with temperature > 0).
    """

    def __init__(
        self,
        message: str,
        *,
        raw_text: str = "",
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        # Truncate to keep log lines bounded.
        self.raw_text = (raw_text or "")[:2000]
        super().__init__(
            message,
            provider_id=provider_id,
            model_id=model_id,
            cause=cause,
        )


# ---------------------------------------------------------------------------
# Response value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMStructuredResponse:
    """The complete output of one `generate_structured` call.

    Frozen so that the orchestrator cannot accidentally mutate
    the parsed dict mid-flight. If a downstream stage needs a
    modified copy, it uses `dict(response.parsed)` explicitly.

    Attributes
    ----------
    parsed
        The JSON object the LLM produced, already parsed.
    raw_text
        The raw text the LLM produced, BEFORE parsing. Kept for
        diagnostics; use `logger.debug` on it only (it may
        contain user content that should not appear in
        production logs at INFO level).
    cost_nano_usd
        The cost reported by the provider, in nano-USD.
        `0` when the provider did not report a cost (local
        models, mocked transports).
    input_tokens
        Prompt tokens consumed.
    output_tokens
        Completion tokens produced.
    """

    parsed: Dict[str, Any]
    raw_text: str
    cost_nano_usd: int
    input_tokens: int
    output_tokens: int


# ---------------------------------------------------------------------------
# JSON parsing (pure function)
# ---------------------------------------------------------------------------


# Fence patterns tried in order. The JSON-language fence is tried
# first because it is the most specific; the plain triple-backtick
# fence is the fallback for models that omit the language tag.
_JSON_FENCE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("```json", "```"),
    ("```JSON", "```"),
    ("```", "```"),
)


def parse_llm_json(raw: str) -> Dict[str, Any]:
    """Extract a JSON object from arbitrary LLM text output.

    Tolerates (in order of priority):

      1. Pure JSON: ``{"candidates": [...]}``.
      2. Fenced JSON: ```` ```json\n{...}\n``` ```` or
         ```` ```\n{...}\n``` ````.
      3. JSON embedded in prose: "Here is the result:
         `{...}`".

    Rejects (with `LLMParseError`):

      * empty or whitespace-only input,
      * a top-level value that is not an object (e.g. a bare
        array, a number, a string).

    Parameters
    ----------
    raw
        The exact text the LLM returned. Do NOT pre-trim; the
        function trims internally.

    Returns
    -------
    Dict[str, Any]
        The parsed JSON object.

    Raises
    ------
    LLMParseError
        When no valid JSON object can be extracted.
    """
    if not isinstance(raw, str):
        raise LLMParseError(
            f"LLM output must be a string, got {type(raw).__name__}",
            raw_text=repr(raw),
        )

    text = raw.strip()
    if not text:
        raise LLMParseError(
            "LLM returned empty response",
            raw_text="",
        )

    # --- 1. Direct parse ------------------------------------------------
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = None
    if isinstance(result, dict):
        return result
    if result is not None:
        # It parsed, but it was not an object (probably a bare array).
        raise LLMParseError(
            f"Expected JSON object, got {type(result).__name__}",
            raw_text=text,
        )

    # --- 2. Fenced JSON -------------------------------------------------
    for opener, closer in _JSON_FENCE_PATTERNS:
        if opener not in text:
            continue
        open_idx = text.index(opener)
        content_start = open_idx + len(opener)
        close_idx = text.find(closer, content_start)
        if close_idx < 0:
            continue
        inner = text[content_start:close_idx].strip()
        if not inner:
            continue
        try:
            result = json.loads(inner)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result

    # --- 3. First brace to last brace -----------------------------------
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError:
            result = None
        if isinstance(result, dict):
            return result

    raise LLMParseError(
        "Could not extract JSON object from LLM response "
        "(tried direct parse, fenced JSON, and brace slicing)",
        raw_text=text,
    )


# ---------------------------------------------------------------------------
# Transport type
# ---------------------------------------------------------------------------


# The transport callable signature is intentionally generic: every
# named argument the gateway forwards is declared as a keyword.
# Implementations can ignore kwargs they do not use. This is what
# lets Phase 1-C's production transport and Phase 1-B's test mocks
# coexist without a shared base class.
TransportFn = Callable[..., Awaitable[LLMStructuredResponse]]


# ---------------------------------------------------------------------------
# MemoryModelGateway
# ---------------------------------------------------------------------------


_DEFAULT_TIMEOUT_SECONDS: int = 45
_DEFAULT_MAX_OUTPUT_TOKENS: int = 1200


class MemoryModelGateway:
    """Provider-agnostic LLM gateway for the extraction pipeline.

    See the module docstring for the design rationale. In short:
    this class knows HOW to call an LLM and HOW to parse its
    output; it does NOT know WHICH LLM to call or HOW to
    authenticate.

    Usage::

        gateway = MemoryModelGateway()  # production (Phase 1-C)
        gateway = MemoryModelGateway(transport=my_mock)  # tests

        response = await gateway.generate_structured(
            system_prompt=prompts.build_extraction_system_prompt(
                max_candidates=20,
            ),
            user_prompt=rendered_conversation,
            provider_id="openai",
            model_id="gpt-4o-mini",
            api_key="sk-...",
            max_output_tokens=1200,
        )
        candidates = response.parsed.get("candidates", [])
    """

    def __init__(
        self,
        *,
        transport: Optional[TransportFn] = None,
    ) -> None:
        """
        Parameters
        ----------
        transport
            Optional callable that performs the actual LLM
            invocation. When ``None``, the gateway uses
            ``_default_transport`` (which, in Phase 1-B, raises
            ``NotImplementedError`` -- Phase 1-C will fill it
            in). Tests ALWAYS inject a transport.
        """
        self._transport = transport

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        provider_id: str,
        model_id: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        connection_id: str = "memory_extraction",
    ) -> LLMStructuredResponse:
        """Invoke the LLM and return its parsed JSON response.

        Parameters
        ----------
        system_prompt
            The system message (from ``prompts.build_extraction_system_prompt``).
        user_prompt
            The user message -- the conversation excerpt to extract from.
        provider_id
            Provider id (e.g. ``"openai"``).
        model_id
            Model id (e.g. ``"gpt-4o-mini"``).
        api_key
            Optional override. When ``None``, the transport
            resolves it from the model vault.
        base_url
            Optional override for the provider endpoint.
        max_output_tokens
            Hard cap on completion tokens. Defaults to 1200 --
            high enough for ~20 candidates, low enough to keep
            cost bounded per job.
        timeout_seconds
            Wall-clock timeout for the entire call. Phase 1-C's
            transport enforces this via ``asyncio.wait_for``.
        connection_id
            Diagnostic identifier for logs and cost attribution.

        Returns
        -------
        LLMStructuredResponse
            The parsed object plus cost and token metadata.

        Raises
        ------
        LLMTransportError
            The transport could not deliver a response (network,
            provider 4xx/5xx, timeout).
        LLMParseError
            The transport delivered text that could not be
            parsed as a JSON object.
        """
        transport = self._transport or self._default_transport

        logger.debug(
            "Gateway call: provider=%r model=%r connection=%r",
            provider_id, model_id, connection_id,
        )

        response = await transport(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            provider_id=provider_id,
            model_id=model_id,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
            connection_id=connection_id,
        )

        return response

    async def _default_transport(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        provider_id: str,
        model_id: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        connection_id: str = "memory_extraction",
    ) -> LLMStructuredResponse:
        """Real transport: calls the configured LLM provider.

        Phase 1-C completion. Replaces the Phase 1-B
        ``NotImplementedError`` stub with a full dispatch that
        mirrors (but does NOT share code with) ``dispatcher.py``'s
        chat path. The memory pipeline is deliberately independent
        because it has different requirements (structured JSON
        output, extraction-specific prompts, bounded per-job
        timeout) that would clutter the chat dispatcher.

        Pipeline
        --------
          1. Resolve credentials from the models vault (or from
             caller-supplied overrides).
          2. Build a ``ConnectionContext`` for the provider.
          3. Resolve model capabilities via the canonical resolver.
          4. Build an ``LLMRequest`` with ``ResponseFormat(kind="text")``
             -- the extraction prompt explicitly asks for JSON, and
             ``parse_llm_json`` handles the markdown-fence tolerance.
          5. Dispatch via ``provider_factory.create()`` and
             ``adapter.generate()`` (non-streaming -- extraction
             is a single round-trip, not a chat stream).
          6. Record cost via ``default_tracker.complete()``.
          7. Parse the raw text into a Python dict.

        All ``peldrun.llm`` imports are lazy inside this method.
        At module load time, ``omweb.memory.extraction`` importing
        ``peldrun.llm.providers`` would trigger the registry
        bootstrap, which is expensive and unnecessary for every
        consumer of the extraction package.

        Error mapping
        -------------
          * ``asyncio.TimeoutError`` -> ``LLMTransportError``
          * Any provider/adapter exception -> ``LLMTransportError``
          * Unparseable text -> ``LLMParseError`` (from
            ``parse_llm_json``)

        All three are retryable by the worker's
        ``_handle_job_error`` path. The distinction is preserved
        only for diagnostics (the ``error_code`` in the jobs row).
        """
        import time
        import uuid

        # --- Lazy imports (avoid circular at module load) --------
        from peldrun.llm.accounting import (
            InvocationContext,
            default_tracker,
        )
        from peldrun.llm.capability_resolver import (
            resolve_model_capabilities,
        )
        from peldrun.llm.contracts import (
            ConnectionContext,
            ReasoningMode,
        )
        from peldrun.llm.providers import provider_factory
        from peldrun.llm.request import (
            GenerationConfig,
            LLMRequest,
            ReasoningConfig,
            ResponseFormat,
            ToolChoiceSpec,
        )

        # --- Step 1: resolve credentials ------------------------
        resolved_base_url, resolved_api_key, extra_params = (
            self._resolve_credentials(
                provider_id=provider_id,
                base_url=base_url,
                api_key=api_key,
            )
        )

        # --- Step 2: build ConnectionContext --------------------
        connection = ConnectionContext(
            connection_id=connection_id,
            provider_id=provider_id,
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            timeout=float(timeout_seconds),
            extra_params=extra_params,
        )

        # --- Step 3: resolve model capabilities -----------------
        model_caps = resolve_model_capabilities(
            provider_id=provider_id,
            model_id=model_id,
            connection_id=None,
        )

        # --- Step 4: build LLMRequest ---------------------------
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        llm_request = LLMRequest(
            provider_id=provider_id,
            model_id=model_id,
            connection_id=connection_id,
            messages=messages,
            system_prompt=None,  # already embedded in messages
            tools=[],
            tool_choice=ToolChoiceSpec(mode="auto"),
            generation=GenerationConfig(
                temperature=0.0,
                max_output_tokens=max_output_tokens,
            ),
            reasoning=ReasoningConfig(
                enabled=False,
                mode=ReasoningMode.NONE,
            ),
            # The extraction prompt asks for JSON. We deliberately
            # use kind="text" because not every provider supports
            # native JSON mode, and parse_llm_json already handles
            # the fenced/prose tolerances. Native JSON mode would
            # be a per-provider optimisation, not a correctness
            # requirement.
            response_format=ResponseFormat(kind="text"),
            model_capabilities=model_caps,
            connection=connection,
        )

        # --- Step 5: dispatch -----------------------------------
        start_time = time.time()
        adapter = None
        try:
            adapter = provider_factory.create(provider_id, connection)
            try:
                response = await asyncio.wait_for(
                    adapter.generate(llm_request),
                    timeout=float(timeout_seconds),
                )
            finally:
                if adapter is not None:
                    try:
                        await adapter.close()
                    except Exception as close_err:
                        logger.debug(
                            "Extraction transport: adapter.close() "
                            "raised (non-fatal): %s", close_err,
                        )
        except asyncio.TimeoutError as exc:
            raise LLMTransportError(
                f"LLM call timed out after {timeout_seconds}s",
                provider_id=provider_id,
                model_id=model_id,
                cause=exc,
            ) from exc
        except LLMTransportError:
            raise
        except Exception as exc:
            raise LLMTransportError(
                f"Provider call failed: {exc}",
                provider_id=provider_id,
                model_id=model_id,
                cause=exc,
            ) from exc

        # --- Step 6: accounting ---------------------------------
        usage = getattr(response, "usage", None)
        inv_id = f"inv_extract_{uuid.uuid4().hex[:10]}"
        tracker_ctx = InvocationContext(
            provider=provider_id,
            model_requested=model_id,
            mode="extraction",  # distinct from "chat"/"agent"
            job_id=connection_id,
            chat_id=None,
            project_id=None,
            turn_id=None,
            base_url=resolved_base_url or "",
        )
        try:
            record = await default_tracker.complete(
                response=response,
                context=tracker_ctx,
                started_at=start_time,
                invocation_id=inv_id,
            )
            cost_nano_usd = int(
                getattr(record, "cost_nano_usd", 0) or 0
            )
        except Exception as exc:
            logger.warning(
                "Extraction transport: cost tracking failed "
                "(using 0): %s", exc,
            )
            cost_nano_usd = 0

        # --- Step 7: parse JSON ---------------------------------
        raw_text = getattr(response, "content", "") or ""
        parsed = parse_llm_json(raw_text)

        return LLMStructuredResponse(
            parsed=parsed,
            raw_text=raw_text,
            cost_nano_usd=cost_nano_usd,
            input_tokens=(
                int(getattr(usage, "input_tokens", 0) or 0)
                if usage else 0
            ),
            output_tokens=(
                int(getattr(usage, "output_tokens", 0) or 0)
                if usage else 0
            ),
        )

    # ------------------------------------------------------------------
    # Credential resolution (from models_vault.json)
    # ------------------------------------------------------------------

    def _resolve_credentials(
        self,
        *,
        provider_id: str,
        base_url: Optional[str],
        api_key: Optional[str],
    ) -> tuple[Optional[str], Optional[str], dict]:
        """Return ``(base_url, api_key, extra_params)`` for a provider.

        Resolution order (highest priority first):

          1. Caller-supplied ``base_url`` / ``api_key`` -- a
             per-request override. Used by tests and by any future
             "temporary key" flow. A NON-EMPTY value here wins
             over the vault, but a ``None`` / empty string
             falls through so callers can override just one of
             the two fields.
          2. ``models_vault.json`` -- the operator's configured
             credentials. This is the ONLY production source.

        Vault shape (canonical, as observed in production)
        --------------------------------------------------
        The vault file has FIVE provider containers, each with a
        different shape:

          ``cloud_vault``          -- ARRAY; each item has
                                      ``id``, ``baseUrl``,
                                      ``apiKey``, optional
                                      ``type``. Covers openai,
                                      google, anthropic, deepseek.

          ``lmstudio_vault``       -- SINGLE object for the local
                                      LM Studio deployment.

          ``ollama_vault``         -- SINGLE object for the local
                                      Ollama deployment. Usually
                                      NO ``apiKey`` (local, no auth).

          ``custom_endpoints``     -- ARRAY of user-added endpoints;
                                      each item has ``id`` or
                                      ``name``.

          ``provider_connections`` -- ARRAY of plugin-package
                                      connections. Each item has
                                      ``provider_package_id``,
                                      ``fields: {base_url,
                                      api_key, ...}``, and
                                      ``is_active``.

        This method probes each container IN THAT ORDER and
        returns the first match. The two ``*_vault`` single-
        objects are checked by comparing ``provider_id`` against
        their canonical names (``lmstudio`` / ``ollama``).

        Vault location
        --------------
          1. ``PELDRUN_MODELS_VAULT_PATH`` env var (tests).
          2. ``<backend>/data/models_vault.json``.
          3. ``<backend>/data/models_vault copy.json``
             (Windows duplicate-file naming).

        Returns ``(None, None, {})`` when no credentials can be
        resolved. The caller (``provider_factory.create``) then
        falls back to its own defaults, which usually means the
        adapter raises a clear "missing base_url / api_key" error
        at call time -- better than a silent wrong-endpoint call.

        Why direct file read (not a helper)
        -----------------------------------
        ``omweb.routers.config_rtr`` does NOT expose a general
        "credentials for provider X" helper in the GitHub tree.
        Adding one would couple the memory subsystem to the web
        routers -- violating the architectural boundary that the
        extraction package is meant to be router-free. Reading the
        vault file directly keeps the memory subsystem fully
        decoupled: the vault is a stable JSON contract, and the
        extractor only needs two keys from it.
        """
        import json
        import os
        from pathlib import Path

        # Fast path: both supplied by caller -- no vault read.
        if base_url and api_key:
            return (base_url, api_key, {})

        # --- Locate the vault ------------------------------------
        vault_path: Optional[Path] = None
        env_override = os.environ.get(
            "PELDRUN_MODELS_VAULT_PATH", ""
        ).strip()
        if env_override:
            vault_path = Path(env_override).expanduser()
        else:
            backend_dir = Path(__file__).resolve().parents[3]
            for candidate in (
                backend_dir / "data" / "models_vault.json",
                backend_dir / "data" / "models_vault copy.json",
            ):
                if candidate.is_file():
                    vault_path = candidate
                    break

        if vault_path is None or not vault_path.is_file():
            logger.warning(
                "Extraction transport: no models vault found "
                "(provider=%r). Checked PELDRUN_MODELS_VAULT_PATH "
                "env and <backend>/data/models_vault.json. "
                "Caller-supplied credentials will be used if "
                "present.",
                provider_id,
            )
            return (base_url, api_key, {})

        # --- Read vault ------------------------------------------
        try:
            with open(vault_path, "r", encoding="utf-8") as f:
                vault = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Extraction transport: vault read failed at %s "
                "(%s); caller-supplied credentials will be used "
                "if present.",
                vault_path,
                exc,
            )
            return (base_url, api_key, {})

        if not isinstance(vault, dict):
            return (base_url, api_key, {})

        provider_lower = provider_id.strip().lower()

        # --- 1. cloud_vault (array) ------------------------------
        cloud_vault = vault.get("cloud_vault")
        if isinstance(cloud_vault, list):
            for entry in cloud_vault:
                if not isinstance(entry, dict):
                    continue
                entry_id = str(entry.get("id", "")).strip().lower()
                if entry_id == provider_lower:
                    return self._extract_from_entry(
                        entry, base_url, api_key,
                    )

        # --- 2. lmstudio_vault (single object) -------------------
        if provider_lower in ("lmstudio", "lm-studio", "lm_studio"):
            lm = vault.get("lmstudio_vault")
            if isinstance(lm, dict):
                return self._extract_from_entry(
                    lm, base_url, api_key,
                )

        # --- 3. ollama_vault (single object) ---------------------
        if provider_lower == "ollama":
            ol = vault.get("ollama_vault")
            if isinstance(ol, dict):
                return self._extract_from_entry(
                    ol, base_url, api_key,
                )

        # --- 4. custom_endpoints (array) -------------------------
        custom = vault.get("custom_endpoints")
        if isinstance(custom, list):
            for entry in custom:
                if not isinstance(entry, dict):
                    continue
                for key in ("id", "name"):
                    raw = entry.get(key)
                    if (
                        isinstance(raw, str)
                        and raw.strip().lower() == provider_lower
                    ):
                        return self._extract_from_entry(
                            entry, base_url, api_key,
                        )

        # --- 5. provider_connections (array, plugin packages) ----
        # Used by package-managed providers (gigachat, cloudflare-ai,
        # etc.). These use snake_case field names, unlike the cloud
        # vault's camelCase.
        conns = vault.get("provider_connections")
        if isinstance(conns, list):
            for conn in conns:
                if not isinstance(conn, dict):
                    continue
                pkg_id = str(
                    conn.get("provider_package_id", "")
                ).strip().lower()
                if pkg_id != provider_lower:
                    continue
                if not conn.get("is_active", False):
                    logger.debug(
                        "Extraction transport: provider_connection "
                        "for %r exists but is_active=False; "
                        "skipping.",
                        provider_id,
                    )
                    continue
                fields = conn.get("fields") or {}
                if not isinstance(fields, dict):
                    continue
                # Build a pseudo-entry in the camelCase shape that
                # `_extract_from_entry` understands, then merge the
                # remaining snake_case fields as extra_params.
                #
                # [PHASE 1-C FIX] The OUTER connection object carries
                # metadata fields (``name``, ``enabled``) that adapters
                # may want to see in ``extra_params`` -- e.g. for
                # logging a human-readable connection name, or for
                # feature-gating on ``enabled``. The ``fields`` sub-
                # dict only holds the CREDENTIAL-shaped fields
                # (base_url, api_key, scope, verify_ssl, ...). Both
                # must be merged: without the outer metadata, the
                # ``name`` assertion in the vault-shape test fails
                # because the field is silently dropped.
                pseudo_entry = {
                    "baseUrl": fields.get("base_url"),
                    "apiKey": fields.get("api_key"),
                }
                for _meta_key in ("name", "enabled"):
                    if _meta_key in conn:
                        pseudo_entry[_meta_key] = conn[_meta_key]
                remaining_extra = {
                    k: v
                    for k, v in fields.items()
                    if k not in ("base_url", "api_key")
                }
                resolved_url, resolved_key, base_extra = (
                    self._extract_from_entry(
                        pseudo_entry, base_url, api_key,
                    )
                )
                return (
                    resolved_url,
                    resolved_key,
                    {**base_extra, **remaining_extra},
                )

        logger.warning(
            "Extraction transport: provider %r not found in vault "
            "%s (searched cloud_vault, lmstudio_vault, "
            "ollama_vault, custom_endpoints, provider_connections).",
            provider_id,
            vault_path,
        )
        return (base_url, api_key, {})

    @staticmethod
    def _extract_from_entry(
        entry: dict,
        base_url: Optional[str],
        api_key: Optional[str],
    ) -> tuple[Optional[str], Optional[str], dict]:
        """Pull ``(base_url, api_key, extra_params)`` from a vault entry.

        Caller-supplied values win over vault values (per-request
        override semantics). Empty / whitespace-only vault values
        are treated as absent so a placeholder like ``"sk- "``
        does not shadow a legitimate caller override.

        ``extra_params`` carries every OTHER field on the entry
        verbatim -- this is how ``api_type`` (from Anthropic's
        ``type: "anthropic"``) and other adapter-specific hints
        reach ``ConnectionContext.extra_params``. The two
        fields consumed here (``baseUrl`` / ``apiKey``) are
        stripped so the adapter cannot see them via
        ``extra_params``.
        """
        vault_base_url = entry.get("baseUrl")
        vault_api_key = entry.get("apiKey")

        resolved_base_url = base_url or (
            vault_base_url
            if isinstance(vault_base_url, str)
            and vault_base_url.strip()
            else None
        )
        resolved_api_key = api_key or (
            vault_api_key
            if isinstance(vault_api_key, str)
            and vault_api_key.strip()
            else None
        )

        extra: dict = {
            k: v
            for k, v in entry.items()
            if k not in ("baseUrl", "apiKey")
        }

        # Map the Anthropic-style ``type`` field to the canonical
        # ``api_type`` key that ``provider_factory`` understands.
        # This preserves the dispatcher's routing intent: a
        # provider configured with ``type: "anthropic"`` must be
        # routed to the native anthropic adapter, not openai_compat.
        if "type" in extra and "api_type" not in extra:
            raw_type = extra.get("type")
            if isinstance(raw_type, str) and raw_type.strip():
                extra["api_type"] = raw_type.strip()

        return (resolved_base_url, resolved_api_key, extra)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "LLMStructuredResponse",
    "LLMTransportError",
    "LLMParseError",
    "MemoryModelGateway",
    "TransportFn",
    "parse_llm_json",
]
