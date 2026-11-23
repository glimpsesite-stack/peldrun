# backend/tests/memory/test_hybrid_intent_and_scores.py
"""Regression tests for gaps G1/G2 (audit R1/R2, hybrid retrieval site).

G1 -- QueryIntent gate in ``HybridMemoryProvider.search_ranked``:
    NO_INTENT queries ("thank you") must reach the FTS provider ONLY
    (which returns zero matches per the repository R1 contract), and
    must NEVER invoke the vector backend, the graph source, or the
    reranker -- in EVERY retrieval mode, including mode="vector".
    BLANK queries ("" / whitespace / punctuation) must keep delegating
    to FTS with filters intact: the write pipeline's dedupe pool
    (``search("", status="all")``) depends on the recency listing.

G2 -- absolute relevance in the hybrid path:
    RRF decides ORDER only. The attached score is the max of the
    absolute lexical coverage and the calibrated vector cosine
    ``clamp((cos - floor) / (ceil - floor), 0, 1)`` -- never the RRF
    score, never normalized against the best candidate. Vector-only
    branches (mode="vector" and the empty-FTS fallback) calibrate too.

Conventions
-----------
* ASCII-only test data (same rule as test_regression_known_defects).
* Calibration bounds are PINNED per test (floor=0.35, ceil=0.85) so
  these tests guard the MECHANISM and stay valid when per-model
  calibration replaces the provisional module defaults.
* Call-counting doubles prove sources were not merely ignored but
  never invoked (the latency + cost half of the G1 contract).
* ``test_premises_query_classification`` pins the normalizer behavior
  the gate relies on, so normalizer drift fails loudly here instead
  of silently voiding the gate tests.

Run from ``backend``::

    python -m pytest tests/memory/test_hybrid_intent_and_scores.py -q
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from omweb.memory.manager.hybrid_provider import HybridMemoryProvider
from omweb.memory.storage.normalizer import QueryIntent, classify_query
from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
)

_REFS: List[str] = ["project:p1"]
_LOOKUP: str = "peldrun memory"  # verified: QueryIntent.LOOKUP
_FLOOR: float = 0.35             # pinned test calibration bounds
_CEIL: float = 0.85


# ---------------------------------------------------------------------------
# Call-counting doubles (only the surface the hybrid actually calls)
# ---------------------------------------------------------------------------

class _FtsDouble:
    """Records calls, queries and filters; returns a fixed ranked list."""

    def __init__(
        self, results: Optional[List[Tuple[MemoryEntry, float]]] = None
    ) -> None:
        self.results = list(results or [])
        self.calls = 0
        self.last_query: Optional[str] = None
        self.last_filters: Optional[Dict[str, Any]] = None

    @property
    def is_open(self) -> bool:
        return True

    async def search_ranked(self, query, scope_refs, limit=5, **filters):
        self.calls += 1
        self.last_query = query
        self.last_filters = dict(filters)
        return list(self.results[:limit])


class _VecDouble:
    """Records calls and the forwarded cosine floor (R4 contract)."""

    def __init__(
        self, results: Optional[List[Tuple[MemoryEntry, float]]] = None
    ) -> None:
        self.results = list(results or [])
        self.calls = 0
        self.last_min_similarity: Optional[float] = None

    async def search(self, *, query, scope_refs, limit=5, min_similarity=0.0):
        self.calls += 1
        self.last_min_similarity = min_similarity
        return list(self.results[:limit])


class _GraphDouble:
    """Records calls; returns a fixed hit list."""

    def __init__(
        self, results: Optional[List[Tuple[MemoryEntry, float]]] = None
    ) -> None:
        self.results = list(results or [])
        self.calls = 0

    async def find_related_for_query(self, query, *, scope_refs=None, limit=20):
        self.calls += 1
        return list(self.results)


class _RerankerDouble:
    """No-op reranker that only counts invocations."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_rerank_succeeded = False

    async def rerank(self, query, candidates, top_k=5):
        self.calls += 1
        return list(candidates[:top_k])


def _entry(memory_id: str) -> MemoryEntry:
    """Detached entry with a deterministic id (fusion tests only)."""
    base = MemoryEntry.create(
        content="peldrun memory platform note",
        memory_type=MemoryType.SEMANTIC,
        scope_type=MemoryScope.PROJECT,
        scope_id="p1",
        provenance=MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER
        ),
    )
    return base.model_copy(update={"memory_id": memory_id})


