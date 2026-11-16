# backend/peldrun/memory/embeddings/contracts.py
"""
PELDRUN Memory Platform - Embedding Provider Contracts.

Provider-agnostic interface for embedding models.

Design philosophy
-----------------

Mirrors the LLM provider architecture in ``peldrun.llm``: a
``Protocol`` plus multiple adapters, each adapter owning its own
API quirks. The gateway selects one based on user settings -- no
hardcoded provider names anywhere in the retrieval code.

Consumers never import a concrete provider class directly; they
always program against ``EmbeddingProvider``. This is what makes
the vector search layer (``peldrun/memory/retrieval/vector_search.py``)
provider-neutral and testable with an in-memory stub.

Batch semantics
---------------

``embed_batch`` accepts multiple texts in one call. The rationale
is cost, not API shape: every supported backend (OpenAI, Google,
Cohere, Ollama, LM Studio) charges for embeddings by input-token
count and offers a batched endpoint. Issuing one call per text
would multiply request overhead (TLS, auth, JSON parsing) by the
batch size, often 10x-100x the actual compute time.

The contract requires the provider to return exactly one vector
per input text, in the same order. This invariant is the caller's
only protection against silent misalignment between documents and
their embeddings (which would corrupt retrieval with no error).
``EmbeddingResult.validate_against`` enforces the caller-side
check; ``EmbeddingResult.__post_init__`` enforces internal
consistency at construction time.

Version tracking
----------------

``model_version`` is the provider's opaque version string for the
embedding model, NOT a semver. It exists so that:

  1. The SQLite vector index can key embeddings by
     ``(model_id, model_version)`` -- required when a user switches
     embedding models: the old vectors are useless with the new
     model (different vector space) and must not be searched
     against.

  2. Re-embedding jobs can be scheduled when the version changes
     without invalidating existing vectors for the old version.

Adapters that do not expose a version return a stable fallback
(usually the model_id itself). See ``versioning.py`` (Task 4.1
follow-up) for the registry that resolves concrete versions.

No mutable module-level state
-----------------------------

This module defines only types, protocols, and exceptions. No
singletons, no caches, no side effects at import time. Importing
``contracts`` is always safe from any context (test, CLI,
headless worker).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence, runtime_checkable


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EmbeddingError(Exception):
    """Base class for embedding subsystem failures.

    All concrete errors below derive from this class so that
    callers can catch ``EmbeddingError`` to handle the whole
    family without enumerating subclasses.

    Rationale for keeping this exception family separate from
    ``MemoryError`` in ``peldrun.memory.domain.errors``: the
    embedding layer is a lower-level concern than the memory
    domain -- an embedding provider failure is not a memory-store
    failure. A caller that only needs to retry embeddings (e.g.,
    the embedding worker) can catch ``EmbeddingError`` without
    also catching unrelated memory-store errors like
    ``MemoryUnavailableError``.
    """


class EmbeddingUnavailableError(EmbeddingError):
    """The embedding provider is not reachable or not configured.

    Raised when:
      * The provider's credentials are missing from the vault.
      * The provider returned a non-retryable error (401/403/404
        indicating "model not found").
      * The requested ``model_id`` is not offered by the provider.

    Callers should treat this as a configuration problem: retrying
    without operator intervention will not fix it.
    """


class EmbeddingTransportError(EmbeddingError):
    """A transient error occurred (network, 5xx, timeout).

    Callers may retry with backoff. The message includes the
    provider's error text when available. This class is
    deliberately distinct from ``EmbeddingUnavailableError`` so
    the embedding worker (Task 4.4) can apply the correct retry
    policy: transient errors are retried automatically; permanent
    errors are surfaced to the operator.
    """


class EmbeddingDimensionMismatchError(EmbeddingError):
    """The provider returned vectors of an unexpected dimension.

    This is a hard error, not a retryable one. It indicates that:

      * The stored embeddings were created with a different model
        than the one currently configured (user changed models
        without reindexing), OR
      * The provider silently aliased the requested model to one
        with a different dimension (rare, but observed with
        OpenAI's ``text-embedding-ada-002`` -> ``text-embedding-3``
        migration, where the ``dimensions`` parameter can be set
        at request time).

    Either way, existing vectors cannot be mixed with the new ones
    -- the caller must schedule a full reindex before proceeding.
    """


class EmbeddingBatchSizeError(EmbeddingError):
    """The batch exceeds the provider's maximum input count.

    Providers enforce different limits:
      * OpenAI         -- 2048 inputs per request
      * Cohere         -- 96 inputs per request
      * Google         -- 100 inputs per request (batchEmbedContents)
      * LM Studio      -- no documented limit (implementation-bound)

    The adapter raises this error when the caller's batch exceeds
    what the provider will accept, so that the caller can split it
    rather than silently losing texts. The retrieval layer never
    hits this because its internal batch size is well below every
    provider's limit; the error exists for callers that submit
    user-controlled batches (e.g., a reindex worker).
    """


# ---------------------------------------------------------------------------
# EmbeddingResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingResult:
    """One successful ``embed_batch`` response.

    Frozen for the same reason ``MemoryEntry`` is frozen: an
    embedding is a value object -- once the provider returned it,
    nothing about it should change. A caller that needs a modified
    version constructs a new one (e.g., via ``dataclasses.replace``).

    Fields
    ------
    vectors
        One vector per input text, in input order. Internal
        consistency (all vectors share the same dimension) is
        enforced at construction time by ``__post_init__`` -- an
        adapter bug cannot produce a malformed result without
        raising.
    model_id
        The model identifier the provider actually used. MAY
        differ from the ``model_id`` argument the caller passed
        (some providers alias ``gpt-4-embed`` to
        ``text-embedding-3-small``). Always trust this field, not
        the request, for version tracking.
    model_version
        The provider's opaque version string for the model. See
        the module docstring's "Version tracking" section. Used
        as part of the vector index primary key.
    input_tokens
        Total tokens consumed across all inputs, as reported by
        the provider's usage field. 0 when the provider does not
        report it (some local models). Billed to the memory
        subsystem's cost ledger.
    cost_nano_usd
        Cost in nano-USD, computed from ``input_tokens`` and the
        model's per-token price. 0 for local models. Same unit
        as ``memory_jobs.cost_nano_usd`` -- no conversion at the
        boundary.
    """

    vectors: List[List[float]]
    model_id: str
    model_version: str
    input_tokens: int
    cost_nano_usd: int

    def __post_init__(self) -> None:
        """Enforce internal consistency at construction time.

        Two checks that catch every "the adapter forgot to validate"
        bug before the result escapes:

          1. All vectors must have the SAME length. Mixing
             dimensions within one result is meaningless -- the
             provider is broken, the adapter is broken, or both.

          2. ``input_tokens`` and ``cost_nano_usd`` must be
             non-negative. Negative values indicate a bug in cost
             arithmetic (e.g., an unsigned int overflow). Reject
             them here rather than letting them corrupt the cost
             ledger downstream.

        We do NOT validate ``len(vectors) == len(texts)`` here
        because the count of ``texts`` is not part of the result
        -- that cross-check is ``validate_against``'s job, called
        by the adapter before returning and (optionally) by the
        caller as a defense-in-depth check.
        """
        if self.input_tokens < 0:
            raise EmbeddingError(
                f"input_tokens must be >= 0, got {self.input_tokens}"
            )
        if self.cost_nano_usd < 0:
            raise EmbeddingError(
                f"cost_nano_usd must be >= 0, got {self.cost_nano_usd}"
            )
        if not self.vectors:
            return
        first_dim = len(self.vectors[0])
        for i, vec in enumerate(self.vectors):
            if len(vec) != first_dim:
                raise EmbeddingDimensionMismatchError(
                    f"vectors[{i}] has dimension {len(vec)}; "
                    f"vectors[0] has dimension {first_dim}; all "
                    f"vectors in one EmbeddingResult must share "
                    f"the same dimension"
                )

    def validate_against(
        self,
        expected_count: int,
        expected_dimensions: Optional[int] = None,
    ) -> None:
        """Raise ``EmbeddingError`` if the result does not match the request.

        Called by every adapter before returning, and by the caller
        as a defense-in-depth check. The redundant call is cheap
        (two length comparisons) and prevents a silently-wrong
        vector from corrupting the index if an adapter skips its
        own check.

        Parameters
        ----------
        expected_count
            The number of input texts the caller supplied. Must
            equal ``len(self.vectors)``.
        expected_dimensions
            The dimensions the caller expects. When not None,
            every vector must have exactly this length. Pass
            ``None`` for the first call in a session (dimensions
            unknown) and the actual dimensions on every call
            thereafter.

        Raises
        ------
        EmbeddingError
            When ``len(self.vectors) != expected_count``.
        EmbeddingDimensionMismatchError
            When ``expected_dimensions`` is provided and any
            vector's length differs.
        """
        if len(self.vectors) != expected_count:
            raise EmbeddingError(
                f"embed_batch returned {len(self.vectors)} vectors "
                f"for {expected_count} input texts; the provider "
                f"must return exactly one vector per input, in "
                f"input order"
            )
        if expected_dimensions is not None:
            for i, vec in enumerate(self.vectors):
                if len(vec) != expected_dimensions:
                    raise EmbeddingDimensionMismatchError(
                        f"vectors[{i}] has dimension {len(vec)}; "
                        f"expected {expected_dimensions}. This "
                        f"usually means the stored embeddings were "
                        f"created with a different model; a full "
                        f"reindex is required before searching."
                    )


# ---------------------------------------------------------------------------
# EmbeddingProvider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Structural protocol -- implement this for any embedding backend.

    Adapters (``provider.py``) implement this protocol; the
    retrieval layer (``vector_search.py``) programs against it.
    Concrete classes need not inherit from anything -- ``Protocol``
    is purely structural, so a test stub with the right methods
    satisfies it automatically.

    ``runtime_checkable`` enables ``isinstance(x,
    EmbeddingProvider)`` at runtime, which the registry (Task 4.4)
    uses to reject malformed plugins before registration. Note
    that ``isinstance`` only verifies METHOD PRESENCE, not
    signatures -- correct signatures are the adapter author's
    responsibility, verified by the adapter's own tests.
    """

    async def embed_batch(
        self,
        texts: Sequence[str],
        model_id: str,
    ) -> EmbeddingResult:
        """Embed a batch of texts in one provider call.

        Parameters
        ----------
        texts
            Input texts, in the order the caller wants vectors
            back. Empty texts are allowed by this protocol (some
            models accept them and return a zero-like vector).
            Individual adapters may reject them via
            provider-specific errors. The returned
            ``EmbeddingResult.vectors`` MUST be in the same order
            as ``texts``.
        model_id
            Provider-specific model identifier (e.g.,
            ``"text-embedding-3-small"``,
            ``"models/text-embedding-004"``,
            ``"nomic-embed-text"``). Adapters MUST use this exact
            value when calling the provider -- no aliasing or
            substitution. When the provider returns a different
            model_id, ``EmbeddingResult.model_id`` echoes the
            actual value back so callers can detect the mismatch.

        Returns
        -------
        EmbeddingResult
            Contains one vector per input text, plus the model id
            actually used, its version, the total input tokens,
            and the computed cost.

        Raises
        ------
        EmbeddingUnavailableError
            Provider unreachable, credentials missing, or
            model_id not offered by the provider.
        EmbeddingTransportError
            Transient network / 5xx / timeout. Callers may retry.
        EmbeddingBatchSizeError
            The batch exceeds the provider's documented maximum.
            Caller should split into smaller batches.
        EmbeddingError
            Any other failure (malformed response, internal
            error).
        """
        ...

    @property
    def dimensions(self) -> int:
        """Number of components per vector for this provider/model.

        Must be stable for a given ``(provider, model_id)`` pair --
        changing dimensions means changing models, which requires
        a full reindex.

        Used by the vector search layer to:

          * size the ``memory_vectors`` virtual table
            (``vec0(embedding FLOAT[N])`` -- N is fixed at table
            creation and cannot be altered without recreating the
            table), and

          * validate every ``EmbeddingResult`` against the
            expected shape (defense against accidental
            cross-model calls).
        """
        ...

    @property
    def model_version(self) -> str:
        """Opaque version string of the current model.

        See the module docstring's "Version tracking" section.
        Used as part of the vector index primary key
        ``(memory_id, model_id, model_version)`` so that switching
        models produces a new key namespace instead of mixing
        vectors from different vector spaces.
        """
        ...

    async def close(self) -> None:
        """Release provider resources (HTTP client, connection pool).

        Called during ``MemoryService`` shutdown. Must be
        idempotent -- the service may call ``close`` twice (once
        during normal shutdown, once during error cleanup) without
        raising.
        """
        ...


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EmbeddingBatchSizeError",
    "EmbeddingDimensionMismatchError",
    "EmbeddingError",
    "EmbeddingProvider",
    "EmbeddingResult",
    "EmbeddingTransportError",
    "EmbeddingUnavailableError",
]
