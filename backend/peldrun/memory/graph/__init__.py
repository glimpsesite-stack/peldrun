# backend/peldrun/memory/graph/__init__.py
"""
PELDRUN Memory Platform - Knowledge Graph Core Package.

Public import surface for the graph layer that lives inside the
``peldrun`` CORE. Consumers outside this package import from here,
never from the individual modules:

    CORRECT:
        from peldrun.memory.graph import Entity, Relation

    WRONG (couples the consumer to the internal file layout):
        from peldrun.memory.graph.contracts import Entity

Layering
--------
``peldrun`` owns the CONTRACTS and the ALGORITHMS:

  * ``contracts``         -- ``Entity``, ``Relation``,
                             ``RelationHop``, normalization helpers.
  * ``entity_extractor``  -- LLM-backed named-entity extraction.

``omweb`` owns PERSISTENCE and WIRING:

  * ``omweb.memory.storage.graph_store`` -- SQLite implementation.
  * ``omweb.memory.graph.indexer``       -- write-time indexing.

This package imports NOTHING from ``omweb``. That is the entire
point of the split: a storage backend can be replaced (or faked in a
test) without the core observing it.

Why ``graph_search`` is absent here
-----------------------------------
Sub-phase 6B adds ``peldrun.memory.graph.graph_search`` (the
depth-N traversal policy). It is deliberately NOT exported yet:
importing this package must never pull in retrieval machinery, which
keeps ``MemoryService`` startup cheap and keeps the 6A deliverable
free of half-wired surface area. 6B adds it to this ``__all__``.
"""

from __future__ import annotations

from peldrun.memory.graph.contracts import (
    ENTITY_TYPES,
    RELATION_TYPES,
    Entity,
    Relation,
    RelationHop,
    canonicalize_name,
    make_entity_id,
    make_relation_id,
    normalize_entity_type,
    normalize_relation_type,
)
from peldrun.memory.graph.entity_extractor import EntityExtractor
from peldrun.memory.graph.graph_search import (
    GraphSearch,
    GraphStoreProtocol,
)


__all__ = [
    "ENTITY_TYPES",
    "RELATION_TYPES",
    "Entity",
    "EntityExtractor",
    "GraphSearch",
    "GraphStoreProtocol",
    "Relation",
    "RelationHop",
    "canonicalize_name",
    "make_entity_id",
    "make_relation_id",
    "normalize_entity_type",
    "normalize_relation_type",
]
