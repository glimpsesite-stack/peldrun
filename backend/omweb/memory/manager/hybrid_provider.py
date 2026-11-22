# backend/omweb/memory/manager/hybrid_provider.py
"""
PELDRUN Memory Platform - Hybrid Memory Provider.

Composes the lexical (FTS/BM25) and semantic (vector) retrieval
paths into a single ``MemoryProvider``-shaped object that the
``MemoryService`` exposes as its live provider. Every retrieval
consumer (the ``/api/memory/search`` endpoint, the
``MemorySearchTool``, the dispatcher's auto-retrieval hook)
therefore gets hybrid recall without any per-call wiring.

Why this file exists
--------------------

The ``peldrun`` layer is intentionally unaware that a vector
backend exists:

  * ``SqliteFtsProvider`` (peldrun) implements ``MemoryProvider``
    over FTS5 only.
  * ``SqliteVecProvider`` (peldrun) exposes a narrower
    ``search(query, scope_refs, limit, min_similarity)`` surface,
    but it does NOT implement ``MemoryProvider`` -- it has no
    write methods, no list/get, no project aggregation.

Neither provider can be swapped in as the OTHER's full
replacement. The natural composition is a thin wrapper that:

  * delegates every write / CRUD method to the FTS provider,
  * runs BOTH search paths, and
  * fuses their ranked lists.

That wrapper is this class. It lives on the ``omweb`` side
because only ``omweb`` holds both providers at the same time
(constructed by ``MemoryService._initialize_embedding``).

Fusion algorithm -- Reciprocal Rank Fusion (RRF)
-----------------------------------------------

RRF is a standard, empirically-strong approach for combining
multiple ranked lists without needing calibrated scores across
sources (FTS produces BM25 values, vector produces cosine
similarities -- different scales, different distributions).

Formula::

    score(d) = sum over each ranked list L of:
                   1 / (k + rank_L(d))

Where ``rank_L(d)`` is 1-based and ``k`` is a smoothing constant
that prevents top-ranked entries from dominating. Standard
value is ``k = 60`` (Cormack et al., 2009), which is what this
class defaults to.

Intuition: an entry appearing in BOTH lists gets a large bonus
(≈ 2/(k+1) at the top), while an entry appearing in only ONE
list is penalized by its absence from the other. This rewards
agreement without needing either side to dominate.

Why not weighted score averaging?
    * BM25 and cosine similarity are not on the same scale. Any
      fixed weight is arbitrary and model-dependent.
    * Weighted averaging breaks when one side returns nothing
      (the fused score would be halved).
    * RRF is rank-only, so it never has to know what the scores
      "mean" -- only what order they produce.

Why not CombSUM/CombMNZ?
    * CombSUM requires normalizing each list independently,
      which introduces its own heuristic (min-max? z-score?).
    * CombMNZ requires knowing how many lists contain each item,
      which RRF already captures structurally.

Error handling -- graceful degradation
--------------------------------------

Every search path is wrapped in its own ``try/except``. If the
vector side fails (provider down, dimension mismatch, sqlite-vec
crash), the hybrid returns the FTS results alone with a WARNING
log. Symmetrically, if FTS fails, the hybrid returns the vector
results alone. The user NEVER sees a 500 from the search
endpoint because of one flaky backend.

Filter semantics
----------------

The hybrid SKIPS the vector path when the caller requests:

  * a non-``active`` status filter (``status != "active"``),
  * a ``memory_type`` filter,
  * an ``is_pinned`` filter,
  * any tag filter.

Rationale: the vector index only contains ACTIVE entries (the
worker writes only on save), and it has no columnar filtering --
hydration filters to ACTIVE only. Adding FTS-only results (which
DO respect the caller's filters) and unfiltered vector results
would violate the caller's contract. Better to degrade to FTS-
only search than to silently leak out-of-filter entries.

For the two filter shapes the vector path DOES respect (empty
query and default status), hybrid is used as designed.

Design invariants
-----------------

1. **Delegation is transparent.**
   ``__getattr__`` forwards every method not explicitly defined
   on this class to the FTS provider. New ``MemoryProvider``
   methods added in the future work here without modification.
   Only ``open``, ``close``, ``is_open``, ``search``, and
   ``search_ranked`` are overridden.

2. **Open / close are no-ops.**
   The FTS provider is opened and closed by
   ``MemoryLifecycleManager``. The embedder and vector backend
   are owned by ``MemoryService._initialize_embedding``. The
   hybrid's lifecycle is therefore implicit -- it exists only
   as long as both underlying providers do. Re-opening or
   closing the hybrid MUST NOT touch the underlying providers,
   or we would tear down state that other subsystems still
   depend on.

3. **RRF scores are normalized to [0, 1].**
   The ranker downstream expects relevance in ``[0, 1]`` so
   ``min_relevance_score`` has a stable meaning across both
   FTS and hybrid modes. Raw RRF scores are tiny (≈ 0.03 for a
   rank-1 entry) and would fail every threshold. Normalizing
   by the max score in the merged set maps the best match to
   1.0 and preserves relative ordering.

4. **No mutable state after construction.**
   Concurrent ``search_ranked`` calls are safe -- the class
   holds only immutable references plus ``_rrf_k``.

See also
--------

    * ``peldrun/memory/retrieval/vector_search.py``
                                -- ``SqliteVecProvider.search``
    * ``peldrun/memory/providers/sqlite_fts.py``
                                -- the FTS provider we delegate
                                   to
    * ``omweb/memory/manager/memory_service.py``
                                -- constructs and wires the
                                   hybrid pipeline
    * ``omweb/memory/storage/vector_hydration.py``
                                -- turns ids into MemoryEntry
                                   with scope filtering
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from peldrun.memory.domain import MemoryEntry, MemoryProvider
from peldrun.memory.retrieval.vector_search import SqliteVecProvider


logger = logging.getLogger(
    "omweb.memory.manager.hybrid_provider"
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_DEFAULT_RRF_K: int = 60
"""Smoothing constant for Reciprocal Rank Fusion.