def _hybrid(
    fts: _FtsDouble,
    vec: _VecDouble,
    *,
    graph: Optional[_GraphDouble] = None,
    reranker: Optional[_RerankerDouble] = None,
    mode: str = "hybrid",
    floor: float = _FLOOR,
    ceil: float = _CEIL,
    min_vector_similarity: float = 0.0,
) -> HybridMemoryProvider:
    """Build the provider with PINNED calibration bounds (see docstring)."""
    return HybridMemoryProvider(
        fts_provider=fts,
        vec_provider=vec,
        graph_search=graph,
        reranker=reranker,
        mode=mode,
        min_vector_similarity=min_vector_similarity,
        vec_score_floor=floor,
        vec_score_ceil=ceil,
    )


# ---------------------------------------------------------------------------
# Premises: pin the classification the G1 gate consumes
# ---------------------------------------------------------------------------

def test_premises_query_classification() -> None:
    assert classify_query(_LOOKUP) is QueryIntent.LOOKUP
    assert classify_query("thank you") is QueryIntent.NO_INTENT
    assert classify_query("hello") is QueryIntent.NO_INTENT
    assert classify_query("") is QueryIntent.BLANK
    assert classify_query("?!") is QueryIntent.BLANK


# ---------------------------------------------------------------------------
# G1: the intent gate precedes every source, in every mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", ["thank you", "hello"])
async def test_G1_no_intent_skips_vector_graph_reranker(query: str) -> None:
    """NO_INTENT delegates to FTS only; no other source is even called."""
    fts = _FtsDouble([])  # repository R1 contract: zero matches
    vec = _VecDouble([(_entry("m_vec"), 0.9)])
    graph = _GraphDouble([(_entry("m_graph"), 1.0)])
    reranker = _RerankerDouble()
    hybrid = _hybrid(fts, vec, graph=graph, reranker=reranker)

    results = await hybrid.search_ranked(query, _REFS, limit=5)

    assert results == []
    assert fts.calls == 1
    assert vec.calls == 0, "embedding HTTP call must be skipped entirely"
    assert graph.calls == 0
    assert reranker.calls == 0
    assert hybrid.last_search_mode == "fts"


async def test_G1_no_intent_in_vector_mode_never_embeds() -> None:
    """The gate must run BEFORE the mode dispatch, not after it."""
    fts = _FtsDouble([])
    vec = _VecDouble([(_entry("m_vec"), 0.9)])
    hybrid = _hybrid(fts, vec, mode="vector")

    results = await hybrid.search_ranked("thank you", _REFS, limit=5)

    assert results == []
    assert vec.calls == 0, "mode='vector' must not bypass the intent gate"
    assert fts.calls == 1
    assert hybrid.last_search_mode == "fts"


async def test_G1_blank_query_preserves_recents_and_filters() -> None:
    """BLANK keeps the recency listing AND forwards filters verbatim.

    The write pipeline's dedupe pool calls search("", status="all");
    losing the entries or the status filter would break REACTIVATE.
    """
    for query in ("", "   ", "?!"):
        fts = _FtsDouble([(_entry("m_a"), 1.0), (_entry("m_b"), 0.8)])
        vec = _VecDouble([(_entry("m_vec"), 0.9)])
        hybrid = _hybrid(fts, vec)

        results = await hybrid.search_ranked(
            query, _REFS, limit=500, status="all"
        )

        assert [e.memory_id for e, _ in results] == ["m_a", "m_b"], query
        assert [s for _, s in results] == [1.0, 0.8], query
        assert fts.calls == 1
        assert fts.last_filters is not None
        assert fts.last_filters.get("status") == "all", query
        assert vec.calls == 0
        assert hybrid.last_search_mode == "fts"


async def test_G1_lookup_runs_all_sources_and_reranker() -> None:
    """Counter-test: the gate must not over-block a real LOOKUP query."""
    fts = _FtsDouble([(_entry("m_fts"), 0.9)])
    vec = _VecDouble([(_entry("m_vec"), 0.9)])
    graph = _GraphDouble([(_entry("m_graph"), 1.0)])
    reranker = _RerankerDouble()
    hybrid = _hybrid(fts, vec, graph=graph, reranker=reranker)

    results = await hybrid.search_ranked(_LOOKUP, _REFS, limit=5)

    assert {e.memory_id for e, _ in results} == {
        "m_fts", "m_vec", "m_graph",
    }
    assert fts.calls == 1
    assert vec.calls == 1
    assert graph.calls == 1
    assert reranker.calls == 1
    assert hybrid.last_search_mode == "hybrid+graph"


# ---------------------------------------------------------------------------
# G2: RRF for order, absolute calibrated scores for value
# ---------------------------------------------------------------------------

