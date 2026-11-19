# backend/tests/memory/test_graph_foundation.py
"""
Phase 6-A acceptance tests: knowledge graph foundation.

Covers the five required 6A tests plus the supporting guarantees the
sub-phase promises:

    Required
    --------
    1. ``test_migration_v6_idempotent``
    2. ``test_add_entity_and_relation``
    3. ``test_traverse_depth_2``
    4. ``test_entity_extractor_parses_llm_output``
    5. ``test_entity_extractor_graceful_on_llm_failure``

    Supporting
    ----------
    * entity identity is ``(entity_type, canonical_name)``
    * aliases union across observations
    * relation idempotency keeps the FIRST observation
    * the v6 foreign keys reject dangling relations
    * graph hydration honours ``scope_refs`` (no cross-scope leak)
    * ``GraphIndexer`` is idempotent, bounded, and never raises
    * ``reindex_memory`` clears stale links
    * an unconfigured extractor makes indexing a cheap no-op

Every test uses its own SQLite file via ``tmp_path``; nothing touches
the production database. No test needs a real LLM -- the gateway is a
small in-file double, which is also what lets the failure paths be
exercised deterministically.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio

from omweb.memory.graph.indexer import GraphIndexer, canonical_pairs
from omweb.memory.storage.graph_store import GraphStore
from omweb.memory.storage.migrations import (
    CURRENT_VERSION,
    apply_schema,
    schema_status,
)
from omweb.memory.storage.repository import SqliteMemoryRepository
from peldrun.memory.domain import (
    MemoryEntry,
    MemoryMode,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
    default_settings,
)
from peldrun.memory.graph import Entity, Relation
from peldrun.memory.graph.entity_extractor import EntityExtractor


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeGateway:
    """Minimal stand-in for ``MemoryModelGateway``.

    Implements the single coroutine ``StructuredGatewayProtocol``
    declares, and records every call so tests can assert on the
    prompt/provider wiring. ``response`` is returned verbatim (a dict
    is accepted by ``extract_parsed_object``), ``error`` is raised
    instead when set, and ``delay`` simulates a slow provider.
    """

    def __init__(
        self,
        response: Any = None,
        *,
        error: Optional[BaseException] = None,
        delay: float = 0.0,
    ) -> None:
        self.response = response
        self.error = error
        self.delay = delay
        self.calls: List[Dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response


class _FakeResponse:
    """Mirrors the production ``LLMStructuredResponse`` shape."""

    def __init__(self, parsed: Dict[str, Any]) -> None:
        self.parsed = parsed
        self.raw_text = ""
        self.cost_nano_usd = 0
        self.input_tokens = 0
        self.output_tokens = 0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh SQLite file with the memory schema applied."""
    path = tmp_path / "graph_test.db"
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


def _make_memory(
    content: str,
    *,
    scope_id: str = "p1",
    scope_type: MemoryScope = MemoryScope.PROJECT,
) -> MemoryEntry:
    """Build a valid, unpasted ``MemoryEntry`` for the test scope."""
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
# 1. Migration
# ---------------------------------------------------------------------------


def test_migration_v6_idempotent(db_path: Path) -> None:
    """``apply_schema`` reaches v6 and re-running it is a no-op.

    Three properties matter:

      * the version lands at ``CURRENT_VERSION`` (6),
      * a SECOND call returns the same version and raises nothing
        (this is the crash-recovery guarantee),
      * the three graph tables and their indexes exist.
    """
    assert CURRENT_VERSION == 6

    conn = sqlite3.connect(str(db_path))
    try:
        first = apply_schema(conn)
        second = apply_schema(conn)
        third = apply_schema(conn)

        assert first == 6
        assert second == 6
        assert third == 6

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "graph_entities",
            "graph_relations",
            "memory_entity_links",
        } <= tables

        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert {
            "idx_entities_canonical",
            "idx_relations_unique",
            "idx_relations_source",
            "idx_relations_target",
            "idx_memory_entity_links_entity",
        } <= indexes

        # Every version 1..6 is recorded exactly once.
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM memory_schema_version "
                "ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6]

        status = schema_status(conn)
        assert status["current_version"] == 6
        assert status["up_to_date"] is True
        assert status["graph_entities_count"] == 0
        assert status["graph_relations_count"] == 0
        assert status["memory_entity_links_count"] == 0
    finally:
        conn.close()


