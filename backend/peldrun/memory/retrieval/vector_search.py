# backend/peldrun/memory/retrieval/vector_search.py
"""
PELDRUN Memory Platform - Vector Search Provider.

Semantic retrieval layer that combines three collaborators:

  1. **An embedding provider** (``peldrun.memory.embeddings.contracts``)
     that turns a text query into a vector.
  2. **A vector backend** (a small Protocol defined here) that
     performs the similarity search. The backend owns the storage
     engine (sqlite-vec, LanceDB, pgvector -- the provider does
     not care) and returns ``(memory_id, similarity)`` pairs.
  3. **A hydration backend** (another Protocol) that converts the
     ids the vector search produced into ``MemoryEntry`` instances,
     **filtering by scope** at the same time. Hydration is the
     layer that enforces isolation: the vector backend has no
     scope concept, and must never be trusted to filter.

The provider itself is intentionally small: it wires the three
collaborators together, computes nothing semantically interesting,
and knows no storage or scope specifics. Everything that requires
infrastructure lives behind a Protocol; everything that requires
policy lives in the caller (the active hybrid provider).

Architectural boundary
----------------------

This module lives in ``peldrun`` -- the framework-agnostic core.
It therefore MUST NOT import from ``omweb``. The two Protocols
below are the entire surface through which ``omweb`` participates:

  * ``VectorSearchBackend``  -- implemented by
    ``omweb.memory.storage.vector_repository.SqliteVecRepository``
    (the shape matches its ``search_in_namespace`` method
    verbatim, so no adapter is needed).
  * ``MemoryHydrationBackend`` -- implemented by
    ``omweb.memory.storage.vector_hydration.SqliteVectorHydration``
    (Task 4.5b), which reads from ``memories`` and applies scope
    filtering in SQL.

Callers (``MemoryService`` in ``omweb``) construct both and pass
them in. ``peldrun`` never knows that sqlite or sqlite-vec exist.

Return type contract
--------------------

``search()`` returns ``List[Tuple[MemoryEntry, float]]`` -- the
exact shape ``SqliteMemoryRepository._search_ranked_sync`` returns.
This is deliberate: the active hybrid provider must be able to
hand the output of either path to ``MemoryRanker`` (Phase 1-C)
without branching. ``float`` is a relevance score in ``[0, 1]``
where ``1.0`` is the best match -- same convention as the lexical
path's normalized BM25.

Note on the class name
----------------------

``SqliteVecProvider`` was named in the plan (§5.1) for the Phase 4
sqlite-vec backend. The implementation is already backend-agnostic
(Protocols all the way down); the name is retained for
compatibility with the plan's wording. A future refactor -- after
Phase 5 introduces a second vector backend -- can rename it to
``VectorSearchProvider`` without changing its behavior.

Empty and edge cases
--------------------

* **Empty query** (``""`` or whitespace-only) -> returns ``[]``
  without calling the embedding provider. There is nothing
  meaningful to embed, and spending an API call to find "things
  similar to nothing" is waste, not signal.

* **Empty ``scope_refs``** -> returns ``[]`` without any work.
  An empty scope set is "search nothing", not "search
  everything" -- matching the lexical path's behavior (see
  ``SqliteMemoryRepository._build_scope_filter``).

* **Dimension mismatch** between the query vector and the
  backend's expected dimension raises
  ``EmbeddingDimensionMismatchError`` from the backend. This is
  the correct behavior: a dimension mismatch means the model was
  switched without reindexing, and silently falling back to
  "no results" would hide a configuration error that needs
  operator attention.

* **Hits with no matching entry** (memory deleted between the
  vector search and the hydration query) are silently skipped.
  A deleted memory is not a search result; surfacing "N results
  where N-1 are gone" would be noise.

See also
--------

    * ``contracts.py`` (``peldrun.memory.embeddings``)
        -- ``EmbeddingProvider`` + ``EmbeddingResult`` +
           exception hierarchy
    * ``vector_repository.py`` (``omweb.memory.storage``)
        -- the concrete vector backend
    * ``vector_hydration.py`` (``omweb.memory.storage``, Task 4.5b)
        -- the concrete hydration backend
    * ``hybrid_provider.py`` (``omweb.memory.manager``)
        -- the active policy layer that consumes this
           provider and fuses its output with the lexical
           path's output
"""

from __future__ import annotations

import logging
from typing import List, Optional, Protocol, Tuple, runtime_checkable

from peldrun.memory.domain.entry import MemoryEntry
from peldrun.memory.embeddings.contracts import (
    EmbeddingDimensionMismatchError,
    EmbeddingProvider,
)


logger = logging.getLogger(
    "peldrun.memory.retrieval.vector_search"
)


# ---------------------------------------------------------------------------
# Protocols (structural interfaces)
# ---------------------------------------------------------------------------


