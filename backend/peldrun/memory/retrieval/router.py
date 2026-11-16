# backend/peldrun/memory/retrieval/router.py
"""
PELDRUN Memory Platform - Retrieval Router.

Policy layer that dispatches a memory search to the right
strategy based on ``MemoryRetrievalSettings.mode`` and falls back
gracefully when the chosen strategy cannot run.

Layered position
----------------

    MemoryService            -- orchestration, settings injection
    MemoryRetrievalRouter    -- THIS CLASS (policy)
      ├── LexicalSearchBackend   (SqliteMemoryRepository)
      ├── RankedVectorBackend    (SqliteVecProvider)
      └── HybridSearch           (fuses the two above)
    ── Domain layer ──
    MemoryEntry, MemoryRetrievalSettings

The router is the ONLY place that knows about the four retrieval
modes (``lexical`` / ``vector`` / ``hybrid`` / ``automatic``).
Every backend below it is a single-strategy component; every
caller above it (``MemoryService``) just calls ``search()`` and
gets back the same shape no matter which strategy ran.

Fallback policy
---------------

The router never silently returns "no results" when a viable
alternative exists. Specifically:

  1. **Config unavailable** -- ``mode='vector'`` (or
     ``'hybrid'``) but no ``vector_backend`` was injected OR
     ``embedding_enabled=False``. The router logs at INFO and
     falls back to lexical. The user asked for a mode they have
     not configured -- the correct answer is "do the best you
     can with what's available", not "return empty".

  2. **Runtime error** -- the vector backend raised. The
     router logs at WARNING and falls back to lexical. A
     transient vector-backend failure must never break retrieval
     when lexical results exist.

  3. **Empty result** -- the vector backend returned ``[]``.
     The router does NOT fall back -- the user asked for vector
     mode, and an empty result is a valid answer (no memories
     are semantically similar to the query). Falling back to
     lexical here would silently override the user's explicit
     choice.

Rule 3 is the important asymmetry: fallback is for UNAVAILABILITY,
not for "I don't have a match". A user who deliberately chooses
vector-only mode expects vector-only semantics.

Automatic mode
--------------

``mode='automatic'`` picks a strategy per query:

  * **Short queries** (fewer than ``SHORT_QUERY_THRESHOLD``
    whitespace-separated words) -> lexical. Rationale:
    short queries tend to be keywords or exact phrases where
    BM25 outperforms cosine similarity. They are also the
    queries most likely to hit long-tail rare tokens that
    lexical search handles precisely.

  * **Longer queries** -> hybrid. Rationale: multi-word
    natural-language questions benefit from semantic matching,
    and the fused result keeps lexical's precision on
    proper nouns / technical terms.

The threshold is intentionally simple (``3``). A learned
threshold would require labeled data this deployment does not
have; the plan (§5.6) already uses ``< 3`` in its pseudocode.
A future Phase 5+ deployment can replace it with a
model-based classifier without touching the router's contract.

Why the router holds settings instead of a settings getter
----------------------------------------------------------

The router captures ``MemoryRetrievalSettings`` at construction
time. A settings change produces a NEW router instance (the
``MemoryService`` lifecycle manager pattern). This means:

  * every search during one router's lifetime uses the same
    settings -- no half-and-half reads mid-search,
  * the router has no dependency on the settings store, no
    I/O, no async method just to read a field,
  * tests can construct a router with fixed settings without
    mocking the store.

This mirrors ``MemoryService.reload_settings``'s behavior in
Phase 1-C: a new router replaces the old one atomically, so an
in-flight search either completes under the old settings or
never started. Neither path yields an inconsistent view.

See also
--------

    * ``hybrid_search.py``  -- ``HybridSearch``,
                                ``LexicalSearchBackend``,
                                ``RankedVectorBackend``
    * ``vector_search.py``  -- ``SqliteVecProvider`` (the
                                concrete ``RankedVectorBackend``)
    * ``fusion.py``         -- ``reciprocal_rank_fusion``,
                                ``weighted_fusion``
    * ``settings.py`` (``peldrun.memory.domain``)
                             -- ``MemoryRetrievalSettings``
    * ``repository.py`` (``omweb.memory.storage``)
                             -- ``SqliteMemoryRepository``
                                (the concrete lexical backend)
"""

from __future__ import annotations

import logging
from functools import partial
from typing import List, Optional, Tuple

from peldrun.memory.domain.entry import MemoryEntry
from peldrun.memory.domain.settings import MemoryRetrievalSettings
from peldrun.memory.retrieval.fusion import (
    reciprocal_rank_fusion,
    weighted_fusion,
)
from peldrun.memory.retrieval.hybrid_search import (
    FusionFn,
    HybridSearch,
    LexicalSearchBackend,
    RankedVectorBackend,
)