def test_migration_v6_upgrades_a_v4_database(db_path: Path) -> None:
    """A database already at v4 gains the graph tables without loss.

    Simulates the real upgrade path: apply, delete the v5/v6
    bookkeeping rows and the graph tables, then re-apply. The
    pre-existing ``memories`` row must survive.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            INSERT INTO memories (
                memory_id, memory_type, scope_type, scope_id,
                content, normalized_content, tags_json, importance,
                confidence, status, is_pinned, source_type,
                content_hash, created_at, updated_at
            ) VALUES (
                'mem_legacy', 'semantic', 'project', 'p1',
                'legacy row', 'legacy row', '[]', 50,
                1.0, 'active', 0, 'explicit_user',
                'hash_legacy', ?, ?
            )
            """,
            (time.time(), time.time()),
        )
        conn.execute(
            "DELETE FROM memory_schema_version WHERE version >= 5"
        )
        conn.execute("DROP TABLE memory_entity_links")
        conn.execute("DROP TABLE graph_relations")
        conn.execute("DROP TABLE graph_entities")
        conn.commit()
        assert apply_schema(conn) == 6
        assert apply_schema(conn) == 6

        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE memory_id='mem_legacy'"
        ).fetchone()[0]
        assert count == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM graph_entities"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Entity + relation CRUD
# ---------------------------------------------------------------------------


async def test_add_entity_and_relation(store: GraphStore) -> None:
    """Entities and relations round-trip, are idempotent, and dedupe."""
    ahmed = await store.add_entity(Entity.build("PERSON", "Ahmed"))
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))

    assert ahmed.entity_id.startswith("ent_")
    assert ahmed.canonical_name == "ahmed"
    assert ahmed.entity_type == "PERSON"

    relation = await store.add_relation(
        Relation.build(
            ahmed.entity_id,
            acme.entity_id,
            "works_at",
            source_memory_id="mem_1",
            confidence=0.9,
        )
    )
    assert relation.relation_type == "works_at"
    assert relation.confidence == 0.9
    assert relation.source_memory_id == "mem_1"

    # Idempotency: the same triple cannot create a second row, and
    # the FIRST observation's attributes win.
    again = await store.add_relation(
        Relation.build(
            ahmed.entity_id,
            acme.entity_id,
            "works_at",
            confidence=0.2,
        )
    )
    assert again.relation_id == relation.relation_id
    assert again.confidence == 0.9
    assert await store.count_relations() == 1

    # Identity is (type, canonical): same name, different type is a
    # different node.
    place = await store.add_entity(Entity.build("PLACE", "Ahmed"))
    assert place.entity_id != ahmed.entity_id
    assert await store.count_entities() == 3

    # Re-adding the same identity returns the same persisted row.
    repeat = await store.add_entity(Entity.build("person", "  AHMED  "))
    assert repeat.entity_id == ahmed.entity_id
    assert await store.count_entities() == 3


async def test_aliases_union_across_observations(
    store: GraphStore,
) -> None:
    """An existing entity accumulates aliases instead of replacing them."""
    first = await store.add_entity(
        Entity.build("PERSON", "Ahmed").with_alias("Ahmed Ali")
    )
    assert "Ahmed Ali" in first.aliases

    second = await store.add_entity(
        Entity.build("PERSON", "Ahmed").with_alias("A. Ali")
    )
    assert "Ahmed Ali" in second.aliases
    assert "A. Ali" in second.aliases
    assert await store.count_entities() == 1


async def test_relation_requires_existing_endpoints(
    store: GraphStore,
) -> None:
    """The v6 foreign keys reject a relation to a missing entity."""
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    with pytest.raises(sqlite3.IntegrityError):
        await store.add_relation(
            Relation.build("ent_does_not_exist", acme.entity_id, "uses")
        )


async def test_find_entity(store: GraphStore) -> None:
    """Lookup folds the name and honours the optional type filter."""
    ahmed = await store.add_entity(Entity.build("PERSON", "Ahmed"))
    await store.add_entity(Entity.build("PLACE", "Ahmed"))

    found = await store.find_entity("  AHMED ")
    assert found is not None
    assert found.canonical_name == "ahmed"

    typed = await store.find_entity("Ahmed", entity_type="PLACE")
    assert typed is not None
    assert typed.entity_type == "PLACE"
    assert typed.entity_id != ahmed.entity_id

    assert await store.find_entity("Nobody") is None
    assert await store.find_entity("   ") is None


# ---------------------------------------------------------------------------
# 3. Traversal
# ---------------------------------------------------------------------------


async def test_traverse_depth_2(store: GraphStore) -> None:
    """A two-hop walk reaches a node that is two edges away.

    Graph:  Ahmed --works_at--> AcmeCorp --uses--> PELDRUN Memory
    """
    ahmed = await store.add_entity(Entity.build("PERSON", "Ahmed"))
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    project = await store.add_entity(Entity.build("PROJECT", "Peldrun"))

    await store.add_relation(
        Relation.build(ahmed.entity_id, acme.entity_id, "works_at")
    )
    await store.add_relation(
        Relation.build(acme.entity_id, project.entity_id, "uses")
    )

    one_hop = await store.traverse(ahmed.entity_id, max_depth=1)
    assert len(one_hop) == 1
    assert {hop.distance for hop in one_hop} == {1}

    two_hops = await store.traverse(ahmed.entity_id, max_depth=2)
    assert len(two_hops) == 2
    assert sorted(hop.distance for hop in two_hops) == [1, 2]

    far = [
        hop for hop in two_hops if hop.distance == 2
    ][0]
    assert far.relation.relation_type == "uses"

    # Deterministic across runs.
    again = await store.traverse(ahmed.entity_id, max_depth=2)
    assert [h.relation.relation_id for h in again] == [
        h.relation.relation_id for h in two_hops
    ]