@runtime_checkable
class VectorSearchBackend(Protocol):
    """Structural interface for a vector similarity backend.

    An implementation receives a query vector, runs its
    similarity search over the currently-active model's index,
    and returns ``(memory_id, similarity)`` pairs sorted by
    descending similarity.

    The signature matches
    ``omweb.memory.storage.vector_repository.SqliteVecRepository
    .search_in_namespace`` exactly, so that class satisfies this
    Protocol without modification (Python's structural typing
    does not require inheritance, and ``runtime_checkable`` only
    checks method presence).

    Contract notes
    --------------
    * The implementation MUST NOT filter by scope. Scope is a
      memory-domain concern; the vector layer has no concept of
      scope and cannot enforce isolation correctly even if it
      tried. Hydration (below) is the enforcement point.
    * ``similarity`` MUST be in ``[0, 1]`` where 1.0 is the best
      match. Backends that natively return a distance convert to
      similarity before returning (the sqlite-vec implementation
      does ``1.0 - distance``).
    * The returned list is empty when the index has no matches
      above ``min_similarity``, or when the index is empty.
    """

    async def search_in_namespace(
        self,
        *,
        query_vector: List[float],
        dimension: int,
        limit: int = 20,
        min_similarity: float = 0.5,
    ) -> List[Tuple[str, float]]:
        ...


@runtime_checkable
class MemoryHydrationBackend(Protocol):
    """Structural interface for turning memory ids into ``MemoryEntry``.

    This is the layer that enforces scope isolation. Its
    implementation MUST:

      1. Fetch every memory row whose ``memory_id`` is in the
         input list AND whose ``(scope_type, scope_id)`` matches
         one of the given scope refs.
      2. Return only rows that are retrievable (status ACTIVE,
         not expired).
      3. Preserve the caller's intent: a memory whose id is
         requested but whose scope does not match MUST be
         omitted from the returned list, not returned as a
         placeholder.

    The provider does not care HOW the implementation does this.
    The concrete implementation in ``omweb`` reads from the
    ``memories`` table in a single query with a scope-filter WHERE
    clause; a future in-memory implementation for tests does the
    same with dict lookups.

    Contract notes
    --------------
    * ``memory_ids`` may contain ids that no longer exist.
      Missing ids are silently skipped (the provider treats
      them as "deleted between search and hydration").
    * ``scope_refs`` uses the canonical ``"<type>:<id>"`` format
      (see ``MemoryScopeRef``). Implementations parse this
      format the same way ``SqliteMemoryRepository`` does.
    * The result is returned in ANY order. The provider
      re-orders hits to match the vector backend's similarity
      ordering; callers should not assume the hydration
      backend preserved input order.
    """

    async def get_entries_by_ids(
        self,
        memory_ids: List[str],
        scope_refs: List[str],
    ) -> List[MemoryEntry]:
        ...


# ---------------------------------------------------------------------------
# SqliteVecProvider
# ---------------------------------------------------------------------------


