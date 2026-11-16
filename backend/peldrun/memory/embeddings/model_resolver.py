# backend/peldrun/memory/embeddings/model_resolver.py
"""
PELDRUN Memory Platform - Embedding Model Resolver (in-memory).

Pure domain component. Holds a snapshot of "which embedding model
maps to which provider credentials" as an immutable map, resolved
ONCE by the caller (omweb layer) and injected at construction.

Architectural boundary
----------------------

This module intentionally does **no file I/O, no vault parsing, no
metadata scanning, and no HTTP probing.** Those are application-
layer concerns that belong in ``omweb`` — specifically:

  * reading ``models_vault.json`` and extracting credentials
    → ``omweb.routers.config_rtr`` (already implemented via
      ``_read_models_vault``) and
      ``omweb.memory.extraction.gateway._resolve_credentials``
      (already implemented, 5-container lookup);
  * reading ``models_metadata.json`` and enumerating embedding
    models
    → ``omweb.routers.config_rtr.get_embedding_models_endpoint``
      (already implemented, returns provider_id per model);
  * resolving a model key to its provider
    → ``omweb.routers.config_rtr._resolve_provider_for_model``
      (already implemented).

Duplicating any of the above in ``peldrun`` would:

  1. violate the layer boundary (``peldrun`` MUST NOT read the
     config directory — see §2.2 of the design document),
  2. create a second source of truth for vault parsing, which
     would inevitably drift from the omweb copy,
  3. make ``peldrun`` untestable in isolation (any unit test
     touching the resolver would need real files on disk).

Why keep a resolver class at all
--------------------------------

The retrieval layer (``SqliteVecProvider``, Task 4.5) and the
embedding worker (``embedding_worker.py``, Task 4.4) receive a
model key (a string) and need to construct an
``OpenAICompatEmbeddingProvider`` from it. Passing a raw ``dict``
around would lose type information and invite silent typos. The
resolver wraps the dict in a typed interface: ``resolve(key)``
returns a frozen ``ResolvedEmbeddingModel``, ``list_models()``
returns the full snapshot, and missing keys raise a typed error.

The resolver is a **value object**: no I/O, no async methods, no
side effects. Constructed by omweb, consumed by peldrun services.

Lifecycle
---------

    # omweb side (worker, MemoryService, test fixture, ...)
    snapshot = {...}  # built via config_rtr helpers
    resolver = EmbeddingModelResolver(snapshot)

    # peldrun side (SqliteVecProvider, worker)
    model = resolver.resolve("openai::text-embedding-3-small")
    provider = OpenAICompatEmbeddingProvider(
        base_url=model.base_url,
        api_key=model.api_key,
        model_id=model.model_id,
        model_version=model.model_version,
        dimensions=model.known_dimensions,
    )

See also
--------

    * ``contracts.py``            — protocol + result value object
    * ``provider.py``             — OpenAICompatEmbeddingProvider
    * ``omweb/routers/config_rtr.py``
                                  — vault + metadata discovery
    * ``omweb/memory/extraction/gateway.py``
                                  — vault credential resolution
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional


logger = logging.getLogger(
    "peldrun.memory.embeddings.model_resolver"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class EmbeddingModelNotConfiguredError(Exception):
    """Raised by ``resolve()`` when a model key is not in the snapshot.

    Distinct from ``EmbeddingUnavailableError`` (in ``contracts.py``)
    because the semantics differ:

      * ``EmbeddingModelNotConfiguredError`` -- the caller asked for
        a model the resolver was never told about. Either the key
        is wrong, or the omweb layer has not yet populated this
        model into the snapshot. It is a **programming/config**
        error, not a provider-unreachable error.

      * ``EmbeddingUnavailableError`` -- the provider exists in the
        snapshot but is not reachable / not authenticated. A
        **runtime** error with retry/operator-intervention
        semantics.

    Callers (worker, retrieval router) catch this specifically to
    log "the model is not configured" without masking genuine
    provider failures.
    """


# ---------------------------------------------------------------------------
# Resolved value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedEmbeddingModel:
    """One embedding model with all credentials resolved.

    Fully immutable. All fields are read-only at construction.
    Built by the omweb layer from vault + metadata; consumed by
    peldrun's provider constructor and vector search.

    Attributes
    ----------
    model_key
        The key the caller uses to identify the model (e.g.
        ``"openai::text-embedding-3-small"`` or a bare
        ``"text-embedding-3-small"``). Echoed back so callers can
        correlate results with their input list.
    model_id
        The bare model identifier the provider expects, with any
        ``provider::`` prefix stripped.
    provider_id
        The provider id that owns the model (e.g. ``"openai"``).
        Required — an entry without a provider cannot be
        constructed into a provider instance.
    base_url
        The provider endpoint, UN-normalized (the provider
        constructor normalizes it). ``None`` for providers using
        the SDK's default endpoint (rare; usually only OpenAI
        itself).
    api_key
        The bearer token. ``None`` for local servers (LM Studio,
        Ollama) that accept unauthenticated requests.
    model_version
        Opaque version string used in the vector index primary
        key ``(memory_id, model_id, model_version)``. Defaults to
        ``model_id`` at construction time when not supplied.
    known_dimensions
        Pre-probed vector dimension. ``None`` means "not yet
        known — the caller must probe via
        ``OpenAICompatEmbeddingProvider.probe_dimensions``
        before constructing a provider that will write to a
        ``vec0(FLOAT[N])`` table". This is intentional: the
        resolver does not probe (that would be I/O), and the
        dimension is a property of the (provider, model) pair
        that only an API call can determine.
    source
        Provenance of the "is_embedding" designation:
          * ``"manual"``        -- user marked it in the UI
          * ``"provider_live"`` -- auto-detected by a scan
    extra_headers
        Provider-specific HTTP headers (e.g. OpenRouter's
        ``HTTP-Referer``). Empty dict by default; the omweb
        layer fills it when the vault entry carries
        ``metadataEndpoint`` or similar.
    """

    model_key: str
    model_id: str
    provider_id: str
    base_url: Optional[str]
    api_key: Optional[str]
    model_version: str
    known_dimensions: Optional[int]
    source: str
    extra_headers: Dict[str, str]

    def __post_init__(self) -> None:
        """Enforce the small set of invariants callers depend on.

        These are checked once at construction so that downstream
        code can trust the value without re-checking:

          1. ``model_key``, ``model_id``, ``provider_id`` must be
             non-empty strings. A blank provider id would force
             the caller to re-validate before use; catching it
             at construction is cleaner.

          2. ``model_version`` must be a non-empty string. An
             empty version would produce an unbindable primary
             key in the vector index.

          3. ``known_dimensions``, when present, must be a
             positive int. A non-positive dimension cannot size a
             vec0 table.

        The dataclass is frozen, so this validator runs exactly
        once per instance.
        """
        for field_name in ("model_key", "model_id", "provider_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{field_name} must be a non-empty string, "
                    f"got {value!r}"
                )
        if (
            not isinstance(self.model_version, str)
            or not self.model_version.strip()
        ):
            raise ValueError(
                f"model_version must be a non-empty string, "
                f"got {self.model_version!r}"
            )
        if self.known_dimensions is not None:
            if (
                not isinstance(self.known_dimensions, int)
                or isinstance(self.known_dimensions, bool)
                or self.known_dimensions <= 0
            ):
                raise ValueError(
                    f"known_dimensions must be a positive int or "
                    f"None, got {self.known_dimensions!r}"
                )
        if not isinstance(self.extra_headers, dict):
            raise ValueError(
                f"extra_headers must be a dict, got "
                f"{type(self.extra_headers).__name__}"
            )


# ---------------------------------------------------------------------------
# Resolver (in-memory registry)
# ---------------------------------------------------------------------------


class EmbeddingModelResolver:
    """In-memory registry of resolved embedding models.

    Construct with a snapshot dict mapping ``model_key`` to
    ``ResolvedEmbeddingModel``. The caller (omweb) builds the
    snapshot from vault + metadata; this class knows nothing about
    those files.

    Instances are immutable after construction. Every method is
    synchronous, side-effect free, and safe to call concurrently
    from any number of tasks on any event loop.

    Lifecycle
    ---------
    One instance per process (or per worker), rebuilt whenever the
    operator changes the embedding configuration. The omweb layer
    rebuilds by constructing a new resolver from fresh vault +
    metadata state and swapping the reference atomically. There is
    no ``reload()`` method — that would require this class to know
    about file paths, defeating the whole point of the split.
    """

    def __init__(
        self,
        snapshot: Mapping[str, ResolvedEmbeddingModel],
        *,
        default_model_key: Optional[str] = None,
    ) -> None:
        """Initialize the resolver.

        Parameters
        ----------
        snapshot
            Map from ``model_key`` to a fully resolved model.
            Copied into an internal dict so external mutations
            cannot leak in. Each value is frozen (the value
            object), so no defensive copying of the values is
            needed.
        default_model_key
            Optional key to return when a caller asks for "the
            default embedding model" without naming one. Must be
            present in ``snapshot`` (a default that does not
            exist is a caller bug, not a "resolver empty"
            condition — catching it here gives a clearer error
            than a later ``resolve(None)`` failure).

        Raises
        ------
        ValueError
            * A snapshot key does not match its value's
              ``model_key`` (silent drift like that would make
              ``list_models()`` return rows whose key differs
              from the value they index).
            * ``default_model_key`` is given but not present in
              ``snapshot``.
            * A snapshot value is not a ``ResolvedEmbeddingModel``
              (e.g. someone passed the raw dict by mistake).
        """
        # Copy + validate the snapshot.
        self._models: Dict[str, ResolvedEmbeddingModel] = {}
        for key, value in snapshot.items():
            if not isinstance(value, ResolvedEmbeddingModel):
                raise ValueError(
                    f"snapshot[{key!r}] must be a "
                    f"ResolvedEmbeddingModel, got "
                    f"{type(value).__name__}. Build the snapshot "
                    f"via the omweb helper, not from raw dicts."
                )
            if value.model_key != key:
                raise ValueError(
                    f"snapshot key {key!r} does not match "
                    f"ResolvedEmbeddingModel.model_key "
                    f"{value.model_key!r}; keys must be "
                    f"consistent so callers can look models up "
                    f"by the key they were given."
                )
            self._models[key] = value

        if default_model_key is not None:
            if default_model_key not in self._models:
                raise ValueError(
                    f"default_model_key {default_model_key!r} is "
                    f"not present in the snapshot. The default "
                    f"must always be a valid model; use "
                    f"snapshot[default] to construct and pass "
                    f"the same key, or omit default_model_key "
                    f"entirely."
                )
        self._default_model_key = default_model_key

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self, model_key: str) -> ResolvedEmbeddingModel:
        """Return the resolved model for ``model_key``.

        Parameters
        ----------
        model_key
            The key to look up. Must exactly match a snapshot key
            (no fuzzy matching, no prefix stripping — see the
            ``EmbeddingModelResolver`` design rationale).

        Returns
        -------
        ResolvedEmbeddingModel
            The frozen value object for that key.

        Raises
        ------
        EmbeddingModelNotConfiguredError
            When ``model_key`` is not in the snapshot. The
            message lists the currently known keys (bounded to
            the first few) so an operator can see what IS
            configured.
        """
        model = self._models.get(model_key)
        if model is None:
            known = sorted(self._models.keys())
            preview = ", ".join(repr(k) for k in known[:5])
            if len(known) > 5:
                preview += f", ... ({len(known) - 5} more)"
            raise EmbeddingModelNotConfiguredError(
                f"Embedding model {model_key!r} is not in the "
                f"resolver snapshot. Known models: "
                f"[{preview}]."
            )
        return model

    def resolve_default(self) -> ResolvedEmbeddingModel:
        """Return the default model.

        Raises
        ------
        EmbeddingModelNotConfiguredError
            When no default was configured at construction.
            Callers that want to handle "no default" gracefully
            should check ``has_default()`` first.
        """
        if self._default_model_key is None:
            raise EmbeddingModelNotConfiguredError(
                "No default embedding model is configured. "
                "Set one in Settings -> Memory -> Embeddings, "
                "or pass an explicit model_key to resolve()."
            )
        return self._models[self._default_model_key]

    def has_default(self) -> bool:
        """True when a default model was configured."""
        return self._default_model_key is not None

    @property
    def default_model_key(self) -> Optional[str]:
        """The default key, or None when unset."""
        return self._default_model_key

    def list_models(self) -> List[ResolvedEmbeddingModel]:
        """Return every model in the snapshot, deterministically ordered.

        Sort order: ``(provider_id, model_id)``. This is stable
        across calls and matches the ordering used by omweb's
        ``GET /api/config/embedding-models`` endpoint, so the
        resolver view and the UI view agree.
        """
        return sorted(
            self._models.values(),
            key=lambda m: (m.provider_id, m.model_id),
        )

    def is_empty(self) -> bool:
        """True when the snapshot is empty.

        A service starting with no embedding models configured
        uses this to decide whether to skip embedding-related
        initialization entirely, rather than discovering the
        emptiness through a raised exception from ``resolve()``.
        """
        return not self._models

    def __len__(self) -> int:
        """Number of models in the snapshot."""
        return len(self._models)

    def __contains__(self, model_key: object) -> bool:
        """Support ``"key" in resolver`` checks."""
        return model_key in self._models

    def __repr__(self) -> str:
        return (
            f"EmbeddingModelResolver("
            f"models={len(self._models)}, "
            f"default={self._default_model_key!r})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EmbeddingModelNotConfiguredError",
    "EmbeddingModelResolver",
    "ResolvedEmbeddingModel",
]
