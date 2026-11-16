# backend/peldrun/memory/retrieval/hybrid_search.py
"""
PELDRUN Memory Platform - Hybrid Search Orchestrator.

Combines lexical (FTS5/BM25) and vector (embedding cosine) search
results using one of the two fusion strategies in ``fusion.py``.
The class itself is deliberately thin: it calls both backends,
passes their outputs to a configurable fusion function, and maps
the fused result back to ``MemoryEntry`` objects.

Layered architecture
--------------------

The Phase 4 retrieval stack, bottom-up:

    SqliteVecRepository       -- sqlite-vec index + vector MATCH
    SqliteVecProvider         -- embeds query + hydrates hits
                                  (``vector_search.py``)
    SqliteMemoryRepository    -- FTS5 search + ranked output
                                  (existing, Phase 1-C)
    HybridSearch              -- fuses the two above
                                  (this module)
    MemoryRetrievalRouter     -- dispatches between lexical /
                                  vector / hybrid / automatic
                                  based on settings (Task 4.9)

``HybridSearch`` is the boundary between "how to fuse" (its job)
and "whether to fuse" (the router's job). It therefore:

  * accepts a ``fusion_fn`` injection point instead of deciding
    between RRF and weighted on its own -- the router decides;
  * lets exceptions from either backend propagate -- the router
    decides whether to fall back to single-source search;
  * applies the caller-provided ``min_lexical_score`` and
    ``min_vector_similarity`` thresholds BEFORE fusion (so
    low-quality hits do not "waste" fusion slots).

What HybridSearch does NOT do
------------------------------

* **No policy.** It does not read settings, does not look at
  ``MemoryRetrievalSettings.mode``, does not decide "should we
  run hybrid right now?". If called, it runs hybrid.
* **No fallback.** If the vector backend raises, the exception
  propagates. The caller (router) can catch and re-run in
  lexical-only mode.
* **No caching.** Every call embeds the query and hits the
  backend. The router (Task 4.9) can add caching later if
  profiling shows it matters.
* **No reranking.** The plan's Phase 5 adds a cross-encoder
  reranker on top of the fused top-K. Not wired here -- the
  router will insert it between ``search()`` and its response
  when ``rerank_enabled=True``.

Determinism and equality
------------------------

The output order is determined entirely by the fusion function.
Given the same inputs and the same fusion function, the output
is identical every time (see ``fusion.py``'s design invariant
#2). This is critical for golden-dataset tests (Task 4.12).

The fused scores returned alongside each ``MemoryEntry`` are
algorithm-specific:

  * RRF: ``[0, 0.033 * N]`` for ``k=60`` where N is the longer
    input list's length. Do NOT compare against lexical
    thresholds (which live in ``[0, 1]``).
  * Weighted: ``[0, 1]``. Comparable to the lexical path's
    normalized scores.

The router never compares fused scores against a threshold --
the per-source thresholds were already applied before fusion.
Fused scores are for ordering only.

See also
--------

    * ``vector_search.py``  -- ``SqliteVecProvider``
    * ``fusion.py``         -- ``reciprocal_rank_fusion``,
                                ``weighted_fusion``
    * ``router.py``         -- ``MemoryRetrievalRouter`` (Task 4.9)
    * ``repository.py`` (``omweb.memory.storage``)
                            -- ``SqliteMemoryRepository``
                                (lexical source)
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from peldrun.memory.domain.entry import MemoryEntry
from peldrun.memory.retrieval.fusion import (
    FusedItem,
    RankedItem,
    reciprocal_rank_fusion,
)


logger = logging.getLogger(
    "peldrun.memory.retrieval.hybrid_search"
)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class LexicalSearchBackend(Protocol):
    """Structural interface for a lexical (FTS5/BM25) search backend.

    Satisfied by ``omweb.memory.storage.repository.SqliteMemoryRepository``
    -- its ``search_ranked`` method has the matching signature and
    return shape. The provider does not care HOW the backend
    searches; it only needs the ``(entry, score)`` pairs in rank
    order, best first.
    """

    async def search_ranked(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = ...,
    ) -> List[Tuple[MemoryEntry, float]]:
        ...


@runtime_checkable
class RankedVectorBackend(Protocol):
    """Structural interface for a high-level vector search provider.

    Distinct from ``vector_search.VectorSearchBackend`` (which is
    the low-level backend returning ``Tuple[str, float]``). This
    Protocol matches the shape of
    ``peldrun.memory.retrieval.vector_search.SqliteVecProvider
    .search`` -- it returns hydrated ``MemoryEntry`` objects.

    The provider does not care which embedding model or storage
    engine the backend uses.
    """

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        *,
        limit: int = ...,
        min_similarity: float = ...,
    ) -> List[Tuple[MemoryEntry, float]]:
        ...


# ---------------------------------------------------------------------------
# Fusion function type
# ---------------------------------------------------------------------------


#: Signature of the fusion functions in ``fusion.py``. Both
#: ``reciprocal_rank_fusion`` and ``weighted_fusion`` match this
#: shape modulo their keyword-only parameters; wrapping either in
#: ``functools.partial`` produces a callable that satisfies this
#: Protocol.
FusionFn = Callable[
    [List[RankedItem], List[RankedItem]],
    List[FusedItem],
]


# ---------------------------------------------------------------------------
# HybridSearch
# ---------------------------------------------------------------------------


class HybridSearch:
    """Runs lexical + vector search and fuses the results.

    See the module docstring for the layered architecture and the
    separation between this class (how to fuse) and the router
    (whether to fuse).

    Lifecycle
    ---------
    Construct once per (lexical backend, vector backend, fusion
    function) triple. Reuse across queries. The class holds no
    mutable state -- concurrent ``search()`` calls are safe.

    Example
    -------
    ::

        from functools import partial
        from peldrun.memory.retrieval.fusion import (
            reciprocal_rank_fusion,
        )

        hybrid = HybridSearch(
            lexical_backend=memory_repo,
            vector_backend=vec_provider,
            fusion_fn=partial(reciprocal_rank_fusion, k=60),
        )
        results = await hybrid.search(
            "coffee preferences", ["project:p1"], limit=10,
        )
    """

    #: Over-fetch factor applied to ``limit`` when querying each
    #: backend. Fetching 2x gives fusion enough candidates to
    #: produce a good merged top-N. This matches the plan's §5.6
    #: router example.
    _OVERFETCH_FACTOR: int = 2

    #: Minimum over-fetch offset (in addition to the factor).
    #: Prevents a 2x factor of 1 from yielding a single-candidate
    #: fetch, which would make fusion meaningless. 5 is enough
    #: to give fusion visible structure for tiny ``limit`` calls.
    _OVERFETCH_OFFSET: int = 5

    def __init__(
        self,
        *,
        lexical_backend: LexicalSearchBackend,
        vector_backend: RankedVectorBackend,
        fusion_fn: Optional[FusionFn] = None,
    ) -> None:
        """Initialize the hybrid searcher.

        Parameters
        ----------
        lexical_backend
            The FTS5/BM25 source. Typically
            ``SqliteMemoryRepository``.
        vector_backend
            The embedding/cosine source. Typically
            ``SqliteVecProvider``.
        fusion_fn
            The function that merges the two ranked lists.
            Defaults to ``reciprocal_rank_fusion`` with ``k=60``
            (the plan's default in §5.6). The router will pass
            ``partial(reciprocal_rank_fusion, k=settings.rrf_k)``
            when the operator has customized ``rrf_k``, and
            ``partial(weighted_fusion, alpha=1 - vector_alpha)``
            when the operator has customized ``vector_alpha``.
            Tests can pass their own deterministic merger.
        """
        self._lexical_backend = lexical_backend
        self._vector_backend = vector_backend
        self._fusion_fn: FusionFn = (
            fusion_fn
            if fusion_fn is not None
            else (lambda lex, vec: reciprocal_rank_fusion(lex, vec, k=60))
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        *,
        limit: int = 10,
        min_lexical_score: float = 0.0,
        min_vector_similarity: float = 0.0,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Run hybrid search and return the fused top-K.

        Parameters
        ----------
        query
            Text to search for. Both backends receive it
            verbatim; the vector backend embeds it, the lexical
            backend normalizes and MATCHes it. Empty/whitespace
            returns ``[]`` without any backend call.
        scope_refs
            Canonical ``"<type>:<id>"`` references. Both
            backends apply the same filter, so the two result
            lists are directly comparable. Empty list returns
            ``[]``.
        limit
            Maximum number of results AFTER fusion. Each
            backend is queried with ``limit * overfetch`` to
            give fusion enough candidates. Must be positive.
        min_lexical_score
            Minimum normalized BM25 score (``[0, 1]``). Lexical
            hits below this are dropped BEFORE fusion so they
            do not contribute RRF/weighted score. ``0.0``
            disables the threshold.
        min_vector_similarity
            Minimum cosine similarity (``[0, 1]``). Applied by
            the vector backend internally (its ``search``
            method takes ``min_similarity``) AND re-checked
            here defensively. ``0.0`` disables the threshold.

        Returns
        -------
        List[Tuple[MemoryEntry, float]]
            Fused results, ordered by descending fused score.
            The float is the FUSED score (not the original
            per-source score). Its range depends on the fusion
            function -- see the module docstring.

            Empty when:
              * the query is blank,
              * ``scope_refs`` is empty,
              * both backends return no hits,
              * every hit falls below the thresholds.

        Raises
        ------
        Any exception raised by either backend. The class
        deliberately does not catch backend errors -- the
        router (or any caller) is responsible for the
        fallback policy. A router that wants "lexical on
        vector failure" wraps this call in try/except.
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

        # ---- Compute the per-backend fetch size ----------------
        #
        # Fetch more candidates than the final limit so that
        # fusion has enough material. A result-list of length
        # ``limit`` from each source would only give fusion
        # ``limit`` merged candidates on the best case (no
        # overlap) -- not enough to find the true top-K under
        # most fusion strategies. Doubling plus a fixed offset
        # is the plan's recommended heuristic (§5.6).
        fetch_limit = max(
            limit * self._OVERFETCH_FACTOR,
            limit + self._OVERFETCH_OFFSET,
        )

        # ---- Query both backends -------------------------------
        #
        # Sequential (not parallel) on purpose:
        #   * the plan's §5.6 example is sequential;
        #   * asyncio.gather with return_exceptions=False leaves
        #     the other task running on failure, wasting work;
        #   * two ~100ms queries in series is still fast enough
        #     for interactive use.
        #
        # If either call raises, the exception propagates to the
        # caller unchanged. The router (Task 4.9) is the layer
        # that decides "vector failed, fall back to lexical" --
        # not this class.
        lexical_results = await self._lexical_backend.search_ranked(
            stripped_query, scope_refs, fetch_limit
        )
        vector_results = await self._vector_backend.search(
            stripped_query,
            scope_refs,
            limit=fetch_limit,
            min_similarity=min_vector_similarity,
        )

        # ---- Apply lexical threshold ---------------------------
        #
        # Applied AFTER retrieval but BEFORE fusion. Rationale:
        # a low-score lexical hit contributes the same
        # rank-based weight in RRF as a high-score one -- the
        # only way to keep it out of the fused result is to
        # remove it from the input list before fusion runs.
        if min_lexical_score > 0.0:
            lexical_results = [
                (e, s) for e, s in lexical_results
                if s >= min_lexical_score
            ]

        # ---- Early exit if both lists are empty ----------------
        if not lexical_results and not vector_results:
            logger.debug(
                "HybridSearch: no hits (query=%r scope_refs=%d "
                "min_lex=%.2f min_vec=%.2f)",
                stripped_query[:80],
                len(scope_refs),
                min_lexical_score,
                min_vector_similarity,
            )
            return []

        # ---- Build id -> MemoryEntry map -----------------------
        #
        # When the same memory appears in both lists, prefer the
        # lexical copy -- it comes directly from the repository,
        # so it is guaranteed to reflect the current stored
        # state. The vector copy came via hydration, which
        # reads from the same repository, so in practice the two
        # are identical; the preference just removes a tie
        # decision.
        entry_by_id: Dict[str, MemoryEntry] = {}
        for entry, _ in lexical_results:
            entry_by_id[entry.memory_id] = entry
        for entry, _ in vector_results:
            entry_by_id.setdefault(entry.memory_id, entry)

        # ---- Convert to RankedItem lists ------------------------
        lexical_ranked = [
            RankedItem(id=e.memory_id, score=s)
            for e, s in lexical_results
        ]
        vector_ranked = [
            RankedItem(id=e.memory_id, score=s)
            for e, s in vector_results
        ]

        # ---- Fuse ----------------------------------------------
        fused = self._fusion_fn(lexical_ranked, vector_ranked)

        # ---- Map back to entries + truncate --------------------
        #
        # Fused items are sorted by descending score. We walk
        # them in order, skip any whose entry was already
        # dropped (should not happen -- every fused id came from
        # one of the two entry lists -- but defensive), and
        # truncate to ``limit``.
        result: List[Tuple[MemoryEntry, float]] = []
        for item in fused:
            if len(result) >= limit:
                break
            entry = entry_by_id.get(item.id)
            if entry is None:
                # Unreachable in practice. Log at warning so a
                # bug surfaces without hiding the rest of the
                # results.
                logger.warning(
                    "HybridSearch: fused id %r not found in "
                    "either entry map (this is a bug)",
                    item.id,
                )
                continue
            result.append((entry, item.score))

        logger.debug(
            "HybridSearch: query=%r scope_refs=%d limit=%d "
            "lex=%d vec=%d fused=%d returned=%d",
            stripped_query[:80],
            len(scope_refs),
            limit,
            len(lexical_results),
            len(vector_results),
            len(fused),
            len(result),
        )
        return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "FusionFn",
    "HybridSearch",
    "LexicalSearchBackend",
    "RankedVectorBackend",
]
