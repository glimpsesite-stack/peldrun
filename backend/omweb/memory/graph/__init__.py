# backend/omweb/memory/graph/__init__.py
"""
PELDRUN Memory Platform - Knowledge Graph Wiring Layer.

The ``omweb`` side of the Phase 6 split. This package contains the
ORCHESTRATION that sequences the ``peldrun`` core algorithms against
the ``omweb`` storage layer:

    * ``indexer`` -- ``GraphIndexer``: write-time memory -> graph
                     indexing (background, idempotent, never raising).

The ``peldrun`` core owns the contracts and the algorithms
(``peldrun.memory.graph``); this package owns the wiring. Import
direction is one-way: ``omweb`` imports ``peldrun``, never the
reverse.
"""

from __future__ import annotations

from omweb.memory.graph.indexer import (
    DEFAULT_COOCCURRENCE_CONFIDENCE,
    DEFAULT_MAX_COOCCURRENCE_PAIRS,
    GraphIndexer,
    canonical_pairs,
)


__all__ = [
    "DEFAULT_COOCCURRENCE_CONFIDENCE",
    "DEFAULT_MAX_COOCCURRENCE_PAIRS",
    "GraphIndexer",
    "canonical_pairs",
]