async def test_traverse_honours_all_three_caps(
    store: GraphStore,
) -> None:
    """Depth, per-node fan-out, and the global total are all enforced."""
    hub = await store.add_entity(Entity.build("TOPIC", "Python"))
    spokes = []
    for index in range(6):
        spoke = await store.add_entity(
            Entity.build("TOPIC", f"tool-{index}")
        )
        spokes.append(spoke)
        await store.add_relation(
            Relation.build(hub.entity_id, spoke.entity_id, "uses")
        )

    assert len(await store.traverse(hub.entity_id, max_depth=1)) == 6
    assert (
        len(
            await store.traverse(
                hub.entity_id,
                max_depth=1,
                max_relations_per_entity=3,
            )
        )
        == 3
    )
    assert (
        len(
            await store.traverse(
                hub.entity_id, max_depth=2, max_total_relations=2
            )
        )
        == 2
    )

    # Degenerate inputs return [] rather than raising.
    assert await store.traverse(hub.entity_id, max_depth=0) == []
    assert await store.traverse("ent_missing", max_depth=2) == []
    assert await store.traverse("", max_depth=2) == []


# ---------------------------------------------------------------------------
# 4 + 5. Entity extraction
# ---------------------------------------------------------------------------


async def test_entity_extractor_parses_llm_output() -> None:
    """The extractor parses, normalizes, and dedupes a realistic reply."""
    gateway = _FakeGateway(
        _FakeResponse(
            {
                "entities": [
                    {"type": "PERSON", "name": "Ahmed"},
                    {"type": "organization", "name": "AcmeCorp"},
                    {"type": "PERSON", "name": "AHMED"},
                    {"type": "PERSON", "name": "Ahmed Ali"},
                    {"type": "GPE", "name": "Cairo"},
                    {"type": "TOPIC", "name": "vector search"},
                    {"type": "DATE", "name": "December 2024"},
                    {"type": "nonsense-type", "name": "coffee"},
                ]
            }
        )
    )
    extractor = EntityExtractor(
        gateway, provider_id="lmstudio", model_id="tiny-model"
    )

    entities = await extractor.extract(
        "Ahmed works at AcmeCorp in Cairo on vector search."
    )

    names = [entity.name for entity in entities]
    assert names == [
        "Ahmed",
        "AcmeCorp",
        "Ahmed Ali",
        "Cairo",
        "vector search",
        "December 2024",
        "coffee",
    ]
    # "AHMED" folded onto "Ahmed" -- one node, not two.
    assert len(entities) == 7

    by_name = {entity.name: entity for entity in entities}
    assert by_name["AcmeCorp"].entity_type == "ORG"
    assert by_name["Cairo"].entity_type == "PLACE"
    assert by_name["coffee"].entity_type == "TOPIC"  # unknown -> TOPIC
    assert by_name["December 2024"].entity_type == "DATE"
    assert all(entity.entity_id == "" for entity in entities)

    # The gateway saw the configured model and a formatted prompt.
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert call["provider_id"] == "lmstudio"
    assert call["model_id"] == "tiny-model"
    assert "Ahmed works at AcmeCorp" in call["user_prompt"]
    assert call["timeout_seconds"] >= 1


async def test_entity_extractor_graceful_on_llm_failure() -> None:
    """Every failure mode yields ``[]`` and never raises."""
    content = "Ahmed works at AcmeCorp."

    # Transport failure.
    boom = EntityExtractor(
        _FakeGateway(error=RuntimeError("provider down")),
        provider_id="p",
        model_id="m",
    )
    assert await boom.extract(content) == []

    # Timeout (the extractor's own wait_for deadline).
    slow = EntityExtractor(
        _FakeGateway({"entities": [{"type": "PERSON", "name": "X Y"}]},
                     delay=0.5),
        provider_id="p",
        model_id="m",
        timeout_seconds=0.05,
    )
    assert await slow.extract(content) == []

    # Unparseable / wrongly shaped replies.
    for payload in (
        {"entities": "not-a-list"},
        {"unexpected": 1},
        None,
        ["not", "a", "dict"],
    ):
        extractor = EntityExtractor(
            _FakeGateway(payload), provider_id="p", model_id="m"
        )
        assert await extractor.extract(content) == []

    # Missing gateway, unresolved model, empty content.
    assert (
        await EntityExtractor(
            None, provider_id="p", model_id="m"
        ).extract(content)
        == []
    )
    assert (
        await EntityExtractor(
            _FakeGateway({"entities": [{"name": "A B"}]}),
            provider_id=None,
            model_id=None,
        ).extract(content)
        == []
    )
    assert (
        await EntityExtractor(
            _FakeGateway({"entities": [{"name": "A B"}]}),
            provider_id="p",
            model_id="m",
        ).extract("   ")
        == []
    )

    # Noise items are rejected individually, not fatally.
    noisy = EntityExtractor(
        _FakeGateway(
            {
                "entities": [
                    {"type": "PERSON", "name": ""},
                    {"type": "PERSON", "name": "   "},
                    {"type": "PERSON", "name": "X"},
                    {"type": "PERSON", "name": "**"},
                    {"type": "PERSON"},
                    "not-a-dict",
                    {"type": "PERSON", "name": "Sara"},
                ]
            }
        ),
        provider_id="p",
        model_id="m",
    )
    survivors = await noisy.extract(content)
    assert [entity.name for entity in survivors] == ["Sara"]