class SqliteVecProvider:
    """Combines an embedding provider, a vector backend, and hydration.

    See the module docstring for the design. In short: this class
    is a thin orchestrator that embeds the query, asks the vector
    backend for candidate ids, hydrates them via the hydration
    backend, and returns ``(entry, similarity)`` pairs ready for
    the active hybrid provider.

    Lifecycle
    ---------
    Construct one per (model, backend) pair. The embedded provider,
    vector backend, and hydration backend are owned by the caller
    (``MemoryService``) -- this class does not close them.

    Thread safety
    -------------
    All three injected collaborators are either thread-safe or
    async-safe by contract (see their respective docstrings). The
    provider itself holds no mutable state after construction, so
    concurrent ``search()`` calls are safe.
    """

    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        vector_backend: VectorSearchBackend,
        hydration_backend: MemoryHydrationBackend,
        model_id: str,
        model_version: str,
        dimensions: int,
    ) -> None:
        """Initialize the provider.

        Parameters
        ----------
        embedding_provider
            Turns text into a vector. Constructed and closed by
            the caller. MUST be bound to ``model_id`` -- calling
            ``embed_batch`` with a different model id raises an
            error from the concrete provider.
        vector_backend
            Runs the similarity search. Expected to already be
            configured with the same model namespace as
            ``model_id`` / ``model_version`` (the sqlite-vec
            backend holds one namespace at a time; see its
            docstring).
        hydration_backend
            Fetches ``MemoryEntry`` rows and applies scope
            filtering. The concrete implementation lives in
            ``omweb``; the provider does not care how it works.
        model_id
            Provider-reported model identifier. Stored so that
            ``embed_batch`` is called with the right argument.
        model_version
            Opaque version string. Stored for diagnostics and for
            the (future) ability to check that the backend's
            active namespace matches this one. Today the backend
            determines its own namespace; this field is a
            forward-compatible passthrough.
        dimensions
            Vector dimension for this model. Must equal
            ``len(embedding_provider.embed_batch(...).vectors[0])``
            and the backend's configured dimension. The
            provider validates the former on every search; the
            latter is the backend's contract.

        Raises
        ------
        ValueError
            When any required string is empty or ``dimensions``
            is not a positive int. These are caller bugs --
            failing at construction gives a clearer stack trace
            than failing on the first search.
        """
        for field_name, value in (
            ("model_id", model_id),
            ("model_version", model_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{field_name} must be a non-empty string, "
                    f"got {value!r}"
                )
        if (
            not isinstance(dimensions, int)
            or isinstance(dimensions, bool)
            or dimensions <= 0
        ):
            raise ValueError(
                f"dimensions must be a positive int, got "
                f"{dimensions!r}"
            )

        self._embedder = embedding_provider
        self._vector_backend = vector_backend
        self._hydration_backend = hydration_backend
        self._model_id = model_id.strip()
        self._model_version = model_version.strip()
        self._dimensions = dimensions

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        *,
        limit: int = 20,
        min_similarity: float = 0.0,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Search semantically for memories related to ``query``.

        Parameters
        ----------
        query
            Text to search for. Embedded via the injected
            provider. Empty/whitespace-only text returns ``[]``
            immediately (no embedding call).
        scope_refs
            Canonical ``"<type>:<id>"`` references (see
            ``MemoryScopeRef``) limiting the search. Empty list
            returns ``[]`` -- "search nothing" is the correct
            interpretation, matching the lexical path.
        limit
            Maximum number of hydrated results to return. Must
            be positive; values <= 0 return ``[]``.
        min_similarity
            Threshold in ``[0, 1]`` applied by the vector
            backend AND re-checked here (defensive -- a broken
            backend that ignores the threshold cannot leak
            low-quality hits past this provider). ``0.0`` keeps
            everything the backend returns; the settings layer
            is responsible for a meaningful default.

        Returns
        -------
        List[Tuple[MemoryEntry, float]]
            Pairs ordered by descending similarity. Empty when
            the query is blank, scopes are empty, the backend
            returns nothing, or hydration removes every hit
            (e.g. every candidate was in an excluded scope).

        Raises
        ------
        EmbeddingDimensionMismatchError
            When the provider's returned vector does not match
            ``self._dimensions``. This is a hard configuration
            error (model switched without reindex) that the
            caller MUST surface to the operator, not swallow.
        """
        if not isinstance(query, str):
            raise TypeError(
                f"query must be a string, got {type(query).__name__}"
            )
        stripped_query = query.strip()
        if not stripped_query:
            return []
        if not scope_refs:
            return []
        if limit <= 0:
            return []

        # ---- 1. Embed the query --------------------------------
        emb_result = await self._embedder.embed_batch(
            [stripped_query], self._model_id
        )
        if not emb_result.vectors:
            logger.warning(
                "SqliteVecProvider: embedding provider returned "
                "no vectors for query %r",
                stripped_query[:80],
            )
            return []
        query_vector = emb_result.vectors[0]
        if len(query_vector) != self._dimensions:
            raise EmbeddingDimensionMismatchError(
                f"Query embedding has dimension "
                f"{len(query_vector)}, expected {self._dimensions}. "
                f"Model {self._model_id!r} was probably switched "
                f"without reindexing; run POST /api/memory/reindex."
            )

        # ---- 2. Ask the vector backend for candidate ids -------
        hits = await self._vector_backend.search_in_namespace(
            query_vector=query_vector,
            dimension=self._dimensions,
            limit=limit,
            min_similarity=min_similarity,
        )
        if not hits:
            return []

        # ---- 3. Hydrate (with scope filtering) -----------------
        memory_ids = [mid for mid, _sim in hits]
        entries = await self._hydration_backend.get_entries_by_ids(
            memory_ids, scope_refs
        )
        if not entries:
            # Every hit was either deleted or out of scope.
            return []

        # ---- 4. Pair + re-order to match the similarity order --
        #
        # The hydration backend does not guarantee input order
        # (a SQL-based implementation returns rows in whatever
        # order the query planner produced). We therefore index
        # by memory_id and iterate ``hits`` (which IS ordered by
        # descending similarity) to rebuild the final result.
        by_id = {entry.memory_id: entry for entry in entries}
        result: List[Tuple[MemoryEntry, float]] = []
        for memory_id, similarity in hits:
            entry = by_id.get(memory_id)
            if entry is None:
                # Deleted or out-of-scope since the vector search.
                # Skip silently -- a memory the caller may not
                # see is not a search result.
                continue
            # Defensive re-check: the backend contract says it
            # honors ``min_similarity``, but a broken
            # implementation must not leak low-quality hits.
            if similarity < min_similarity:
                continue
            result.append((entry, similarity))

        logger.debug(
            "SqliteVecProvider: query=%r scope_refs=%d "
            "hits=%d hydrated=%d kept=%d",
            stripped_query[:80],
            len(scope_refs),
            len(hits),
            len(entries),
            len(result),
        )
        return result

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        """The model id this provider embeds with."""
        return self._model_id

    @property
    def model_version(self) -> str:
        """The version string this provider targets."""
        return self._model_version

    @property
    def dimensions(self) -> int:
        """The vector dimension of this provider's model."""
        return self._dimensions


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemoryHydrationBackend",
    "SqliteVecProvider",
    "VectorSearchBackend",
]
