# backend/tests/memory/test_graph_search.py
"""
Phase 6-B acceptance tests: graph search + three-way fusion.

Covers the five required 6B tests plus the guarantees the sub-phase
promises:

    Required
    --------
    1. ``test_find_related_direct_only``
    2. ``test_find_related_depth_2``
    3. ``test_graph_search_returns_empty_for_unknown_entity``
    4. ``test_three_way_rrf_fusion``
    5. ``test_graph_search_disabled_no_extra_calls``

    Supporting
    ----------
    * the ``1 / (1 + distance)`` scoring contract
    * deduplication keeps the BEST (shortest-distance) score
    * ``scope_refs`` is enforced (no cross-scope leak through the
      graph)
    * query n-gram resolution finds multi-word entity names
    * a graph failure degrades to two-way fusion, never to an error
    * ``search_mode`` reports ``hybrid+graph``
    * the late-binding proxy honours the LIVE setting, so disabling
      graph search at runtime takes effect on the next search

Nothing here requires an LLM: the graph path is deterministic by
design (see ``graph_search.py`` invariant #1), which is exactly what
lets these tests be exact rather than probabilistic.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
import pytest_asyncio

from omweb.memory.manager.hybrid_provider import HybridMemoryProvider
from omweb.memory.storage.graph_store import GraphStore
from omweb.memory.storage.migrations import apply_schema
from omweb.memory.storage.repository import SqliteMemoryRepository
from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
)
from peldrun.memory.graph import Entity, Relation
from peldrun.memory.graph.graph_search import GraphSearch, _query_ngrams


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh SQLite file with the memory + graph schema applied."""
    path = tmp_path / "graph_search.db"
    conn = sqlite3.connect(str(path))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return path


@pytest_asyncio.fixture
async def repo(db_path: Path):
    """An open ``SqliteMemoryRepository``; closed on teardown."""
    repository = SqliteMemoryRepository(db_path)
    try:
        yield repository
    finally:
        repository.close_all()


@pytest_asyncio.fixture
async def store(repo: SqliteMemoryRepository):
    """A ``GraphStore`` bound to the test repository."""
    return GraphStore(repo)


def _memory(
    content: str,
    *,
    scope_id: str = "p1",
    scope_type: MemoryScope = MemoryScope.PROJECT,
) -> MemoryEntry:
    """Build a valid, unpersisted entry."""
    return MemoryEntry.create(
        content=content,
        memory_type=MemoryType.SEMANTIC,
        scope_type=scope_type,
        scope_id=scope_id,
        provenance=MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER
        ),
    )


# ---------------------------------------------------------------------------
# Doubles for the hybrid provider
# ---------------------------------------------------------------------------