async def test_entity_extractor_caps_entities() -> None:
    """``max_entities`` bounds the node count per memory."""
    payload = {
        "entities": [
            {"type": "TOPIC", "name": f"concept number {index}"}
            for index in range(50)
        ]
    }
    extractor = EntityExtractor(
        _FakeGateway(payload),
        provider_id="p",
        model_id="m",
        max_entities=5,
    )
    assert len(await extractor.extract("text")) == 5


#: Ten extraction cases: (text, genuine entities, noise the model
#: also emitted). The noise models the four failure shapes actually
#: observed from instruction-tuned models: empty names, punctuation
#: fragments, single characters, and items with no ``name`` key.
_EXTRACTION_CASES = (
    (
        "Ahmed works at AcmeCorp on Peldrun Memory.",
        ["Ahmed", "AcmeCorp", "Peldrun Memory"],
        ["", "**", "X"],
    ),
    (
        "I moved to Cairo in December 2024.",
        ["Cairo", "December 2024"],
        ["", "--"],
    ),
    (
        "Sara manages the platform team at Globex.",
        ["Sara", "Globex"],
        ["the", "**", "X"],
    ),
    (
        "We use Python and SQLite for persistence.",
        ["Python", "SQLite"],
        ["", "and", "X"],
    ),
    (
        "Omar from Contoso reviewed the ADR-14 document.",
        ["Omar", "Contoso"],
        ["", "...", "X"],
    ),
    (
        "The migration to Postgres was owned by Layla.",
        ["Postgres", "Layla"],
        ["", "to", "**"],
    ),
    (
        "Berlin team: Hans, Initech, ProjectX.",
        ["Hans", "Initech", "ProjectX"],
        ["", ":", "X"],
    ),
    (
        "Tokyo office reported to Nadia about VectorSearch.",
        ["Tokyo", "Nadia", "VectorSearch"],
        ["", "-", "X"],
    ),
    (
        "Karim at Umbrella uses Redis and Kafka.",
        ["Karim", "Umbrella", "Redis", "Kafka"],
        ["", "and", "**"],
    ),
    (
        "Fatima joined Initech in March 2025 for ProjectY.",
        ["Fatima", "Initech", "March 2025", "ProjectY"],
        ["", "@", "X"],
    ),
)


async def test_entity_extraction_precision_over_ten_cases() -> None:
    """The plan's acceptance metric: entity precision >= 80%.

    What precision means HERE
    -------------------------
    The LLM is not under test -- it is scripted. The measured
    quantity is the FILTERING layer's precision: of the items this
    extractor returns, what fraction are genuine entities? Each case
    feeds the model's genuine entities PLUS the noise shapes it
    really emits (empty names, punctuation, single characters,
    name-less items), so any item that leaks through the filter
    lowers precision.

    Recall is asserted too: every genuine entity must survive, since
    a silently dropped entity is a permanent hole in the graph.
    """
    genuine_total = 0
    returned_total = 0
    true_positives = 0
    missed: List[str] = []
    leaked: List[str] = []

    for text, genuine, noise in _EXTRACTION_CASES:
        items = [
            {"type": "TOPIC", "name": name} for name in genuine
        ] + [
            {"type": "TOPIC", "name": name} for name in noise
        ]
        # One item with NO name key at all.
        items.append({"type": "TOPIC"})

        extractor = EntityExtractor(
            _FakeGateway({"entities": items}),
            provider_id="p",
            model_id="m",
        )
        returned = await extractor.extract(text)
        returned_names = {entity.canonical_name for entity in returned}
        genuine_names = {
            entity.canonical_name
            for entity in (
                Entity.build("TOPIC", name) for name in genuine
            )
        }

        genuine_total += len(genuine_names)
        returned_total += len(returned_names)
        true_positives += len(genuine_names & returned_names)
        missed.extend(
            f"{text!r}: {name}"
            for name in genuine_names - returned_names
        )
        leaked.extend(
            f"{text!r}: {name}"
            for name in returned_names - genuine_names
        )

    precision = (
        true_positives / returned_total if returned_total else 0.0
    )
    recall = (
        true_positives / genuine_total if genuine_total else 0.0
    )

    assert precision >= 0.8, (
        f"entity precision {precision:.2f} < 0.80; leaked: {leaked}"
    )
    assert recall == 1.0, f"dropped genuine entities: {missed}"
    # With this filter the result is exact.
    assert precision == 1.0, f"leaked: {leaked}"
    assert returned_total == genuine_total