Standard value from Cormack et al. (2009). Higher values flatten
the curve (rank 1 and rank 2 become more similar); lower values
sharpen the advantage of top-ranked entries. ``60`` is the
empirically-recommended default and matches the convention used
in Elasticsearch and OpenSearch's built-in RRF.

Increasing this value helps when both retrievers produce long
lists of similar-quality results. Decreasing it helps when one
retriever has a very confident top match that should dominate.
``60`` is the right compromise for FTS + vector search over the
size of lists the memory subsystem produces (<100 candidates).
"""


_VEC_INCOMPATIBLE_FILTERS: Tuple[str, ...] = (
    "memory_type",
    "is_pinned",
    "tags",
)
"""Filter keys that force FTS-only mode.

The vector path cannot honor these:
  * ``memory_type`` -- the vec0 index has no column for it.
  * ``is_pinned``   -- same.
  * ``tags``        -- same.

Passing any of them to a search that used vector would silently
return entries that violate the caller's filter -- unacceptable
for e.g. the ``MemorySearchTool`` where "find all decisions"
must return only decisions.

The FTS provider honors all three, so delegating to it keeps
the caller's contract intact.
"""


# ---------------------------------------------------------------------------
# HybridMemoryProvider
# ---------------------------------------------------------------------------


class HybridMemoryProvider:
    """``MemoryProvider``-shaped wrapper fusing FTS + Vector search.

    Every method not explicitly defined here is delegated to the
    wrapped FTS provider via ``__getattr__``. The class overrides
    ONLY:

      * ``open()`` / ``close()`` -- no-ops (lifecycle-managed),
      * ``is_open``          -- mirrors the FTS provider,
      * ``search()``         -- hybrid (FTS + vector, scores
                                stripped),
      * ``search_ranked()``  -- hybrid (FTS + vector, RRF-fused).

    See the module docstring for the algorithm and its rationale.
    """

    def __init__(
        self,
        *,
        fts_provider: MemoryProvider,
        vec_provider: SqliteVecProvider,
        rrf_k: int = _DEFAULT_RRF_K,
        reranker: Optional[Any] = None,
        graph_search: Optional[Any] = None,
        min_vector_similarity: float = 0.0,
        mode: str = "hybrid",
    ) -> None:
        """Construct the hybrid wrapper.

        Parameters
        ----------
        fts_provider
            The fully-opened FTS provider. Owned by
            ``MemoryLifecycleManager``; this wrapper does NOT
            close it. Every non-search method delegates here.
        vec_provider
            The configured ``SqliteVecProvider``. Owned by
            ``MemoryService``; this wrapper does NOT close it
            either. Its ``search`` method provides the semantic
            signal.
        rrf_k
            Smoothing constant for RRF. Defaults to 60.
        reranker
            Optional Phase 5A LLM reranker. When supplied AND
            active (see ``CrossEncoderReranker.is_active``) AND
            the search produces a hybrid fusion, its ``rerank``
            coroutine is invoked AFTER ``_rrf_merge`` and BEFORE
            the caller receives results. The reranker is a
            NO-OP when not configured (no model selected) and
            gracefully degrades to RRF ordering on any failure
            (timeout, parse error, provider down) -- so passing
            ``None`` (the default) and passing an INACTIVE
            reranker produce IDENTICAL search behavior. Stored
            as ``Any`` rather than a concrete type to keep this
            module decoupled from
            ``peldrun.memory.retrieval.reranker``.

        Raises
        ------
        TypeError
            When ``fts_provider`` is ``None`` (defensive -- a
            hybrid with no lexical side is just a vector
            provider, which is a different design).
        ValueError
            When ``rrf_k`` is not a positive int. A zero or
            negative smoothing constant breaks the RRF formula.
        """
        if fts_provider is None:
            raise TypeError(
                "fts_provider must be a MemoryProvider instance, "
                "got None. A hybrid without an FTS side is a "
                "different design (use SqliteVecProvider directly "
                "if that is what you want)."
            )
        if (
            not isinstance(rrf_k, int)
            or isinstance(rrf_k, bool)
            or rrf_k <= 0
        ):
            raise ValueError(
                f"rrf_k must be a positive int, got {rrf_k!r}"
            )

        self._fts_provider: MemoryProvider = fts_provider
        self._vec_provider: SqliteVecProvider = vec_provider
        self._rrf_k: int = rrf_k
        # R4/C2: retrieval strategy + vector cosine floor, wired from
        # MemoryRetrievalSettings by MemoryService. ``mode`` selects
        # lexical / vector / hybrid / automatic; min_vector_similarity
        # was hardcoded to 0.0 (audit R4).
        self._min_vector_similarity: float = float(min_vector_similarity)
        self._mode: str = mode
        #: Phase 5A: optional LLM-based reranker. ``None`` when
        #: disabled in settings or when no model is configured --
        #: in that case ``search_ranked`` skips the rerank step
        #: entirely, so the search path is unchanged from
        #: Phase 4.
        #:
        #: Stored as ``Any`` (not a typed reference to
        #: ``peldrun.memory.retrieval.reranker.CrossEncoderReranker``)
        #: to keep this module decoupled from the concrete
        #: reranker implementation. Any object exposing an async
        #: ``rerank(query, candidates, top_k)`` coroutine and a
        #: ``last_rerank_succeeded: bool`` property is compatible.
        self._reranker: Optional[Any] = reranker

        #: Phase 6B: optional knowledge-graph source. When supplied,
        #: ``search_ranked`` runs a THIRD ranked list in parallel
        #: with FTS and vector, and fuses all three via RRF.
        #:
        #: Contract: an object with an async
        #: ``find_related_for_query(query, scope_refs=..., limit=...)``
        #: coroutine returning ``List[Tuple[MemoryEntry, float]]``.
        #: ``peldrun.memory.graph.graph_search.GraphSearch`` is the
        #: production implementation; tests use a tiny double.
        #:
        #: Stored as ``Any`` for the same reason as ``reranker``:
        #: this module must not import the graph stack.
        #:
        #: ``None`` (the default) means the graph never participates
        #: and the search path is byte-for-byte identical to Phase 5.
        self._graph_search: Optional[Any] = graph_search

        #: Records the strategy used by the MOST RECENT
        #: ``search_ranked`` call. One of:
        #:
        #:   * ``"hybrid+rerank"`` -- both FTS and vector contributed,
        #:                    their ranked lists were fused via RRF,
        #:                    and the fused result was re-ordered
        #:                    by the Phase 5A LLM reranker.
        #:   * ``"hybrid"`` -- both FTS and vector produced results
        #:                    and were fused via RRF. (Either no
        #:                    reranker is configured OR the reranker
        #:                    fell back to RRF ordering.)
        #:   * ``"vector"`` -- vector succeeded but FTS returned
        #:                    nothing (or FTS raised).
        #:   * ``"fts"``    -- FTS-only (either by choice, or because
        #:                    the vector path was inapplicable /
        #:                    returned nothing / raised).
        #:
        #: Read by ``MemoryService.last_search_mode`` and surfaced
        #: through ``POST /api/memory/search`` so the operator can
        #: verify whether semantic recall and/or LLM reranking
        #: actually contributed to a given result set.
        #:
        #: ``None`` until the first ``search_ranked`` call -- a
        #: fresh instance has no history to report.
        #:
        #: Concurrency: this is diagnostic state, not load-bearing.
        #: If two searches overlap on the same process, the second
        #: overwrites the first's mode. Acceptable because (a) the
        #: value informs UI display only, (b) PELDRUN memory is
        #: single-process per host (WAL on a local SQLite file --
        #: no cross-process sharing). A future refactor to return
        #: ``(results, mode)`` would eliminate the shared state but
        #: is not required for the diagnostic use case.
        self._last_search_mode: Optional[str] = None

    # ------------------------------------------------------------------
    # Delegation
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward every unknown attribute to the FTS provider.

        Any ``MemoryProvider`` method that this class does not
        override (``create``, ``get``, ``update``, ``delete``,
        ``purge``, ``list_by_scope``, ``mark_used``,
        ``list_project_summaries``, ``get_project_stats``,
        ``bulk_archive``, ``bulk_purge``, ``provider_id``, ...)
        lands here. This is the whole point of the delegation:
        new methods added to the ``MemoryProvider`` contract in
        the future work transparently without edits to this file.

        Guard against recursion: accessing ``self._fts_provider``
        from within ``__getattr__`` would loop if the attribute
        did not yet exist (which cannot happen -- it is set in
        ``__init__`` before any attribute access -- but the
        guard costs nothing and removes the failure mode from
        the possibility space).
        """
        if name in (
            "_fts_provider",
            "_vec_provider",
            "_rrf_k",
        ):
            # Avoid infinite recursion during hypothetical
            # unpickling / copy operations where __init__ may
            # not have run.
            raise AttributeError(name)
        return getattr(self._fts_provider, name)

    # ------------------------------------------------------------------
    # Lifecycle -- no-ops
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """Mirror the FTS provider's open state.

        The hybrid itself has no lifecycle -- it exists only
        because both underlying providers do. If the FTS provider
        reports closed, the hybrid is effectively closed too (no
        write path, and the vector path alone cannot satisfy the
        full ``MemoryProvider`` contract).
        """
        # ``is_open`` is defined on every BaseMemoryProvider
        # subclass, so this is safe. If a future provider removes
        # it, we would fall through to ``__getattr__`` -- which
        # is what we want anyway.
        return bool(getattr(self._fts_provider, "is_open", False))

    async def open(self) -> None:
        """No-op.

        The FTS provider is opened by ``MemoryLifecycleManager``
        during service startup. The vector path is lazy (its
        SQLite connection opens on first use). Neither needs to
        be re-opened here.

        Calling this method is safe (idempotent) -- it exists
        only to satisfy the ``MemoryProvider`` structural
        contract for any code that unconditionally calls
        ``provider.open()``.
        """
        return

    async def close(self) -> None:
        """No-op.

        The FTS provider is closed by ``MemoryLifecycleManager``
        on shutdown. The embedder and vector backend are closed
        by ``MemoryService._close_embedding``. This wrapper owns
        neither, so closing here would tear down state that
        other subsystems still depend on.

        Calling this method is safe (idempotent) -- it exists
        only to satisfy the ``MemoryProvider`` structural
        contract for any code that unconditionally calls
        ``provider.close()``.
        """
        return

    # ------------------------------------------------------------------
    # Search -- hybrid
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[MemoryEntry]:
        """Hybrid search returning bare ``MemoryEntry`` objects.

        Same return shape as ``SqliteFtsProvider.search``: the
        caller gets entries only, no relevance scores. This is
        what ``WritePipeline._fetch_existing_pool`` and every
        other non-ranked consumer expects.

        Implementation: delegates to ``search_ranked`` (which
        does the fusion) and strips the scores.
        """
        ranked = await self.search_ranked(
            query, scope_refs, limit, **filters
        )
        return [entry for entry, _score in ranked]

    async def search_ranked(
        self,
        query: str,
        scope_refs: List[str],
        limit: int = 5,
        **filters: Any,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Hybrid search returning ``(entry, relevance)`` pairs.

        ``relevance`` is normalized to ``[0, 1]`` and reflects
        RRF fusion -- see the module docstring for the formula
        and the normalization rationale.

        When the vector path is inapplicable (see
        ``_VEC_INCOMPATIBLE_FILTERS``) or fails, this method
        returns FTS results verbatim (with FTS's native
        relevance scale). When FTS fails, it returns vector
        results verbatim. When both fail, it returns an empty
        list with a WARNING log -- the caller degrades to
        "no results", which is the correct behavior for a
        search endpoint.
        """
        # Reset the last-mode tracker at the START of every call.
        # This guarantees the reported mode belongs to THIS search
        # -- not to a previous one whose flow happened to skip the
        # setter (e.g. an early return that bypassed the branches
        # below). Every return path from here on is responsible
        # for updating ``_last_search_mode`` to its actual
        # strategy.
        self._last_search_mode = None

        # Fast path: nothing to search.
        if (
            limit <= 0
            or not scope_refs
            or not isinstance(query, str)
        ):
            self._last_search_mode = "fts"
            return await self._fts_provider.search_ranked(
                query, scope_refs, limit, **filters
            )

        # R4/C2: retrieval.mode selects the strategy. ``lexical``
        # bypasses the vector source; ``vector`` bypasses FTS;
        # ``hybrid``/``automatic`` fuse both (automatic == hybrid here
        # because this wrapper only exists when the vector side is ready).
        if self._mode == "lexical":
            self._last_search_mode = "fts"
            return await self._fts_provider.search_ranked(
                query, scope_refs, limit, **filters
            )
        if self._mode == "vector":
            self._last_search_mode = "vector"
            try:
                vec_only = await self._vec_provider.search(
                    query=query, scope_refs=scope_refs, limit=limit,
                    min_similarity=self._min_vector_similarity,
                )
                return list(vec_only)
            except Exception as exc:
                logger.warning(
                    "Hybrid: vector-only search failed; falling back "
                    "to FTS: %s", exc,
                )
                self._last_search_mode = "fts"
                return await self._fts_provider.search_ranked(
                    query, scope_refs, limit, **filters
                )

        # Decide whether vector participates.
        use_vec = self._should_use_vector(query, filters)
        if not use_vec:
            self._last_search_mode = "fts"
            return await self._fts_provider.search_ranked(
                query, scope_refs, limit, **filters
            )

        # ---- Run both paths in parallel ----------------------
        #
        # ``return_exceptions=True`` lets one side fail without
        # cancelling the other. The cancellation discipline of
        # ``asyncio.gather`` with the default setting would
        # abort the healthy coroutine when the broken one
        # raised -- the opposite of what we want here.
        #
        # [PHASE 6B] The graph source runs in the SAME gather, so
        # enabling it adds no serial latency: the graph lookup
        # overlaps the FTS query and the embedding HTTP call. It
        # performs NO LLM call (see ``graph_search.py`` invariant
        # #1), so it stays inside the dispatcher's latency budget.
        fts_coro = self._fts_provider.search_ranked(
            query, scope_refs, limit, **filters
        )
        vec_coro = self._vec_provider.search(
            query=query,
            scope_refs=scope_refs,
            limit=limit,
            min_similarity=self._min_vector_similarity,
        )
        graph_coro = self._graph_ranked(query, scope_refs, limit)

        if graph_coro is None:
            fts_result, vec_result = await asyncio.gather(
                fts_coro, vec_coro, return_exceptions=True
            )
            graph_result: Any = []
        else:
            fts_result, vec_result, graph_result = (
                await asyncio.gather(
                    fts_coro,
                    vec_coro,
                    graph_coro,
                    return_exceptions=True,
                )
            )

        # ---- Degrade gracefully on per-path failure ----------
        if isinstance(fts_result, BaseException):
            logger.warning(
                "Hybrid: FTS search failed (falling back to "
                "vector-only for this query): %s",
                fts_result,
                exc_info=fts_result,
            )
            fts_result = []
        if isinstance(vec_result, BaseException):
            logger.warning(
                "Hybrid: vector search failed (falling back to "
                "FTS-only for this query): %s",
                vec_result,
                exc_info=vec_result,
            )
            vec_result = []
        graph_hits: List[Tuple[MemoryEntry, float]] = []
        if isinstance(graph_result, BaseException):
            # The graph is an ADDITIONAL recall source; losing it
            # must leave the lexical and vector results intact.
            logger.warning(
                "Hybrid: graph search failed (continuing with "
                "FTS + vector only): %s",
                graph_result,
            )
        elif graph_result:
            graph_hits = list(graph_result)

        # ---- Phase 6B: three-way fusion ---------------------
        #
        # Only taken when the graph actually contributed. With an
        # empty graph (the common case right after enabling the
        # feature) this branch is skipped entirely, so behaviour is
        # IDENTICAL to Phase 5 -- including the single-source
        # short-circuits below.
        if graph_hits:
            return await self._finish_with_graph(
                query=query,
                fts_result=fts_result,
                vec_result=vec_result,
                graph_hits=graph_hits,
                limit=limit,
            )

        # ---- Handle empty-on-one-side ------------------------
        #
        # If only one side produced results, return them
        # verbatim. This preserves each side's native relevance
        # scale, which the ranker downstream understands better
        # than a one-sided RRF normalization would.
        if not vec_result and fts_result:
            self._last_search_mode = "fts"
            return fts_result
        if not fts_result and vec_result:
            self._last_search_mode = "vector"
            return vec_result
        if not fts_result and not vec_result:
            # Both sides empty (either they had nothing, or
            # both raised and were reset to []). Report "fts"
            # because that is what the caller effectively
            # received from their perspective -- no results,
            # no semantic contribution.
            self._last_search_mode = "fts"
            return []

        # ---- Both sides produced results -- fuse with RRF ---
        fused = self._rrf_merge(fts_result, vec_result, limit)

        # ---- Optional: LLM-based reranking (Phase 5A) -------
        #
        # The reranker is applied ONLY to the fused hybrid
        # results, never to the single-source branches above.
        # Reasons:
        #
        #   * The reranker's prompt was designed to re-order
        #     candidates whose scores are already RRF-fused --
        #     its job is to refine an ALREADY blended
        #     ordering, not to compete with a single source's
        #     native scale (BM25 or cosine).
        #
        #   * The reference §6.3 specifies "after RRF fusion,
        #     before the ranker" -- i.e. the hybrid path only.
        #
        #   * Cost: single-source searches (FTS-only via
        #     filter incompatibility, vector-only after FTS
        #     failure, etc.) pay no LLM cost. Those paths are
        #     typically fast-fallback states where adding an
        #     LLM call would make the degradation WORSE.
        #
        # ``self._reranker`` is None when the operator has
        # not enabled reranking in settings. Even when present,
        # the reranker itself is a NO-OP if no model is
        # configured (see ``CrossEncoderReranker.is_active``) --
        # so ``rerank()`` returns its input unchanged with
        # negligible overhead.
        #
        # [PHASE 6C] ``no_rerank`` lets a caller opt out for a
        # single search. The agentic retriever sets it on every hop:
        # it already selects evidence across hops by fusion, so
        # re-scoring each hop's candidates on top of that buys no
        # accuracy, while costing ``ceil(candidates / batch_size)``
        # extra completions PER sub-query -- roughly 36 per 3-hop run
        # at the default configuration. Against a local model that
        # amplification is what starved the agentic calls themselves.
        # The flag is popped here, so it never reaches the FTS/vector
        # backends.
        skip_rerank = bool(filters.pop("no_rerank", False))
        if (
            self._reranker is not None
            and len(fused) > 1
            and not skip_rerank
        ):
            reranked = await self._reranker.rerank(
                query, fused, top_k=limit,
            )
            # ``last_rerank_succeeded`` distinguishes "the LLM
            # actually re-ordered the results" from "the
            # reranker fell back to RRF ordering". Both cases
            # return the correct result set (rerank is
            # no-worse-than-input by contract), but only the
            # former deserves the ``"hybrid+rerank"`` tag --
            # otherwise the operator sees a misleading
            # diagnostic.
            #
            # ``getattr(..., False)`` tolerates a reranker
            # implementation (e.g. a test mock) that does not
            # expose the property -- falls back to "assume
            # failed", which keeps ``_last_search_mode`` at
            # the conservative ``"hybrid"``.
            if getattr(
                self._reranker, "last_rerank_succeeded", False
            ):
                self._last_search_mode = "hybrid+rerank"
            else:
                self._last_search_mode = "hybrid"
            return reranked

        self._last_search_mode = "hybrid"
        return fused

    # ------------------------------------------------------------------
    # Fusion internals
    # ------------------------------------------------------------------

    def _should_use_vector(
        self,
        query: str,
        filters: Dict[str, Any],
    ) -> bool:
        """Return whether the vector path is applicable for this call.

        Skips vector when:
          * the query is empty / whitespace (nothing to embed),
          * a filter that the vector path cannot honor is set
            (see ``_VEC_INCOMPATIBLE_FILTERS``),
          * the caller asked for a non-active status
            (``status not in (None, "active")``).

        The last case matters because the vector index only
        contains ACTIVE entries. Returning active-only results
        when the caller asked for "all" or "archived" would
        silently violate their contract -- better to give them
        FTS's correct results and lose the semantic bonus.
        """
        if not isinstance(query, str) or not query.strip():
            return False

        # Non-active status filter -> FTS only.
        status = filters.get("status")
        if status is not None and status != "active":
            return False

        # Incompatible filter keys -> FTS only.
        for key in _VEC_INCOMPATIBLE_FILTERS:
            if key in filters and filters[key] is not None:
                return False

        return True

    def _rrf_merge(
        self,
        fts_ranked: List[Tuple[MemoryEntry, float]],
        vec_ranked: List[Tuple[MemoryEntry, float]],
        limit: int,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Fuse two ranked lists using Reciprocal Rank Fusion.

        See the module docstring for the formula. The two input
        lists MAY share entries; shared entries accumulate
        contributions from both.

        Normalization
        -------------
        Raw RRF scores are tiny (≈ 0.033 for rank 1 with k=60)
        and not comparable to ``min_relevance_score`` (a
        ``[0, 1]`` threshold in the memory settings). We
        therefore normalize the top score to 1.0 -- a monotonic
        transform that preserves ordering and gives the ranker a
        usable threshold scale.

        Zero-max edge case
        ------------------
        If every score is exactly 0 (which cannot happen with
        well-formed input -- rank 1 in either list yields a
        positive contribution -- but which we guard anyway),
        fall back to 1.0 for every entry so nothing gets
        filtered out by the ranker. This is the correct
        degradation: "the fusion produced something, the
        downstream is free to accept it".
        """
        scores: Dict[str, float] = {}
        entries: Dict[str, MemoryEntry] = {}
        k = self._rrf_k

        for rank, (entry, _sim) in enumerate(fts_ranked, start=1):
            mid = entry.memory_id
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
            entries[mid] = entry

        for rank, (entry, _sim) in enumerate(vec_ranked, start=1):
            mid = entry.memory_id
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
            if mid not in entries:
                entries[mid] = entry

        if not scores:
            return []

        # Sort all candidates by RRF score descending, cap to limit.
        ordered_ids = sorted(
            scores.keys(),
            key=lambda mid: scores[mid],
            reverse=True,
        )[:limit]

        # Normalize to [0, 1] -- see the docstring above.
        max_score = scores[ordered_ids[0]]
        if max_score <= 0.0:
            return [(entries[mid], 1.0) for mid in ordered_ids]

        return [
            (entries[mid], scores[mid] / max_score)
            for mid in ordered_ids
        ]

    # ------------------------------------------------------------------
    # Graph source (Phase 6B)
    # ------------------------------------------------------------------

    def _graph_ranked(
        self,
        query: str,
        scope_refs: List[str],
        limit: int,
    ) -> Optional[Any]:
        """Return the graph-search coroutine, or ``None`` when off.

        Returning a CORO (not awaiting here) is what lets the caller
        hand all three sources to a single ``asyncio.gather``, so
        the graph lookup overlaps the FTS query and the embedding
        HTTP call instead of running after them.

        ``scope_refs`` is forwarded explicitly and always: the graph
        is the one source whose underlying table has NO scope column
        of its own, so failing to pass the live scope set would
        return memories from other projects (see ``graph_search.py``
        invariant #4).
        """
        if self._graph_search is None:
            return None
        try:
            return self._graph_search.find_related_for_query(
                query,
                scope_refs=list(scope_refs),
                limit=max(int(limit), 1),
            )
        except AttributeError as exc:
            # A mis-shaped graph_search object (wrong contract) must
            # not break search. Log once per call and drop the
            # source for this query.
            logger.warning(
                "Hybrid: graph_search does not expose "
                "find_related_for_query (%s); graph source "
                "disabled for this query.",
                exc,
            )
            return None

    async def _finish_with_graph(
        self,
        *,
        query: str,
        fts_result: List[Tuple[MemoryEntry, float]],
        vec_result: List[Tuple[MemoryEntry, float]],
        graph_hits: List[Tuple[MemoryEntry, float]],
        limit: int,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Fuse the non-empty sources (2 or 3 way) and optionally rerank.

        Mode reporting
        --------------
          * ``"hybrid+graph"`` -- at least two sources contributed
            (so the fusion genuinely blended signals),
          * ``"graph"`` -- the graph was the ONLY source that
            returned anything. Honest: the caller received
            graph-only results, not a blend.

        When the reranker improves the ordering the tag gains a
        ``+rerank`` segment, mirroring the Phase 5 behaviour
        (``"hybrid+rerank"``).

        RRF is used for 3-way fusion for the same reason it is used
        for 2-way: the three sources produce scores on incompatible
        scales (BM25, cosine, and distance-derived), so only their
        ORDER can be meaningfully combined.
        """
        ranked_lists = [
            ranked
            for ranked in (fts_result, vec_result, graph_hits)
            if ranked
        ]
        fused = self._rrf_merge_many(ranked_lists, limit)
        base_mode = "hybrid+graph" if len(ranked_lists) >= 2 else "graph"

        if self._reranker is not None and len(fused) > 1:
            reranked = await self._reranker.rerank(
                query, fused, top_k=limit,
            )
            if getattr(
                self._reranker, "last_rerank_succeeded", False
            ):
                self._last_search_mode = (
                    base_mode + "+rerank"
                )
            else:
                self._last_search_mode = base_mode
            return reranked

        self._last_search_mode = base_mode
        return fused

    def _rrf_merge_many(
        self,
        ranked_lists: List[List[Tuple[MemoryEntry, float]]],
        limit: int,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Fuse N ranked lists with RRF, normalized to ``[0, 1]``.

        Generalizes :meth:`_rrf_merge` to any number of sources so
        the 2-way (Phase 4) and 3-way (Phase 6) paths share one
        formula. Entries present in several lists accumulate a
        contribution from each -- the "consensus bonus" RRF relies
        on, which is exactly what should happen when FTS, vector,
        AND the graph all surface the same memory.

        Ties break by ``memory_id`` ascending so two identical
        searches return identical orderings (required for
        reproducible retrieval and for golden tests).
        """
        scores: Dict[str, float] = {}
        entries: Dict[str, MemoryEntry] = {}
        k = self._rrf_k

        for ranked in ranked_lists:
            for rank, (entry, _sim) in enumerate(ranked, start=1):
                if entry is None or not entry.memory_id:
                    continue
                mid = entry.memory_id
                scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank)
                if mid not in entries:
                    entries[mid] = entry

        if not scores:
            return []

        ordered_ids = sorted(
            scores.keys(),
            key=lambda mid: (-scores[mid], mid),
        )[:limit]
        if not ordered_ids:
            return []

        max_score = scores[ordered_ids[0]]
        if max_score <= 0.0:
            return [(entries[mid], 1.0) for mid in ordered_ids]

        return [
            (entries[mid], scores[mid] / max_score)
            for mid in ordered_ids
        ]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def last_search_mode(self) -> Optional[str]:
        """Strategy used by the most recent ``search_ranked`` call.

        Returns ``None`` before any search runs; otherwise one of
        ``"hybrid+rerank"``, ``"hybrid"``, ``"vector"``, or
        ``"fts"``. See ``_last_search_mode`` in ``__init__`` for
        the semantics of each value.

        The result reflects the FIRST decision that determined the
        outcome, not a post-hoc reconstruction: if both sides
        produced results and the reranker improved them, it is
        ``"hybrid+rerank"``; if both sides produced results but
        no reranker applied (or the reranker fell back), it is
        ``"hybrid"``; if only one side produced, it is that
        side's tag. This is exactly what the operator wants -- a
        reliable indicator that "did semantic recall and/or LLM
        reranking actually contribute?" -- rather than a
        synthesized guess after the fact.
        """
        return self._last_search_mode

    @property
    def rrf_k(self) -> int:
        """The RRF smoothing constant in use (for diagnostics)."""
        return self._rrf_k

    @property
    def min_vector_similarity(self) -> float:
        """The cosine floor applied to the vector source (diagnostics)."""
        return self._min_vector_similarity

    @property
    def mode(self) -> str:
        """The retrieval strategy in use (for diagnostics)."""
        return self._mode

    @property
    def fts_provider(self) -> MemoryProvider:
        """The wrapped FTS provider (for tests + diagnostics)."""
        return self._fts_provider

    @property
    def vec_provider(self) -> SqliteVecProvider:
        """The wrapped vector provider (for tests + diagnostics)."""
        return self._vec_provider

    @property
    def reranker(self) -> Optional[Any]:
        """The optional Phase 5A LLM reranker, or ``None``.

        Exposed for diagnostics and for ``MemoryService`` (which
        needs to distinguish "hybrid but no reranker" from
        "hybrid+reranker disabled in settings" when building
        the ``search_mode`` diagnostic surfaced through
        ``POST /api/memory/search``).
        """
        return self._reranker

    @property
    def graph_search(self) -> Optional[Any]:
        """The optional Phase 6B graph source, or ``None``.

        ``None`` means the graph never participates. A non-``None``
        value does NOT guarantee graph hits: most queries mention no
        known entity, and the graph simply contributes nothing.
        """
        return self._graph_search

    def __repr__(self) -> str:
        reranker_desc = (
            repr(self._reranker)
            if self._reranker is not None
            else "None"
        )
        return (
            f"HybridMemoryProvider("
            f"fts={type(self._fts_provider).__name__}, "
            f"vec={type(self._vec_provider).__name__}, "
            f"rrf_k={self._rrf_k}, "
            f"reranker={reranker_desc})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["HybridMemoryProvider"]
