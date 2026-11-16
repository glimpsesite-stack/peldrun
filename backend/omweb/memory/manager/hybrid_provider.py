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
        # Fast path: nothing to search.
        if (
            limit <= 0
            or not scope_refs
            or not isinstance(query, str)
        ):
            return await self._fts_provider.search_ranked(
                query, scope_refs, limit, **filters
            )

        # Decide whether vector participates.
        use_vec = self._should_use_vector(query, filters)
        if not use_vec:
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
        fts_coro = self._fts_provider.search_ranked(
            query, scope_refs, limit, **filters
        )
        vec_coro = self._vec_provider.search(
            query=query,
            scope_refs=scope_refs,
            limit=limit,
            min_similarity=0.0,
        )

        fts_result, vec_result = await asyncio.gather(
            fts_coro, vec_coro, return_exceptions=True
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

        # ---- Handle empty-on-one-side ------------------------
        #
        # If only one side produced results, return them
        # verbatim. This preserves each side's native relevance
        # scale, which the ranker downstream understands better
        # than a one-sided RRF normalization would.
        if not vec_result:
            return fts_result
        if not fts_result:
            return vec_result

        # ---- Both sides produced results -- fuse with RRF ---
        return self._rrf_merge(fts_result, vec_result, limit)

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
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def rrf_k(self) -> int:
        """The RRF smoothing constant in use (for diagnostics)."""
        return self._rrf_k

    @property
    def fts_provider(self) -> MemoryProvider:
        """The wrapped FTS provider (for tests + diagnostics)."""
        return self._fts_provider

    @property
    def vec_provider(self) -> SqliteVecProvider:
        """The wrapped vector provider (for tests + diagnostics)."""
        return self._vec_provider

    def __repr__(self) -> str:
        return (
            f"HybridMemoryProvider("
            f"fts={type(self._fts_provider).__name__}, "
            f"vec={type(self._vec_provider).__name__}, "
            f"rrf_k={self._rrf_k})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = ["HybridMemoryProvider"]