# ---------------------------------------------------------------------------
# Hydration + scope isolation
# ---------------------------------------------------------------------------


async def test_get_memories_for_entity_respects_scope(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """Graph hydration must not leak memories across scopes."""
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))

    in_scope = await repo.create(
        _make_memory("I work at AcmeCorp.", scope_id="p1")
    )
    other_scope = await repo.create(
        _make_memory("AcmeCorp is a vendor.", scope_id="p2")
    )
    await store.link_memory_to_entity(in_scope.memory_id, acme.entity_id)
    await store.link_memory_to_entity(
        other_scope.memory_id, acme.entity_id
    )

    unscoped = await store.get_memories_for_entity(acme.entity_id)
    assert len(unscoped) == 2

    scoped = await store.get_memories_for_entity(
        acme.entity_id, scope_refs=["project:p1"]
    )
    assert [entry.memory_id for entry in scoped] == [in_scope.memory_id]

    # An EMPTY scope list means "match nothing", never "no filter".
    assert (
        await store.get_memories_for_entity(
            acme.entity_id, scope_refs=[]
        )
        == []
    )
    # A malformed ref degrades to no results instead of raising.
    assert (
        await store.get_memories_for_entity(
            acme.entity_id, scope_refs=["not-a-scope-ref"]
        )
        == []
    )


async def test_archived_memories_are_not_hydrated(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """Only ``status='active'`` memories come back through the graph."""
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    entry = await repo.create(_make_memory("I work at AcmeCorp."))
    await store.link_memory_to_entity(entry.memory_id, acme.entity_id)

    await repo.delete(entry.memory_id, "project:p1")

    assert (
        await store.get_memories_for_entity(acme.entity_id) == []
    )


async def test_prune_orphan_links(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """Links whose memory was purged are reclaimable."""
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))
    kept = await repo.create(_make_memory("AcmeCorp one."))
    purged = await repo.create(
        _make_memory("AcmeCorp two.", scope_id="p2")
    )
    await store.link_memory_to_entity(kept.memory_id, acme.entity_id)
    await store.link_memory_to_entity(purged.memory_id, acme.entity_id)
    assert await store.count_links() == 2

    await repo.purge(purged.memory_id, "project:p2")
    assert await store.count_links() == 2  # orphan still present
    assert await store.prune_orphan_links() == 1
    assert await store.count_links() == 1


# ---------------------------------------------------------------------------
# GraphIndexer
# ---------------------------------------------------------------------------