class _FakeFtsProvider:
    """Only what ``HybridMemoryProvider.search_ranked`` calls."""

    def __init__(
        self,
        results: Optional[List[Tuple[MemoryEntry, float]]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls = 0

    @property
    def is_open(self) -> bool:
        return True

    async def search_ranked(self, query, scope_refs, limit=5, **filters):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [(entry, score) for entry, score in self.results[:limit]]


class _FakeVecProvider:
    """Only what ``HybridMemoryProvider.search_ranked`` calls."""

    def __init__(
        self,
        results: Optional[List[Tuple[MemoryEntry, float]]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls = 0

    async def search(
        self, query, scope_refs, limit=5, min_similarity=0.0
    ):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [(entry, score) for entry, score in self.results[:limit]]


class _FakeGraphSearch:
    """Counts invocations and returns a fixed list."""

    def __init__(
        self,
        results: Optional[List[Tuple[MemoryEntry, float]]] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls = 0
        self.last_scope_refs: Optional[Sequence[str]] = None

    async def find_related_for_query(
        self, query, *, scope_refs=None, limit=20
    ):
        self.calls += 1
        self.last_scope_refs = scope_refs
        if self.error is not None:
            raise self.error
        return list(self.results)


# ---------------------------------------------------------------------------
# GraphSearch: direct links
# ---------------------------------------------------------------------------


async def test_find_related_direct_only(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """``max_depth=0`` returns only the directly linked memories.

    Depth counts HOPS from the resolved entity:
      0 -> direct links only (score 1.0),
      1 -> plus direct neighbours (score 0.5).
    """
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    project = await store.add_entity(Entity.build("PROJECT", "Peldrun"))

    direct = await repo.create(_memory("I work at AcmeCorp."))
    neighbour = await repo.create(_memory("Peldrun is our product."))

    await store.link_memory_to_entity(direct.memory_id, acme.entity_id)
    await store.link_memory_to_entity(
        neighbour.memory_id, project.entity_id
    )
    await store.add_relation(
        Relation.build(acme.entity_id, project.entity_id, "uses")
    )

    searcher = GraphSearch(store, max_depth=0)
    results = await searcher.find_related("AcmeCorp")

    ids = [entry.memory_id for entry, _score in results]
    assert ids == [direct.memory_id]
    assert results[0][1] == 1.0

    # One hop reaches the neighbour's memory at score 0.5.
    one_hop = await GraphSearch(store, max_depth=1).find_related(
        "AcmeCorp"
    )
    by_id = {entry.memory_id: score for entry, score in one_hop}
    assert by_id[direct.memory_id] == 1.0
    assert by_id[neighbour.memory_id] == pytest.approx(0.5)


async def test_find_related_depth_2(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """Depth 2 reaches a two-hop node, scored 1/(1+distance).

    Chain:  AcmeCorp --uses--> Peldrun --related_to--> VectorSearch
    From AcmeCorp: Peldrun is distance 1, VectorSearch distance 2.
    """
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    project = await store.add_entity(Entity.build("PROJECT", "Peldrun"))
    topic = await store.add_entity(Entity.build("TOPIC", "VectorSearch"))

    direct = await repo.create(_memory("I work at AcmeCorp."))
    one_hop = await repo.create(_memory("Peldrun is our product."))
    two_hop = await repo.create(_memory("VectorSearch powers Peldrun."))

    await store.link_memory_to_entity(direct.memory_id, acme.entity_id)
    await store.link_memory_to_entity(
        one_hop.memory_id, project.entity_id
    )
    await store.link_memory_to_entity(two_hop.memory_id, topic.entity_id)
    await store.add_relation(
        Relation.build(acme.entity_id, project.entity_id, "uses")
    )
    await store.add_relation(
        Relation.build(
            project.entity_id, topic.entity_id, "related_to"
        )
    )

    searcher = GraphSearch(store, max_depth=2)
    results = await searcher.find_related("AcmeCorp")
    by_id = {entry.memory_id: score for entry, score in results}

    assert set(by_id) == {
        direct.memory_id,
        one_hop.memory_id,
        two_hop.memory_id,
    }
    assert by_id[direct.memory_id] == 1.0
    assert by_id[one_hop.memory_id] == pytest.approx(0.5)
    assert by_id[two_hop.memory_id] == pytest.approx(1.0 / 3.0)
    # Ordered best-first by distance.
    assert [entry.memory_id for entry, _ in results] == [
        direct.memory_id,
        one_hop.memory_id,
        two_hop.memory_id,
    ]

    # Depth 0 sees only the direct hit for the same graph.
    shallow = await GraphSearch(store, max_depth=0).find_related(
        "AcmeCorp"
    )
    assert [entry.memory_id for entry, _ in shallow] == [
        direct.memory_id
    ]


async def test_score_keeps_best_distance(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """A memory reachable both directly and via a hop scores 1.0."""
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    project = await store.add_entity(Entity.build("PROJECT", "Peldrun"))
    entry = await repo.create(_memory("AcmeCorp builds Peldrun."))

    # Linked to BOTH the start entity (distance 0) and the far end
    # of a relation (distance 1).
    await store.link_memory_to_entity(entry.memory_id, acme.entity_id)
    await store.link_memory_to_entity(entry.memory_id, project.entity_id)
    await store.add_relation(
        Relation.build(acme.entity_id, project.entity_id, "uses")
    )

    results = await GraphSearch(store, max_depth=2).find_related(
        "AcmeCorp"
    )
    assert len(results) == 1
    assert results[0][1] == 1.0


async def test_graph_search_returns_empty_for_unknown_entity(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """Unknown names are a normal miss, not an error."""
    await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    searcher = GraphSearch(store)

    assert await searcher.find_related("Nonexistent Thing") == []
    assert await searcher.find_related("") == []
    assert await searcher.find_related("   ") == []
    assert (
        await searcher.find_related_for_query("who is the manager?")
        == []
    )


# ---------------------------------------------------------------------------
# GraphSearch: scope + query resolution
# ---------------------------------------------------------------------------


async def test_scope_refs_enforced_through_graph(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """Graph results never cross a scope boundary."""
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    in_scope = await repo.create(_memory("AcmeCorp is my employer."))
    other = await repo.create(
        _memory("AcmeCorp is a vendor.", scope_id="p2")
    )
    await store.link_memory_to_entity(in_scope.memory_id, acme.entity_id)
    await store.link_memory_to_entity(other.memory_id, acme.entity_id)

    searcher = GraphSearch(store)

    # No restriction (diagnostics) sees both.
    assert len(await searcher.find_related("AcmeCorp")) == 2

    scoped = await searcher.find_related(
        "AcmeCorp", scope_refs=["project:p1"]
    )
    assert [entry.memory_id for entry, _ in scoped] == [
        in_scope.memory_id
    ]

    # Empty scope list = match nothing, never "no filter".
    assert (
        await searcher.find_related("AcmeCorp", scope_refs=[]) == []
    )


async def test_query_ngram_resolution(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """A multi-word entity is found inside a natural-language query."""
    project = await store.add_entity(
        Entity.build("PROJECT", "Peldrun Memory")
    )
    entry = await repo.create(_memory("Peldrun Memory is the platform."))
    await store.link_memory_to_entity(entry.memory_id, project.entity_id)

    searcher = GraphSearch(store)
    entities = await searcher.resolve_query_entities(
        "what did we decide about Peldrun Memory last week?"
    )
    assert [entity.canonical_name for entity in entities] == [
        "peldrun memory"
    ]

    results = await searcher.find_related_for_query(
        "what did we decide about Peldrun Memory last week?"
    )
    assert [entry.memory_id for entry, _ in results] == [
        entry.memory_id
    ]


def test_query_ngrams_prefers_longest_spans() -> None:
    """Longer spans are emitted first so the budget buys specificity."""
    grams = _query_ngrams("the Peldrun Memory Platform rocks")
    assert grams[0] == "peldrun memory platform"
    assert "peldrun memory" in grams
    assert "peldrun" in grams
    # Single-token stopwords are dropped; multi-token spans keep them.
    assert "the" not in grams
    assert _query_ngrams("") == []
    assert _query_ngrams("a I") == []


# ---------------------------------------------------------------------------
# Three-way fusion
# ---------------------------------------------------------------------------


async def _entry(memory_id: str, content: str = "x") -> MemoryEntry:
    """Build a detached entry with a chosen id (fusion tests only)."""
    entry = _memory(content)
    return entry.model_copy(update={"memory_id": memory_id})


async def test_three_way_rrf_fusion() -> None:
    """All three sources contribute; consensus entries win."""
    shared = await _entry("mem_shared")
    fts_only = await _entry("mem_fts")
    vec_only = await _entry("mem_vec")
    graph_only = await _entry("mem_graph")

    fts = _FakeFtsProvider([(shared, 0.9), (fts_only, 0.8)])
    vec = _FakeVecProvider([(shared, 0.9), (vec_only, 0.85)])
    graph = _FakeGraphSearch([(shared, 1.0), (graph_only, 0.5)])

    hybrid = HybridMemoryProvider(
        fts_provider=fts,
        vec_provider=vec,
        graph_search=graph,
        # Identity calibration isolates fusion mechanics from the
        # PROVISIONAL per-model cosine bounds: fake cosines pass
        # through unchanged, so score assertions below stay exact
        # even after real floor/ceil calibration lands.
        vec_score_floor=0.0,
        vec_score_ceil=1.0,
    )
    # G1: the hybrid gates on QueryIntent before any source runs.
    # A bare "q" is NO_INTENT (single-char token drops out of the
    # normalizer), which correctly routes to FTS-only. Fusion tests
    # must use a real LOOKUP query.
    results = await hybrid.search_ranked(
        "peldrun memory", ["project:p1"], limit=10
    )

    ids = [entry.memory_id for entry, _score in results]
    assert set(ids) == {
        "mem_shared",
        "mem_fts",
        "mem_vec",
        "mem_graph",
    }
    # The entry found by ALL three sources ranks first.
    assert ids[0] == "mem_shared"
    # G2: relevance is ABSOLUTE (max over sources), never the RRF
    # score and never normalized against the best candidate.
    by_id = {entry.memory_id: score for entry, score in results}
    assert by_id["mem_shared"] == pytest.approx(1.0)   # graph link score
    assert by_id["mem_fts"] == pytest.approx(0.8)      # lexical stays 0.8
    assert by_id["mem_vec"] == pytest.approx(0.85)     # identity-calibrated
    assert by_id["mem_graph"] == pytest.approx(0.5)
    assert all(0.0 <= score <= 1.0 for _entry_, score in results)
    # The graph source was consulted with the live scope set.
    assert graph.calls == 1
    assert graph.last_scope_refs == ["project:p1"]
    assert hybrid.last_search_mode == "hybrid+graph"


async def test_graph_only_results_report_graph_mode() -> None:
    """When only the graph contributes, the mode says so honestly."""
    graph_only = await _entry("mem_graph")
    hybrid = HybridMemoryProvider(
        fts_provider=_FakeFtsProvider([]),
        vec_provider=_FakeVecProvider([]),
        graph_search=_FakeGraphSearch([(graph_only, 1.0)]),
    )
    # G1: a LOOKUP query is required to reach the fusion path ("q"
    # is NO_INTENT and would delegate to FTS-only).
    results = await hybrid.search_ranked(
        "peldrun memory", ["project:p1"], limit=5
    )
    assert [entry.memory_id for entry, _ in results] == ["mem_graph"]
    assert hybrid.last_search_mode == "graph"


async def test_graph_failure_degrades_to_two_way_fusion() -> None:
    """A raising graph source must not fail the search."""
    left = await _entry("mem_fts")
    right = await _entry("mem_vec")
    hybrid = HybridMemoryProvider(
        fts_provider=_FakeFtsProvider([(left, 0.9)]),
        vec_provider=_FakeVecProvider([(right, 0.9)]),
        graph_search=_FakeGraphSearch(
            error=RuntimeError("graph exploded")
        ),
    )
    # G1: LOOKUP query so all three sources actually run.
    results = await hybrid.search_ranked(
        "peldrun memory", ["project:p1"], limit=5
    )
    assert {entry.memory_id for entry, _ in results} == {
        "mem_fts",
        "mem_vec",
    }
    # Falls back to the Phase 5 tag -- the graph contributed nothing.
    assert hybrid.last_search_mode == "hybrid"


async def test_graph_recalls_memory_not_textually_similar(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """6B acceptance: the graph recalls what text search cannot reach.

    The stored memory shares no vocabulary with the question -- only
    an ENTITY. That is exactly the gap Phase 6 exists to close.
    """
    ahmed = await store.add_entity(Entity.build("PERSON", "Ahmed"))
    entry = await repo.create(
        _memory("The deadline moved to the end of March.")
    )
    await store.link_memory_to_entity(entry.memory_id, ahmed.entity_id)

    question = "what did Ahmed say about the schedule?"

    searcher = GraphSearch(store, max_depth=2)
    graph_hits = await searcher.find_related_for_query(
        question, scope_refs=["project:p1"]
    )
    assert [e.memory_id for e, _ in graph_hits] == [entry.memory_id]

    # Lexical and vector both find NOTHING; only the graph does.
    hybrid = HybridMemoryProvider(
        fts_provider=_FakeFtsProvider([]),
        vec_provider=_FakeVecProvider([]),
        graph_search=searcher,
    )
    results = await hybrid.search_ranked(
        question, ["project:p1"], limit=5
    )
    assert [entry.memory_id for entry, _ in results] == [
        entry.memory_id
    ]
    assert hybrid.last_search_mode == "graph"


# ---------------------------------------------------------------------------
# Disabled / no-extra-work guarantees
# ---------------------------------------------------------------------------


async def test_graph_search_disabled_no_extra_calls() -> None:
    """With no graph source, the search behaves exactly as Phase 5."""
    left = await _entry("mem_fts")
    right = await _entry("mem_vec")
    fts = _FakeFtsProvider([(left, 0.9)])
    vec = _FakeVecProvider([(right, 0.9)])

    hybrid = HybridMemoryProvider(
        fts_provider=fts, vec_provider=vec, graph_search=None
    )
    assert hybrid.graph_search is None

    # G1: LOOKUP query so the two-way path actually runs.
    results = await hybrid.search_ranked(
        "peldrun memory", ["project:p1"], limit=5
    )
    assert {entry.memory_id for entry, _ in results} == {
        "mem_fts",
        "mem_vec",
    }
    assert hybrid.last_search_mode == "hybrid"
    assert fts.calls == 1
    assert vec.calls == 1


async def test_lazy_proxy_respects_live_flag() -> None:
    """The service proxy honours the CURRENT setting on every search."""
    from omweb.memory.manager.memory_service import (
        _LazyGraphSearch,
    )

    class _Settings:
        def __init__(self, enabled: bool) -> None:
            self.retrieval = type(
                "R", (), {"graph_search_enabled": enabled}
            )()

    class _Lifecycle:
        def __init__(self, enabled: bool) -> None:
            self.settings = _Settings(enabled)

    class _Service:
        def __init__(self, enabled: bool, searcher: Any) -> None:
            self._lifecycle = _Lifecycle(enabled)
            self._graph_search = searcher

    entry = await _entry("mem_graph")
    inner = _FakeGraphSearch([(entry, 1.0)])

    proxy = _LazyGraphSearch(_Service(True, inner))
    assert await proxy.find_related_for_query("q") == [(entry, 1.0)]
    assert inner.calls == 1

    # Flag flipped OFF at runtime: the very next search stops using
    # the graph, even though the searcher still exists.
    disabled = _LazyGraphSearch(_Service(False, inner))
    assert await disabled.find_related_for_query("q") == []
    assert inner.calls == 1  # not called again

    # No searcher installed: also a clean empty result.
    empty = _LazyGraphSearch(_Service(True, None))
    assert await empty.find_related_for_query("q") == []
    assert empty.is_available is False


# ---------------------------------------------------------------------------
# End-to-end: service wiring for graph search
# ---------------------------------------------------------------------------


async def test_service_graph_search_is_wired_into_hybrid(
    tmp_path: Path,
) -> None:
    """With embedding enabled the hybrid carries a graph source.

    Embedding is not actually exercised (no model configured), so
    ``HybridMemoryProvider`` is never built here; instead this test
    asserts the SERVICE-level contract: the graph store/searcher are
    built from the settings flag alone, with no LLM involved.
    """
    from omweb.memory.manager.memory_service import MemoryService
    from omweb.memory.manager.memory_settings_manager import (
        MemorySettingsManager,
    )
    from peldrun.memory.domain import default_settings

    db = tmp_path / "svc.db"
    settings_path = tmp_path / "svc.json"

    base = default_settings()
    retrieval = base.retrieval.model_copy(
        update={"graph_search_enabled": True}
    )
    settings = base.model_copy(update={"retrieval": retrieval})
    await MemorySettingsManager(path=settings_path).save(settings)

    service = MemoryService(db_path=db, settings_path=settings_path)
    await service.ainitialize()
    try:
        assert service.graph_search is not None
        assert service.graph_search.max_depth == 2
        # Read half only: no indexing, because the AI flags are off.
        assert service.graph_enabled is False
        assert service._graph_backfill_task is None
    finally:
        await service.aclose()
        assert service.graph_search is None


async def test_service_graph_search_off_by_default(
    tmp_path: Path,
) -> None:
    """Default settings build no graph objects at all."""
    from omweb.memory.manager.memory_service import MemoryService
    from omweb.memory.manager.memory_settings_manager import (
        MemorySettingsManager,
    )
    from peldrun.memory.domain import default_settings

    db = tmp_path / "svc2.db"
    settings_path = tmp_path / "svc2.json"
    await MemorySettingsManager(path=settings_path).save(
        default_settings()
    )

    service = MemoryService(db_path=db, settings_path=settings_path)
    await service.ainitialize()
    try:
        assert service.graph_search is None
        assert service.graph_store is None
        assert service.graph_enabled is False
    finally:
        await service.aclose()