async def test_G2_weak_best_match_keeps_absolute_score() -> None:
    """The best candidate is NOT forced to 1.0; weak entries stay weak."""
    fts = _FtsDouble([(_entry("m_a"), 0.42), (_entry("m_b"), 0.10)])
    vec = _VecDouble([(_entry("m_a"), 0.50), (_entry("m_c"), 0.40)])
    hybrid = _hybrid(fts, vec)

    results = await hybrid.search_ranked(_LOOKUP, _REFS, limit=5)
    by_id = {e.memory_id: s for e, s in results}

    # Consensus entry first; tie between m_b/m_c broken by id ascending.
    assert [e.memory_id for e, _ in results] == ["m_a", "m_b", "m_c"]
    # max(lexical 0.42, calibrated cosine 0.30) -- never normalized.
    assert by_id["m_a"] == pytest.approx(0.42, abs=1e-9)
    assert by_id["m_a"] < 1.0
    assert by_id["m_b"] == pytest.approx(0.10, abs=1e-9)
    # Vector-only entry: calibrated (0.40 -> 0.10), rejectable by the
    # default min_relevance_score=0.3 threshold.
    assert by_id["m_c"] == pytest.approx(0.10, abs=1e-9)
    assert by_id["m_c"] < 0.30
    assert hybrid.last_search_mode == "hybrid"


async def test_G2_vector_only_fallback_calibrates() -> None:
    """Empty-FTS fallback must emit calibrated scores, not raw cosine."""
    fts = _FtsDouble([])
    vec = _VecDouble([(_entry("m_a"), 0.90), (_entry("m_b"), 0.30)])
    hybrid = _hybrid(fts, vec)

    results = await hybrid.search_ranked(_LOOKUP, _REFS, limit=5)

    assert hybrid.last_search_mode == "vector"
    # Raw cosine would have been [0.9, 0.3]; calibration maps to [1, 0].
    assert [s for _, s in results] == [
        pytest.approx(1.0, abs=1e-9),
        pytest.approx(0.0, abs=1e-9),
    ]


async def test_G2_vector_mode_calibrates_and_forwards_threshold() -> None:
    """mode='vector': calibrated score + min_vector_similarity forwarded."""
    fts = _FtsDouble([(_entry("m_fts"), 0.9)])
    vec = _VecDouble([(_entry("m_a"), 0.60)])
    hybrid = _hybrid(fts, vec, mode="vector", min_vector_similarity=0.42)

    results = await hybrid.search_ranked(_LOOKUP, _REFS, limit=5)

    assert fts.calls == 0
    assert vec.calls == 1
    assert vec.last_min_similarity == pytest.approx(0.42)
    assert hybrid.last_search_mode == "vector"
    assert results[0][1] == pytest.approx(0.5, abs=1e-9)


@pytest.mark.parametrize(
    "cosine,expected",
    [
        (0.35, 0.0),   # at floor -> no evidence
        (0.85, 1.0),   # at ceil  -> full evidence
        (0.60, 0.5),   # midpoint
        (0.10, 0.0),   # below floor clamps to 0
        (0.99, 1.0),   # above ceil clamps to 1
    ],
)
async def test_G2_calibration_boundaries(
    cosine: float, expected: float
) -> None:
    fts = _FtsDouble([])
    vec = _VecDouble([(_entry("m_a"), cosine)])
    hybrid = _hybrid(fts, vec, mode="vector")

    results = await hybrid.search_ranked(_LOOKUP, _REFS, limit=5)

    assert results[0][1] == pytest.approx(expected, abs=1e-9)


async def test_G2_rrf_order_is_consensus_value_is_absolute() -> None:
    """Sharpest G2 guard: ORDER and VALUE are deliberately decoupled.

    m_a appears in BOTH lists (consensus -> ranks first) but its
    absolute relevance is only 0.20; m_b is lexical-only with 0.95.
    Old code normalized m_a to 1.0; value-sorting would put m_b first.
    """
    fts = _FtsDouble([(_entry("m_a"), 0.20), (_entry("m_b"), 0.95)])
    vec = _VecDouble([(_entry("m_a"), 0.36)])  # calibrated -> 0.02
    hybrid = _hybrid(fts, vec)

    results = await hybrid.search_ranked(_LOOKUP, _REFS, limit=5)
    by_id = {e.memory_id: s for e, s in results}

    assert [e.memory_id for e, _ in results] == ["m_a", "m_b"]
    assert by_id["m_a"] == pytest.approx(0.20, abs=1e-9)
    assert by_id["m_b"] == pytest.approx(0.95, abs=1e-9)


@pytest.mark.parametrize(
    "floor,ceil",
    [
        (0.8, 0.8),            # equal bounds -> zero span
        (0.9, 0.3),            # inverted bounds
        (float("nan"), 0.9),   # non-finite floor
        (0.1, float("inf")),   # non-finite ceil
        (float("-inf"), 0.9),  # non-finite floor (negative)
    ],
)
def test_G2_invalid_calibration_bounds_rejected(
    floor: float, ceil: float
) -> None:
    with pytest.raises(ValueError):
        _hybrid(_FtsDouble([]), _VecDouble([]), floor=floor, ceil=ceil)