async def test_indexer_writes_entities_links_and_cooccurrence(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """One memory becomes entities, links, and bounded pairs."""
    gateway = _FakeGateway(
        {
            "entities": [
                {"type": "PERSON", "name": "Ahmed"},
                {"type": "ORG", "name": "AcmeCorp"},
                {"type": "PROJECT", "name": "Peldrun"},
                {"type": "TOPIC", "name": "Python"},
            ]
        }
    )
    extractor = EntityExtractor(
        gateway, provider_id="p", model_id="m"
    )
    indexer = GraphIndexer(store=store, extractor=extractor)

    entry = await repo.create(
        _make_memory("Ahmed at AcmeCorp builds Peldrun in Python.")
    )
    count = await indexer.index_memory(entry)

    assert count == 4
    assert await store.count_entities() == 4
    assert await store.count_links() == 4
    # 4 entities -> 6 unordered pairs.
    assert await store.count_relations() == 6

    linked = await store.get_entities_for_memory(entry.memory_id)
    assert {entity.canonical_name for entity in linked} == {
        "ahmed",
        "acmecorp",
        "peldrun",
        "python",
    }

    memories = await store.get_memories_for_entity(
        linked[0].entity_id, scope_refs=["project:p1"]
    )
    assert [m.memory_id for m in memories] == [entry.memory_id]


async def test_indexer_is_idempotent(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """Indexing the same memory twice changes nothing."""
    gateway = _FakeGateway(
        {
            "entities": [
                {"type": "PERSON", "name": "Ahmed"},
                {"type": "ORG", "name": "AcmeCorp"},
            ]
        }
    )
    indexer = GraphIndexer(
        store=store,
        extractor=EntityExtractor(
            gateway, provider_id="p", model_id="m"
        ),
    )
    entry = await repo.create(_make_memory("Ahmed works at AcmeCorp."))

    await indexer.index_memory(entry)
    first = (
        await store.count_entities(),
        await store.count_relations(),
        await store.count_links(),
    )
    await indexer.index_memory(entry)
    second = (
        await store.count_entities(),
        await store.count_relations(),
        await store.count_links(),
    )
    assert first == second == (2, 1, 2)


async def test_indexer_cooccurrence_pairs_are_canonical(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """The same entity pair yields ONE edge regardless of mention order."""
    entities = [
        {"type": "PERSON", "name": "Ahmed"},
        {"type": "ORG", "name": "AcmeCorp"},
    ]
    first_entry = await repo.create(
        _make_memory("Ahmed works at AcmeCorp.", scope_id="p1")
    )
    second_entry = await repo.create(
        _make_memory("AcmeCorp employs Ahmed.", scope_id="p2")
    )

    for payload_entry, names in (
        (first_entry, entities),
        (second_entry, list(reversed(entities))),
    ):
        indexer = GraphIndexer(
            store=store,
            extractor=EntityExtractor(
                _FakeGateway({"entities": names}),
                provider_id="p",
                model_id="m",
            ),
        )
        await indexer.index_memory(payload_entry)

    # Two memories, but ONE undirected related_to edge.
    assert await store.count_relations() == 1


async def test_indexer_never_raises_on_extraction_failure(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """A failing extractor returns 0 and preserves existing links."""
    entry = await repo.create(_make_memory("Ahmed works at AcmeCorp."))

    good = GraphIndexer(
        store=store,
        extractor=EntityExtractor(
            _FakeGateway(
                {"entities": [{"type": "PERSON", "name": "Ahmed"}]}
            ),
            provider_id="p",
            model_id="m",
        ),
    )
    assert await good.index_memory(entry) == 1
    assert await store.count_links() == 1

    broken = GraphIndexer(
        store=store,
        extractor=EntityExtractor(
            _FakeGateway(error=RuntimeError("down")),
            provider_id="p",
            model_id="m",
        ),
    )
    assert await broken.index_memory(entry) == 0
    # The pre-existing link SURVIVES a failed re-index.
    assert await store.count_links() == 1


async def test_indexer_disabled_and_unconfigured_are_noops(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """The kill switch and an unresolved model both short-circuit."""
    entry = await repo.create(_make_memory("Ahmed works at AcmeCorp."))
    gateway = _FakeGateway(
        {"entities": [{"type": "PERSON", "name": "Ahmed"}]}
    )

    disabled = GraphIndexer(
        store=store,
        extractor=EntityExtractor(
            gateway, provider_id="p", model_id="m"
        ),
        enabled=False,
    )
    assert await disabled.index_memory(entry) == 0
    assert gateway.calls == []
    assert disabled.is_enabled is False

    unconfigured = GraphIndexer(
        store=store,
        extractor=EntityExtractor(
            gateway, provider_id=None, model_id=None
        ),
    )
    assert await unconfigured.index_memory(entry) == 0
    assert gateway.calls == []
    assert unconfigured.is_active is False

    assert await store.count_entities() == 0

    # Unpersisted entries are refused.
    assert await disabled.index_memory(_make_memory("x")) == 0


async def test_reindex_memory_clears_stale_links(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """``reindex_memory`` replaces links instead of accumulating them."""
    entry = await repo.create(_make_memory("Ahmed works at AcmeCorp."))
    indexer = GraphIndexer(
        store=store,
        extractor=EntityExtractor(
            _FakeGateway(
                {
                    "entities": [
                        {"type": "PERSON", "name": "Ahmed"},
                        {"type": "ORG", "name": "AcmeCorp"},
                    ]
                }
            ),
            provider_id="p",
            model_id="m",
        ),
    )
    await indexer.index_memory(entry)
    assert await store.count_links() == 2

    # Content no longer mentions AcmeCorp.
    narrower = GraphIndexer(
        store=store,
        extractor=EntityExtractor(
            _FakeGateway(
                {"entities": [{"type": "PERSON", "name": "Ahmed"}]}
            ),
            provider_id="p",
            model_id="m",
        ),
    )
    await narrower.reindex_memory(entry)

    linked = await store.get_entities_for_memory(entry.memory_id)
    assert [entity.canonical_name for entity in linked] == ["ahmed"]


def test_canonical_pairs_is_bounded_and_stable() -> None:
    """The pair helper is capped and order-independent."""
    entities = [
        Entity(entity_id=f"ent_{index:02d}", name=f"node {index}",
               entity_type="TOPIC")
        for index in range(5)
    ]

    pairs = canonical_pairs(entities, 100)
    assert len(pairs) == 10  # C(5,2)
    assert [pair[0].entity_id for pair in pairs] == sorted(
        [pair[0].entity_id for pair in pairs]
    )

    capped = canonical_pairs(entities, 3)
    assert len(capped) == 3
    assert capped == canonical_pairs(entities, 3)

    assert canonical_pairs(entities, 0) == []
    assert canonical_pairs(entities[:1], 10) == []


# ---------------------------------------------------------------------------
# MemoryService integration
# ---------------------------------------------------------------------------


class _ServiceTransport:
    """A gateway transport that answers entity-extraction prompts.

    ``MemoryModelGateway`` accepts a ``transport=`` callable with
    exactly this signature, so this double exercises the REAL
    gateway -> extractor -> indexer path rather than stubbing at a
    higher level.
    """

    def __init__(self, entities: List[Dict[str, str]]) -> None:
        self.entities = entities
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> Any:
        from omweb.memory.extraction.gateway import (
            LLMStructuredResponse,
        )

        self.calls += 1
        return LLMStructuredResponse(
            parsed={"entities": list(self.entities)},
            raw_text="",
            cost_nano_usd=0,
            input_tokens=0,
            output_tokens=0,
        )


async def _prepare_service(tmp_path: Path, **kwargs: Any):
    """Async helper: write the settings file, then initialize."""
    from omweb.memory.manager.memory_settings_manager import (
        MemorySettingsManager,
    )

    db_path = tmp_path / "service.db"
    settings_path = tmp_path / "memory_settings.json"

    base = default_settings()
    ai = base.ai.model_copy(
        update={
            "enabled": True,
            "extraction_enabled": False,
            "graph_indexing_enabled": bool(
                kwargs.get("graph_enabled", False)
            ),
            "agentic": base.ai.agentic.model_copy(
                update={
                    "provider_id": "mock-provider",
                    "model_id": "mock-model",
                    "timeout_seconds": 10,
                }
            ),
        }
    )
    settings = base.model_copy(
        update={
            "mode": MemoryMode(kwargs.get("mode", "read_write")),
            "ai": ai,
        }
    )
    manager = MemorySettingsManager(path=settings_path)
    await manager.save(settings)

    from omweb.memory.manager.memory_service import MemoryService

    transport = kwargs.get("transport")
    service = MemoryService(
        db_path=db_path,
        settings_path=settings_path,
        transport_provider=(
            (lambda: transport) if transport is not None else None
        ),
    )
    await service.ainitialize()
    return service


async def test_service_graph_disabled_by_default(
    tmp_path: Path,
) -> None:
    """With the flag off, nothing graph-related is constructed."""
    service = await _prepare_service(
        tmp_path, graph_enabled=False, transport=_ServiceTransport([])
    )
    try:
        assert service.graph_enabled is False
        assert service.graph_store is None
        assert service.graph_indexer is None
    finally:
        await service.aclose()


async def test_service_enables_graph_and_indexes_on_write(
    tmp_path: Path,
) -> None:
    """A write through the pipeline becomes graph structure."""
    from omweb.memory.manager.write_pipeline import (
        MemoryWritePipeline,
        drain_graph_tasks,
    )

    transport = _ServiceTransport(
        [
            {"type": "PERSON", "name": "Ahmed"},
            {"type": "ORG", "name": "AcmeCorp"},
        ]
    )
    service = await _prepare_service(
        tmp_path, graph_enabled=True, transport=transport
    )
    try:
        assert service.graph_enabled is True
        assert service.graph_store is not None
        assert service.graph_indexer is not None
        assert service.graph_indexer.is_active is True

        pipeline = MemoryWritePipeline(
            port=service.provider,
            settings=service.settings,
            graph_indexer=service.graph_indexer,
        )
        outcomes = await pipeline.save_many(
            [_make_memory("Ahmed works at AcmeCorp.")],
            scope_ref="project:p1",
        )
        assert outcomes[0].succeeded is True

        # The write returned BEFORE indexing finished; drain and
        # then assert on the resulting graph.
        await drain_graph_tasks(10.0)

        store = service.graph_store
        assert await store.count_entities() == 2
        assert await store.count_links() == 2
        assert await store.count_relations() == 1
        assert transport.calls == 1
    finally:
        await service.aclose()
        # aclose must have torn the pipeline down.
        assert service.graph_enabled is False


async def test_service_backfill_covers_direct_provider_writes(
    tmp_path: Path,
) -> None:
    """``memory_save``-style writes are picked up by the backfill.

    The ``memory_save`` tool writes straight through the provider
    and never touches ``MemoryWritePipeline``, so it cannot schedule
    indexing. The periodic backfill is what closes that gap.
    """
    transport = _ServiceTransport(
        [{"type": "TOPIC", "name": "vector search"}]
    )
    service = await _prepare_service(
        tmp_path, graph_enabled=True, transport=transport
    )
    try:
        entry = await service.provider.create(
            _make_memory("I like vector search a lot.")
        )
        store = service.graph_store
        assert store is not None
        # Nothing indexed yet.
        assert await store.count_entities() == 0

        indexed = await service.run_graph_backfill_once()
        assert indexed == 1
        assert await store.count_entities() == 1
        assert await store.count_links() == 1

        # A second tick finds nothing left to do.
        assert await service.run_graph_backfill_once() == 0
        assert await store.count_links() == 1

        # The backfilled memory is reachable through the graph.
        entity = await store.find_entity("vector search")
        assert entity is not None
        memories = await store.get_memories_for_entity(
            entity.entity_id, scope_refs=["project:p1"]
        )
        assert [m.memory_id for m in memories] == [entry.memory_id]
    finally:
        await service.aclose()


async def test_service_retrieve_unaffected_by_graph(
    tmp_path: Path,
) -> None:
    """``retrieve`` stays on the Phase 4/5 path and stays fast."""
    transport = _ServiceTransport(
        [{"type": "PERSON", "name": "Ahmed"}]
    )
    service = await _prepare_service(
        tmp_path, graph_enabled=True, transport=transport
    )
    try:
        await service.provider.create(
            _make_memory("Ahmed likes vector search.")
        )
        started = time.monotonic()
        context = await service.retrieve(
            query="vector search",
            chat_id=None,
            project_id="p1",
        )
        elapsed = time.monotonic() - started

        assert context is not None
        assert context.query == "vector search"
        # Graph must not insert an LLM call into the hot path: the
        # transport is only ever used by the extractor, and no
        # indexing has been triggered by this read.
        assert transport.calls == 0
        # Generous CI-safe bound (the plan's target is ~500ms).
        assert elapsed < 2.0
    finally:
        await service.aclose()


async def test_service_aclose_cancels_backfill(
    tmp_path: Path,
) -> None:
    """Shutdown leaves no pending graph tasks behind."""
    from omweb.memory.manager.write_pipeline import (
        pending_graph_task_count,
    )

    service = await _prepare_service(
        tmp_path,
        graph_enabled=True,
        transport=_ServiceTransport([]),
    )
    backfill = service._graph_backfill_task
    assert backfill is not None
    assert not backfill.done()

    await service.aclose()

    assert service._graph_backfill_task is None
    assert backfill.done()
    assert pending_graph_task_count() == 0
    assert service.graph_enabled is False


async def test_service_graph_requires_writable_mode(
    tmp_path: Path,
) -> None:
    """READ_ONLY mode does not start graph indexing."""
    service = await _prepare_service(
        tmp_path,
        graph_enabled=True,
        mode="read_only",
        transport=_ServiceTransport([]),
    )
    try:
        assert service.graph_enabled is False
    finally:
        await service.aclose()



async def test_llm_calls_are_serialized_process_wide(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """Concurrent indexing must not open parallel CHAT completions.

    The write pipeline dispatches ONE background task per written
    memory, so a batch write of N memories would otherwise fire N
    simultaneous ``/chat/completions`` calls. A local runtime (LM
    Studio, Ollama) serves one request at a time and INTERLEAVES
    overlapping generations, so both responses become unparseable.

    The gate is process-wide and lives in
    ``peldrun.memory.llm_contracts`` so it also covers agentic calls.
    ``configure_llm_concurrency`` is the single knob.
    """
    from peldrun.memory.llm_contracts import (
        configure_llm_concurrency,
        current_llm_concurrency,
    )

    entries = [
        await repo.create(_make_memory(f"note number {index}."))
        for index in range(6)
    ]

    gate = _ConcurrencyGauge()
    extractor = EntityExtractor(
        gate, provider_id="p", model_id="m"
    )
    indexer = GraphIndexer(store=store, extractor=extractor)

    # ---- default: one at a time --------------------------------
    configure_llm_concurrency(1)
    results = await asyncio.gather(
        *[indexer.index_memory(entry) for entry in entries]
    )
    assert results == [1] * 6
    assert gate.peak == 1, (
        f"extractor saw {gate.peak} concurrent calls at "
        f"concurrency=1"
    )

    # ---- widened for a hosted provider -------------------------
    configure_llm_concurrency(3)
    assert current_llm_concurrency() == 3
    gate.reset()
    await asyncio.gather(
        *[indexer.index_memory(entry) for entry in entries]
    )
    assert 1 < gate.peak <= 3

    # Restore the process-wide default so later tests are unaffected.
    configure_llm_concurrency(1)


class _ConcurrencyGauge:
    """Gateway that measures how many calls are in flight at once."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.now = 0
        self.peak = 0

    async def generate_structured(self, **kwargs: Any) -> Any:
        self.now += 1
        self.peak = max(self.peak, self.now)
        try:
            await asyncio.sleep(0.02)
        finally:
            self.now -= 1
        return _FakeResponse(
            {"entities": [{"type": "TOPIC", "name": "gauge topic"}]}
        )


# ---------------------------------------------------------------------------
# Zero-coupling check
# ---------------------------------------------------------------------------


def test_peldrun_graph_package_imports_no_omweb() -> None:
    """The core graph package must stay free of ``omweb`` imports.

    This is the architectural boundary the whole split depends on:
    if ``peldrun.memory.graph`` ever imports ``omweb``, the layering
    is broken and a future storage backend swap becomes impossible.
    Checked by parsing the SOURCE rather than ``sys.modules`` (which
    is polluted by other test modules in the same session).
    """
    import ast

    package_dir = (
        Path(__file__).resolve().parents[2]
        / "peldrun"
        / "memory"
        / "graph"
    )
    assert package_dir.is_dir()

    for source_file in package_dir.glob("*.py"):
        tree = ast.parse(
            source_file.read_text(encoding="utf-8"),
            filename=str(source_file),
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("omweb"), (
                        f"{source_file.name} imports {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("omweb"), (
                    f"{source_file.name} imports from {module}"
                )