logger = logging.getLogger(
    "peldrun.memory.retrieval.router"
)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class MemoryRetrievalRouter:
    """Dispatch memory queries to the configured retrieval strategy.

    See the module docstring for the layered position, fallback
    policy, and the rationale behind capturing settings at
    construction time.

    Lifecycle
    ---------
    Construct once per (settings, backends) tuple. Reuse across
    queries. The router holds no mutable state after construction
    -- concurrent ``search()`` calls are safe.

    Example
    -------
    ::

        router = MemoryRetrievalRouter(
            lexical_backend=memory_repo,
            vector_backend=vec_provider,   # None if unavailable
            retrieval_settings=settings.retrieval,
            default_limit=settings.default_limit,
            embedding_enabled=settings.embedding.enabled,
        )
        results = await router.search(
            "coffee preferences", ["project:p1"], limit=10,
        )
    """

    #: Query-length threshold used by ``automatic`` mode. Queries
    #: with fewer than this many whitespace-separated words use
    #: lexical; longer queries use hybrid. Matches the plan's
    #: §5.6 pseudocode (``len(query.split()) < 3``).
    SHORT_QUERY_THRESHOLD: int = 3

    def __init__(
        self,
        *,
        lexical_backend: LexicalSearchBackend,
        retrieval_settings: MemoryRetrievalSettings,
        vector_backend: Optional[RankedVectorBackend] = None,
        default_limit: int = 5,
        embedding_enabled: bool = False,
    ) -> None:
        """Initialize the router.

        Parameters
        ----------
        lexical_backend
            Always required -- the lexical (FTS5/BM25) source.
            Every fallback path ends here, so it must not be
            ``None``.
        retrieval_settings
            The current retrieval configuration. Captured by
            reference -- callers who mutate the settings object
            mid-flight will see the change (the router does not
            deep-copy). ``MemoryService`` follows the
            "construct-new-router-on-settings-change"
            convention, so this is safe in practice.
        vector_backend
            Optional. When ``None``, ``mode='vector'`` and
            ``mode='hybrid'`` fall back to lexical (see module
            docstring's fallback policy). When not ``None``, a
            ``HybridSearch`` is constructed eagerly so that a
            misconfigured ``fusion_fn`` fails at construction
            time, not on the first query.
        default_limit
            Used when ``search(limit=None)``. Matches
            ``MemorySettings.default_limit`` in production.
        embedding_enabled
            ``MemoryEmbeddingSettings.enabled`` from the same
            settings tree. The router does NOT read
            ``settings.embedding.enabled`` itself -- it accepts
            the flag as a parameter so that the caller
            (``MemoryService``) has a single, explicit moment
            where it reads the whole settings tree and hands
            the router a fully-specified configuration. This
            also lets tests exercise the "vector mode without
            embedding enabled" path without constructing a
            full settings object.

        Raises
        ------
        ValueError
            When ``default_limit`` is not positive.
        TypeError
            When ``lexical_backend`` or ``retrieval_settings``
            is ``None``.
        """
        if lexical_backend is None:
            raise TypeError(
                "lexical_backend is required (every fallback path "
                "depends on it)"
            )
        if retrieval_settings is None:
            raise TypeError(
                "retrieval_settings is required"
            )
        if (
            not isinstance(default_limit, int)
            or isinstance(default_limit, bool)
            or default_limit <= 0
        ):
            raise ValueError(
                f"default_limit must be a positive int, got "
                f"{default_limit!r}"
            )

        self._lexical = lexical_backend
        self._vector = vector_backend
        self._settings = retrieval_settings
        self._default_limit = default_limit
        self._embedding_enabled = bool(embedding_enabled)

        # Build the hybrid searcher eagerly. See __init__
        # docstring for the rationale. When vector_backend is
        # None, no hybrid searcher is possible -- hybrid mode
        # will fall back to lexical.
        self._hybrid: Optional[HybridSearch] = None
        if vector_backend is not None:
            self._hybrid = HybridSearch(
                lexical_backend=lexical_backend,
                vector_backend=vector_backend,
                fusion_fn=self._build_fusion_fn(),
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        *,
        limit: Optional[int] = None,
        min_lexical_score: Optional[float] = None,
        min_vector_similarity: Optional[float] = None,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Dispatch ``query`` to the configured retrieval strategy.

        Parameters
        ----------
        query
            Text to search for. Empty/whitespace returns ``[]``
            without any backend call.
        scope_refs
            Canonical ``"<type>:<id>"`` references. Empty list
            returns ``[]``.
        limit
            Maximum number of results. ``None`` uses
            ``default_limit``. ``<= 0`` returns ``[]``.
        min_lexical_score
            Override for ``settings.min_lexical_score``.
            ``None`` uses the settings value.
        min_vector_similarity
            Override for ``settings.min_vector_similarity``.
            ``None`` uses the settings value.

        Returns
        -------
        List[Tuple[MemoryEntry, float]]
            Results ordered best-first. The float's meaning
            depends on the strategy that ran:

              * ``lexical``  -- normalized BM25 in ``[0, 1]``;
              * ``vector``   -- cosine similarity in ``[0, 1]``;
              * ``hybrid``   -- fused score, range depends on
                                the fusion algorithm (see
                                ``fusion.py``).

            Callers that need a consistent numeric meaning
            across modes should only look at the ORDER, not the
            absolute float value.

        Raises
        ------
        ValueError
            When ``settings.mode`` is not one of the four known
            values (defense against a settings schema regression
            or a manual edit that bypassed Pydantic).
        """
        if not isinstance(query, str):
            raise TypeError(
                f"query must be a string, got "
                f"{type(query).__name__}"
            )
        stripped_query = query.strip()
        if not stripped_query:
            return []
        if not scope_refs:
            return []

        effective_limit = (
            limit if limit is not None else self._default_limit
        )
        if effective_limit <= 0:
            return []

        effective_min_lex = (
            min_lexical_score
            if min_lexical_score is not None
            else self._settings.min_lexical_score
        )
        effective_min_vec = (
            min_vector_similarity
            if min_vector_similarity is not None
            else self._settings.min_vector_similarity
        )

        mode = self._settings.mode
        if mode == "lexical":
            return await self._dispatch_lexical(
                stripped_query,
                scope_refs,
                effective_limit,
                effective_min_lex,
            )
        if mode == "vector":
            return await self._dispatch_vector(
                stripped_query,
                scope_refs,
                effective_limit,
                effective_min_vec,
            )
        if mode == "hybrid":
            return await self._dispatch_hybrid(
                stripped_query,
                scope_refs,
                effective_limit,
                effective_min_lex,
                effective_min_vec,
            )
        if mode == "automatic":
            return await self._dispatch_automatic(
                stripped_query,
                scope_refs,
                effective_limit,
                effective_min_lex,
                effective_min_vec,
            )

        # Unreachable under normal operation -- Pydantic
        # constrains mode to the four known values. This branch
        # exists to catch a manual edit to memory_settings.json
        # or a future schema regression that bypasses Pydantic.
        raise ValueError(
            f"Unknown retrieval mode {mode!r}. Expected one of "
            f"'lexical', 'vector', 'hybrid', 'automatic'."
        )

    def classify_query(self, query: str) -> str:
        """Return the effective strategy for ``query`` under the current settings.

        Useful for tests and diagnostics: it resolves the
        ``automatic`` threshold without performing any search.

        Returns ``"lexical"``, ``"vector"``, or ``"hybrid"``
        (never ``"automatic"`` -- that pseudo-mode is resolved
        to one of the two concrete strategies).
        """
        mode = self._settings.mode
        if mode == "automatic":
            word_count = len(query.split())
            if word_count < self.SHORT_QUERY_THRESHOLD:
                return "lexical"
            return "hybrid"
        return mode

    # ------------------------------------------------------------------
    # Internal -- fusion fn
    # ------------------------------------------------------------------

    def _build_fusion_fn(self) -> FusionFn:
        """Choose a fusion function based on the current settings.

        The choice is:

          * ``vector_alpha == 0.5`` (default) -> RRF with
            ``settings.rrf_k``. Rationale: equal-weight fusion is
            exactly what RRF optimizes for; the operator has not
            expressed a preference between the two sources, and
            RRF's scale-free behavior is the correct default
            (see ``fusion.py``'s design docstring).

          * ``vector_alpha != 0.5`` -> weighted fusion with
            ``alpha=1 - vector_alpha``. Rationale: a non-default
            ``vector_alpha`` is an explicit statement of
            preference ("I want more lexical" or "I want more
            vector"). Weighted fusion is the only algorithm in
            this module that respects a numeric weight.

        ``weighted_fusion``'s ``alpha`` is the LEXICAL weight;
        ``vector_alpha`` is the VECTOR weight. Hence the
        ``1.0 - vector_alpha`` translation. The operator-facing
        name matches the intuitive reading ("alpha is the vector
        weight"); the internal name is preserved so that
        ``weighted_fusion``'s docstring stays accurate.
        """
        if abs(self._settings.vector_alpha - 0.5) < 1e-9:
            return partial(
                reciprocal_rank_fusion,
                k=self._settings.rrf_k,
            )
        return partial(
            weighted_fusion,
            alpha=1.0 - self._settings.vector_alpha,
        )

    # ------------------------------------------------------------------
    # Internal -- dispatch helpers
    # ------------------------------------------------------------------

    async def _dispatch_lexical(
        self,
        query: str,
        scope_refs: List[str],
        limit: int,
        min_lexical_score: float,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Dispatch to the lexical backend; apply threshold post-hoc.

        The lexical backend (``SqliteMemoryRepository``) does not
        accept a ``min_score`` parameter -- its contract is "give
        me the ranked list, filter downstream". We filter here so
        the router's output is consistent across all modes: an
        item below ``min_lexical_score`` never reaches the
        caller.
        """
        results = await self._lexical.search_ranked(
            query, scope_refs, limit
        )
        if min_lexical_score > 0.0:
            results = [
                (e, s) for e, s in results
                if s >= min_lexical_score
            ]
        return results

    async def _dispatch_vector(
        self,
        query: str,
        scope_refs: List[str],
        limit: int,
        min_vector_similarity: float,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Dispatch to the vector backend, with fallback on config/error.

        See the module docstring's "Fallback policy" section for
        the full rationale. In summary:

          * Unavailable (config) -> fallback to lexical.
          * Exception (runtime) -> fallback to lexical.
          * Empty result       -> return empty (the user asked
            for vector mode and got no matches).
        """
        # Config-level check: did MemoryService hand us a vector
        # backend, and did the user enable embedding?
        if self._vector is None or not self._embedding_enabled:
            logger.info(
                "Router: vector mode requested but vector backend "
                "is unavailable (vector_backend=%s, "
                "embedding_enabled=%s); falling back to lexical.",
                self._vector is not None,
                self._embedding_enabled,
            )
            return await self._dispatch_lexical(
                query, scope_refs, limit,
                self._settings.min_lexical_score,
            )

        try:
            return await self._vector.search(
                query,
                scope_refs,
                limit=limit,
                min_similarity=min_vector_similarity,
            )
        except Exception as exc:
            logger.warning(
                "Router: vector search raised %s: %s; falling "
                "back to lexical.",
                type(exc).__name__,
                exc,
            )
            return await self._dispatch_lexical(
                query, scope_refs, limit,
                self._settings.min_lexical_score,
            )

    async def _dispatch_hybrid(
        self,
        query: str,
        scope_refs: List[str],
        limit: int,
        min_lexical_score: float,
        min_vector_similarity: float,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Dispatch to hybrid search, with fallback on config/error.

        Same fallback policy as ``_dispatch_vector``. The
        difference is that ``HybridSearch`` already handles the
        "vector is empty but lexical has results" case
        internally (fusion degenerates to lexical-only
        ranking), so this method does not need a special branch
        for it.
        """
        if self._hybrid is None or not self._embedding_enabled:
            logger.info(
                "Router: hybrid mode requested but hybrid searcher "
                "is unavailable (hybrid=%s, "
                "embedding_enabled=%s); falling back to lexical.",
                self._hybrid is not None,
                self._embedding_enabled,
            )
            return await self._dispatch_lexical(
                query, scope_refs, limit, min_lexical_score,
            )

        try:
            return await self._hybrid.search(
                query,
                scope_refs,
                limit=limit,
                min_lexical_score=min_lexical_score,
                min_vector_similarity=min_vector_similarity,
            )
        except Exception as exc:
            logger.warning(
                "Router: hybrid search raised %s: %s; falling "
                "back to lexical.",
                type(exc).__name__,
                exc,
            )
            return await self._dispatch_lexical(
                query, scope_refs, limit, min_lexical_score,
            )

    async def _dispatch_automatic(
        self,
        query: str,
        scope_refs: List[str],
        limit: int,
        min_lexical_score: float,
        min_vector_similarity: float,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Dispatch based on query length (see module docstring)."""
        effective = self.classify_query(query)
        if effective == "lexical":
            logger.debug(
                "Router: automatic -> lexical (query has %d "
                "word(s), threshold=%d)",
                len(query.split()),
                self.SHORT_QUERY_THRESHOLD,
            )
            return await self._dispatch_lexical(
                query, scope_refs, limit, min_lexical_score,
            )
        # effective == "hybrid" (classify_query never returns
        # "automatic").
        logger.debug(
            "Router: automatic -> hybrid (query has %d "
            "word(s), threshold=%d)",
            len(query.split()),
            self.SHORT_QUERY_THRESHOLD,
        )
        return await self._dispatch_hybrid(
            query, scope_refs, limit,
            min_lexical_score, min_vector_similarity,
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def settings(self) -> MemoryRetrievalSettings:
        """The captured settings. Read-only by convention."""
        return self._settings

    @property
    def embedding_enabled(self) -> bool:
        """Whether the router was told the embedding subsystem is active."""
        return self._embedding_enabled

    @property
    def has_vector_backend(self) -> bool:
        """Whether a vector backend was injected."""
        return self._vector is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["MemoryRetrievalRouter"]
