# backend/peldrun/memory/embeddings/provider.py
"""
PELDRUN Memory Platform - OpenAI-Compatible Embedding Provider.

The adapter that implements ``EmbeddingProvider`` for every backend
exposing the standard ``POST /embeddings`` endpoint: OpenAI, Google
Gemini (via its OpenAI-compat surface), LM Studio, Ollama (via
``/v1``), vLLM, SGLang, TGI, Cohere, Voyage, Jina, and any future
provider following the same protocol.

Design philosophy
-----------------

**Hermetic to the chat path.** This module builds ``AsyncOpenAI``
directly — the same public SDK class the chat transport uses, but
constructed for the embeddings endpoint. It does NOT:
    * import or modify ``peldrun.llm.client``,
    * import or modify ``peldrun.llm.providers``,
    * rely on ``provider_factory``, ``LLMRequest``, or the
      capability resolver.

The chat path is untouched. A future change to any of those
components cannot affect embeddings. This is the same discipline
the extraction gateway already applies (see ``gateway.py``'s
``_transport_via_openai_compat`` — identical rationale, identical
result).

**No hardcoded providers.** Every backend that speaks the OpenAI
embeddings protocol works through this single class. Swapping
providers means changing the vault credentials — not the code.
Adding a new backend means registering it in the vault — not
editing this file.

**No cost arithmetic.** The provider returns the token counts the
backend reported (``usage.prompt_tokens``) and ``cost_nano_usd=0``.
Cost computation lives in the caller's accounting layer, matching
the split of concerns already established by
``MemoryModelGateway``.

Provider quirks handled here
----------------------------

1. **Base URL normalization.**
   Native API URLs (``/api/v1``, ``/api/v0``, ``/api``) are stripped
   before being passed to the SDK, so ``AsyncOpenAI`` can append
   ``/embeddings`` without producing paths like
   ``/api/api/v1/embeddings``. Already-compat URLs (ending in
   ``/v1`` or ``/v1beta/openai``) are preserved. Bare hosts get
   ``/v1`` appended. See ``_normalize_base_url_for_openai_sdk``.

2. **Missing API key.**
   Local servers (LM Studio, Ollama) accept requests without auth,
   but the OpenAI SDK refuses to start without an API key. We
   substitute a placeholder string when the caller passed ``None``.

3. **Batch chunking.**
   Batches exceeding ``max_batch_size`` (default 2048, OpenAI's
   documented limit) are split transparently and recombined. Callers
   never have to know each provider's batch limit.

4. **Error mapping.**
   The OpenAI SDK's exception hierarchy is translated to the
   ``EmbeddingError`` family, so callers (the embedding worker,
   the retrieval router) can apply the correct retry policy without
   importing OpenAI SDK internals.

Provider quirks NOT handled here (deliberate)
---------------------------------------------

* **Retries.** Failures propagate as exceptions. The embedding
  worker is the only place retries happen.
* **Rate limiting.** 429s surface as ``EmbeddingTransportError``,
  which the worker's backoff treats as retryable.
* **Batch size refusal.** The class auto-chunks. Callers who
  specifically need to know about the limit can inspect
  ``max_batch_size`` on the instance.
* **Cost.** See above.

Version tracking
----------------

``model_version`` defaults to ``model_id`` when the caller did not
supply a distinct version. This is the documented fallback in
``contracts.py``. OpenAI does not expose a version dimension
distinct from the model id; local servers likewise. If a future
backend exposes one (e.g. a Cohere model revision), the caller
passes it via the ``model_version`` constructor argument, and the
vector index keys embeddings by ``(model_id, model_version)`` so
the two namespaces stay separate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from peldrun.memory.embeddings.contracts import (
    EmbeddingError,
    EmbeddingResult,
    EmbeddingTransportError,
    EmbeddingUnavailableError,
)

if TYPE_CHECKING:
    # Imported only for type checking — the real import happens
    # lazily inside ``_get_client`` so that this module can be
    # imported in environments where the SDK is temporarily
    # unavailable (e.g. during preflight checks with a mismatched
    # interpreter). See ``_get_client`` for the runtime import.
    from openai import AsyncOpenAI


logger = logging.getLogger("peldrun.memory.embeddings.provider")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_DEFAULT_TIMEOUT_SECONDS: float = 30.0
"""Per-request timeout, matching typical embedding-call latencies
well below the chat timeout. Embedding requests are compute-light
compared to chat completions; a 30-second budget is generous and
prevents a wedged network from blocking the worker for minutes."""


_DEFAULT_MAX_BATCH_SIZE: int = 2048
"""Maximum number of texts per API call. Matches OpenAI's
documented limit; most OpenAI-compat servers honor the same cap.
The class auto-chunks larger inputs, so this value is a
performance knob, not a hard constraint for callers."""


_PLACEHOLDER_API_KEY: str = "sk-no-key-required"
"""Substituted when the caller passed ``None``. Local servers
(LM Studio, Ollama, vLLM without auth) ignore the value entirely;
the OpenAI SDK still requires *some* non-empty string to start."""


# ---------------------------------------------------------------------------
# Base URL normalization
# ---------------------------------------------------------------------------


def _normalize_base_url_for_openai_sdk(url: Optional[str]) -> Optional[str]:
    """Normalize a vault base_url into a form suitable for AsyncOpenAI.

    ``AsyncOpenAI`` expects ``base_url`` to be the prefix the SDK
    appends ``/embeddings`` to. For OpenAI that is
    ``https://api.openai.com/v1``; for a local vLLM deployment that
    is ``http://host:port/v1``.

    Three cases handled
    -------------------

    **Case A — native API URL** (``http://host:port/api/v1``,
    ``/api/v0``, ``/api``). The native suffix is stripped, leaving
    the host. If the result is then a bare host, ``/v1`` is
    appended (the standard compat convention).

        http://localhost:1234/api/v1 -> http://localhost:1234/v1
        http://localhost:8000/api    -> http://localhost:8000/v1

    **Case B — bare host** (``http://localhost:1234``). ``/v1`` is
    appended, because every OpenAI-compatible local server serves
    the compat endpoints under ``/v1``.

        http://localhost:1234       -> http://localhost:1234/v1

    **Case C — already a compat URL** (ending in ``/v1`` or a
    provider-specific compat suffix such as Google's
    ``/v1beta/openai``). The URL is preserved unchanged, because
    the caller already produced the correct form and stripping it
    would break the request.

        https://api.openai.com/v1   -> unchanged
        https://generativelanguage.googleapis.com/v1beta/openai
                                    -> unchanged

    ``None`` returns ``None`` so the SDK uses its own default
    (``https://api.openai.com/v1``).
    """
    if not url:
        return None

    clean = url.rstrip("/")

    # Case A — strip a native suffix when present.
    for native_suffix in ("/api/v1", "/api/v0", "/api"):
        if clean.endswith(native_suffix):
            clean = clean[: -len(native_suffix)].rstrip("/")
            break

    # Cases B and C — if we stripped to a bare host (no path), the
    # SDK needs /v1. Otherwise the caller already provided the
    # correct compat path (e.g. /v1 or /v1beta/openai).
    parsed = urlparse(clean)
    if not parsed.path or parsed.path == "/":
        clean = clean + "/v1"

    return clean


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class OpenAICompatEmbeddingProvider:
    """Embedding adapter for any OpenAI-compatible ``/embeddings`` endpoint.

    Construct one instance per ``(provider, model)`` pair. The
    instance holds:
        * the model id and version (fixed at construction),
        * the vector dimension (validated against every response),
        * the resolved base URL (normalized once, reused for every
          call),
        * an ``AsyncOpenAI`` client (lazily created on first call,
          closed by ``close()``).

    All methods are safe to call concurrently from one event loop.
    The client is created once and reused across calls; concurrent
    ``embed_batch`` calls share it (the SDK handles multiplexing).

    Lifecycle
    ---------
    Instantiate → ``await embed_batch(...)`` (any number of times)
    → ``await close()``. The provider is unusable after ``close()``
    — attempting to embed raises ``EmbeddingError``. This matches
    the chat providers' lifecycle contract.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str],
        api_key: Optional[str],
        model_id: str,
        model_version: Optional[str] = None,
        dimensions: int,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Initialize the provider.

        Parameters
        ----------
        base_url
            Provider endpoint. ``None`` uses the OpenAI SDK default
            (``https://api.openai.com/v1``). Native API URLs (ending
            in ``/api/v1``, ``/api/v0``, or ``/api``) are normalized
            to the compat form (see module docstring).
        api_key
            Bearer token. ``None`` uses a placeholder for local
            servers that do not require auth.
        model_id
            Provider-specific model identifier (e.g.
            ``"text-embedding-3-small"``,
            ``"models/text-embedding-004"``). REQUIRED; the
            instance is bound to one model.
        model_version
            Optional version string. Defaults to ``model_id`` when
            ``None``. Used as part of the vector index primary key.
        dimensions
            Vector dimension. REQUIRED; used by
            ``EmbeddingResult.validate_against`` to reject
            responses from a different model. The caller is
            responsible for resolving this value — see
            ``probe_dimensions`` below for the one-time discovery
            path.
        timeout_seconds
            Per-request timeout. Default 30s. Raise this if the
            provider is known to be slow on cold starts.
        max_batch_size
            Maximum number of texts per API call. Larger batches
            are auto-chunked. Default 2048 (OpenAI's limit).
        extra_headers
            Optional per-provider HTTP headers (e.g. OpenRouter's
            ``HTTP-Referer``). Forwarded to the SDK's
            ``default_headers``.

        Raises
        ------
        ValueError
            When ``model_id`` is empty, ``dimensions`` is not a
            positive int, or ``timeout_seconds`` is not positive.
        """
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError(
                f"model_id must be a non-empty string, got "
                f"{model_id!r}"
            )
        if not isinstance(dimensions, int) or isinstance(dimensions, bool):
            raise ValueError(
                f"dimensions must be an int, got "
                f"{type(dimensions).__name__}"
            )
        if dimensions <= 0:
            raise ValueError(
                f"dimensions must be positive, got {dimensions}"
            )
        if timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be positive, got "
                f"{timeout_seconds}"
            )
        if max_batch_size <= 0:
            raise ValueError(
                f"max_batch_size must be positive, got "
                f"{max_batch_size}"
            )

        self._base_url = _normalize_base_url_for_openai_sdk(base_url)
        self._api_key = api_key
        self._model_id = model_id.strip()
        self._model_version = (
            model_version.strip()
            if isinstance(model_version, str) and model_version.strip()
            else self._model_id
        )
        self._dimensions = dimensions
        self._timeout_seconds = float(timeout_seconds)
        self._max_batch_size = int(max_batch_size)
        self._extra_headers: Dict[str, str] = dict(extra_headers or {})

        # Lazy — created on first embed_batch call. See _get_client.
        self._client: Optional["AsyncOpenAI"] = None

    # ------------------------------------------------------------------
    # EmbeddingProvider protocol
    # ------------------------------------------------------------------

    async def embed_batch(
        self,
        texts: Sequence[str],
        model_id: str,
    ) -> EmbeddingResult:
        """Embed a batch of texts in one or more API calls.

        See ``EmbeddingProvider.embed_batch`` for the protocol-level
        contract. Provider-specific behavior:

        * **Auto-chunking.** Inputs larger than ``max_batch_size``
          are split transparently and recombined, preserving order.
          ``input_tokens`` and ``cost_nano_usd`` on the returned
          ``EmbeddingResult`` sum across all chunks.

        * **Model-id validation.** ``model_id`` MUST match the
          constructor's ``model_id``. A mismatch is a caller bug
          (a provider instance is bound to one model) and raises
          ``EmbeddingError``.

        * **Empty input.** Returns an empty
          ``EmbeddingResult(vectors=[])`` without an API call.
          Useful for the caller to short-circuit empty batches.

        * **Dimension check.** Every response is validated via
          ``EmbeddingResult.validate_against``; a vector whose
          length differs from ``dimensions`` raises
          ``EmbeddingDimensionMismatchError``. This is the last
          line of defense against silent vector-space drift.
        """
        # 1. Model binding check.
        if model_id != self._model_id:
            raise EmbeddingError(
                f"embed_batch called with model_id={model_id!r} but "
                f"this provider instance is bound to "
                f"model_id={self._model_id!r}. Create a separate "
                f"provider instance for a different model — one "
                f"instance per (provider, model) pair."
            )

        # 2. Empty batch — no API call.
        if not texts:
            return EmbeddingResult(
                vectors=[],
                model_id=self._model_id,
                model_version=self._model_version,
                input_tokens=0,
                cost_nano_usd=0,
            )

        # 3. Materialize to list — Sequence may be a generator.
        text_list = list(texts)

        # 4. Single-chunk fast path.
        if len(text_list) <= self._max_batch_size:
            return await self._embed_one_chunk(text_list)

        # 5. Multi-chunk path — split, embed sequentially, combine.
        #
        # Sequential (not parallel) on purpose: concurrent calls
        # against the same provider would hit rate limits and
        # multiply 429s. The worker already parallelizes at the
        # job level; within one job, sequential chunking keeps
        # the provider's view of our traffic linear.
        all_vectors: List[List[float]] = []
        total_input_tokens = 0
        total_cost = 0

        for start in range(0, len(text_list), self._max_batch_size):
            chunk = text_list[start : start + self._max_batch_size]
            chunk_result = await self._embed_one_chunk(chunk)
            all_vectors.extend(chunk_result.vectors)
            total_input_tokens += chunk_result.input_tokens
            total_cost += chunk_result.cost_nano_usd

        return EmbeddingResult(
            vectors=all_vectors,
            model_id=self._model_id,
            model_version=self._model_version,
            input_tokens=total_input_tokens,
            cost_nano_usd=total_cost,
        )

    @property
    def dimensions(self) -> int:
        """Vector dimension, validated against every response."""
        return self._dimensions

    @property
    def model_version(self) -> str:
        """Opaque version string for the vector index primary key."""
        return self._model_version

    async def close(self) -> None:
        """Close the underlying SDK client.

        Idempotent: calling twice is a no-op. Never raises. Matches
        the lifecycle contract of the chat providers (see
        ``ProviderAdapter.close``).
        """
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:
                logger.debug(
                    "OpenAICompatEmbeddingProvider.close(): "
                    "client.close() raised (non-fatal): %s",
                    exc,
                )
            finally:
                self._client = None

    # ------------------------------------------------------------------
    # Internal — API call
    # ------------------------------------------------------------------

    async def _embed_one_chunk(
        self, texts: List[str]
    ) -> EmbeddingResult:
        """Embed one chunk (``len(texts) <= max_batch_size``).

        Assumes ``texts`` is non-empty and already validated for
        model binding. Never called directly by external code.
        """
        client = self._get_client()

        # Lazy import of the error types — the runtime ``openai``
        # package is the one ``_get_client`` just imported. Importing
        # these here (rather than at module top) keeps module import
        # cheap for callers who never embed, and ensures the error
        # types match the actual SDK version in use.
        from openai import (
            APIConnectionError,
            APIError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            NotFoundError,
            PermissionDeniedError,
            RateLimitError,
        )

        try:
            response = await client.embeddings.create(
                model=self._model_id,
                input=texts,
            )
        except (AuthenticationError, PermissionDeniedError) as exc:
            raise EmbeddingUnavailableError(
                f"Authentication failed for embedding model "
                f"{self._model_id!r} at {self._base_url!r}: {exc}. "
                f"Check the API key in the model vault."
            ) from exc
        except NotFoundError as exc:
            raise EmbeddingUnavailableError(
                f"Embedding model {self._model_id!r} not found at "
                f"{self._base_url!r}: {exc}. The model may not be "
                f"offered by this provider, or the base URL may be "
                f"incorrect."
            ) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            raise EmbeddingTransportError(
                f"Could not reach embedding provider at "
                f"{self._base_url!r} for model {self._model_id!r}: "
                f"{exc}"
            ) from exc
        except RateLimitError as exc:
            raise EmbeddingTransportError(
                f"Embedding provider rate limit hit for model "
                f"{self._model_id!r}: {exc}. The worker's retry "
                f"backoff will handle this."
            ) from exc
        except BadRequestError as exc:
            # 400 — usually a request the provider cannot satisfy
            # (malformed input, unsupported model_id, over-long
            # text). Not retryable; surface it so the operator
            # can see the exact provider complaint.
            raise EmbeddingError(
                f"Embedding provider rejected the request for "
                f"model {self._model_id!r}: {exc}. This usually "
                f"indicates a malformed request or an unsupported "
                f"input (e.g. text too long for this model)."
            ) from exc
        except APIError as exc:
            # Catch-all for other 5xx / unknown API failures.
            # Retryable — the worker's backoff covers this path.
            raise EmbeddingTransportError(
                f"Embedding provider returned an error for model "
                f"{self._model_id!r}: {exc}"
            ) from exc

        vectors = [list(item.embedding) for item in response.data]

        # Usage — OpenAI returns usage.prompt_tokens; some compat
        # servers omit usage entirely. Treat absence as 0.
        usage = getattr(response, "usage", None)
        input_tokens = 0
        if usage is not None:
            try:
                input_tokens = int(
                    getattr(usage, "prompt_tokens", 0) or 0
                )
            except (TypeError, ValueError):
                input_tokens = 0

        result = EmbeddingResult(
            vectors=vectors,
            model_id=self._model_id,
            model_version=self._model_version,
            input_tokens=input_tokens,
            cost_nano_usd=0,  # Computed by the caller's accounting layer.
        )

        # Last line of defense against vector-space drift.
        result.validate_against(
            expected_count=len(texts),
            expected_dimensions=self._dimensions,
        )

        return result

    # ------------------------------------------------------------------
    # Internal — client management
    # ------------------------------------------------------------------

    def _get_client(self) -> "AsyncOpenAI":
        """Return the AsyncOpenAI client, creating it on first use.

        Lazy creation is deliberate: importing ``openai`` at module
        load time would slow down every consumer of
        ``peldrun.memory.embeddings`` (including tests that only
        touch ``contracts.py``). Constructing the client on first
        ``embed_batch`` keeps module import cheap.
        """
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise EmbeddingUnavailableError(
                    "The 'openai' Python package is not installed. "
                    "Run 'pip install openai' to enable embedding "
                    "support. (It is a PELDRUN dependency; this "
                    "error usually means the active interpreter "
                    "differs from the one PELDRUN was installed "
                    "into.)"
                ) from exc

            self._client = AsyncOpenAI(
                base_url=self._base_url,
                api_key=self._api_key or _PLACEHOLDER_API_KEY,
                timeout=self._timeout_seconds,
                default_headers=self._extra_headers or {},
            )
        return self._client

    # ------------------------------------------------------------------
    # Dimension discovery (class method, no instance required)
    # ------------------------------------------------------------------

    @classmethod
    async def probe_dimensions(
        cls,
        *,
        base_url: Optional[str],
        api_key: Optional[str],
        model_id: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> int:
        """Discover an embedding model's vector dimension.

        Sends a single ``"probe"`` text and returns ``len(vector)``.
        Intended to run ONCE per (provider, model) pair, typically
        by ``EmbeddingModelResolver`` when the operator first
        selects an embedding model. The result is stored alongside
        the model configuration so subsequent constructions do not
        need to probe.

        Cost: ~1-5 input tokens. Negligible.

        Parameters
        ----------
        base_url, api_key, model_id
            Same semantics as the constructor.
        timeout_seconds
            Per-probe timeout. Default 30s.

        Returns
        -------
        int
            The provider's vector dimension.

        Raises
        ------
        EmbeddingError
            Any failure (auth, network, model not found, empty
            response). The message names the model and base URL.
        """
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise EmbeddingUnavailableError(
                "The 'openai' Python package is not installed; "
                "cannot probe embedding dimensions. Run "
                "'pip install openai' and try again."
            ) from exc

        normalized_url = _normalize_base_url_for_openai_sdk(base_url)
        client = AsyncOpenAI(
            base_url=normalized_url,
            api_key=api_key or _PLACEHOLDER_API_KEY,
            timeout=timeout_seconds,
        )
        try:
            try:
                response = await client.embeddings.create(
                    model=model_id,
                    input=["probe"],
                )
            except Exception as exc:
                raise EmbeddingError(
                    f"Could not probe dimensions for model "
                    f"{model_id!r} at {base_url!r}: {exc}"
                ) from exc

            if not response.data:
                raise EmbeddingError(
                    f"Provider returned no vectors when probing "
                    f"dimensions for model {model_id!r} at "
                    f"{base_url!r}"
                )
            return len(response.data[0].embedding)
        finally:
            try:
                await client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Convenience subclass
# ---------------------------------------------------------------------------


class GoogleEmbeddingProvider(OpenAICompatEmbeddingProvider):
    """Convenience subclass signaling "Google embeddings".

    Google exposes an OpenAI-compatible embeddings endpoint at
    ``https://generativelanguage.googleapis.com/v1beta/openai``.
    Using it instead of the native ``google-genai`` API gives us:

      * identical batch semantics as every other provider,
      * identical error mapping (the SDK exceptions the parent
        handles are the same ones Google's compat endpoint
        produces),
      * zero code duplication.

    This class adds **no behavior**. It exists so that downstream
    code (log messages, diagnostics, type hints) can name the
    intent explicitly rather than always saying "OpenAI-compat".
    The caller MUST supply the correct ``base_url`` (the
    ``EmbeddingModelResolver`` reads it from the vault); this
    class does not set a default.
    """

    pass


# ---------------------------------------------------------------------------
# Aliases
# ---------------------------------------------------------------------------


#: Alias matching the name used in the implementation plan
#: (§5.1). Both names refer to the same class.
OpenAIEmbeddingProvider = OpenAICompatEmbeddingProvider


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "GoogleEmbeddingProvider",
    "OpenAICompatEmbeddingProvider",
    "OpenAIEmbeddingProvider",
]
