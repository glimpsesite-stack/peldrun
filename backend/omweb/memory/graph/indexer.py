# backend/omweb/memory/graph/indexer.py
"""
PELDRUN Memory Platform - Write-Time Knowledge Graph Indexer.

Turns a persisted ``MemoryEntry`` into graph structure: entities,
memory-to-entity links, and co-occurrence relations between the
entities the same memory mentions.

Why it lives in ``omweb``
-------------------------
The indexer is pure ORCHESTRATION: it owns no algorithm of its own,
it just sequences the extractor (``peldrun``) and the store
(``omweb.storage``). Orchestration belongs on the ``omweb`` side,
where the concrete components are wired together. The core stays
free of storage concerns.

Design invariants
-----------------

1. **Never blocks the write path.**
   ``MemoryWritePipeline.save_many`` dispatches
   ``asyncio.create_task(indexer.index_memory(entry))`` and does NOT
   await it. Memory persistence latency must not include an LLM
   round-trip. This module is therefore written to be safe as a
   FIRE-AND-FORGET task:

     * it never raises (``CancelledError`` excepted), because an
       unretrieved task exception is logged by asyncio as noise the
       operator cannot act on,
     * it holds no shared mutable state, so concurrent tasks for
       different memories cannot interfere,
     * it is idempotent, so a duplicate task (a retry, a re-index, a
       double dispatch) converges on the same graph.

2. **Extraction failure must not destroy existing graph data.**
   Links for the memory are REPLACED only when the extractor
   actually returned entities. If extraction fails or legitimately
   finds nothing, the previous links survive. Rationale: the graph
   is a derived index, and the worst outcome is losing a correct
   index because a transient LLM timeout coincided with a re-index.
   The cost of the safer policy is that a memory whose content was
   edited to remove an entity keeps a stale link until the next
   successful extraction -- a quality nit, not a data-loss event.

3. **Co-occurrence edges are single, undirected-in-practice.**
   Two entities mentioned in the same memory get ONE
   ``related_to`` edge. The direction is chosen by sorting the pair
   on ``entity_id``, so the edge is identical no matter which order
   the extractor listed them in, or which memory observed them
   first. Without that canonicalisation, two memories mentioning
   the same pair in opposite orders would create two parallel
   edges, doubling the degree of both nodes and inflating every
   later traversal score.

4. **Bounded fan-out.**
   Co-occurrence pairs grow as ``n(n-1)/2``: a memory with 30
   entities would mint 435 edges. ``max_cooccurrence_pairs`` caps
   that, and ``EntityExtractor.max_entities`` caps ``n`` upstream.
   A graph that grows quadratically per memory turns every later
   traversal into a full scan.

5. **One bad entity does not abort the memory.**
   Each entity is written inside its own ``try/except``. A single
   integrity or encoding failure loses that entity, not the other
   nineteen.

See also
--------

    * ``peldrun/memory/graph/entity_extractor.py`` -- produces the
      entities
    * ``omweb/memory/storage/graph_store.py``      -- persists them
    * ``omweb/memory/manager/write_pipeline.py``   -- dispatches the
      background task
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Tuple

from peldrun.memory.domain import MemoryEntry
from peldrun.memory.graph.contracts import Entity, Relation
from peldrun.memory.graph.entity_extractor import EntityExtractor

from omweb.memory.storage.graph_store import GraphStore


logger = logging.getLogger("omweb.memory.graph.indexer")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_MAX_COOCCURRENCE_PAIRS: int = 20
"""Cap on ``related_to`` edges minted for ONE memory.

``n(n-1)/2`` pairs: 7 entities -> 21 pairs, so the default is
reached at 7 entities. Beyond that the memory is almost certainly a
dense document where every pair is "related" in a sense too weak to
be worth storing -- the strongest signal was already captured by the
first pairs in canonical order.
"""


DEFAULT_COOCCURRENCE_CONFIDENCE: float = 0.55
"""Confidence assigned to a co-occurrence edge.

Deliberately below the 0.8 default of an explicitly typed relation.
``traverse`` orders neighbours by ``confidence DESC``, so a real
``works_at`` edge always outranks a mere "these two appeared
together" edge when the per-node cap forces a choice. 0.55 also sits
above the 0.5 midpoint so a co-occurrence still beats a
confidence-0 edge from a failing extractor.
"""


_COOCCURRENCE_RELATION_TYPE: str = "related_to"
"""The relation type used for co-occurrence edges.

Reusing the designated fallback type keeps the vocabulary closed:
``related_to`` already means "an edge exists but is not more
specifically typed", which is exactly what co-occurrence asserts.
"""


DEFAULT_MAX_CONCURRENCY: int = 1
"""Deprecated placeholder kept for import compatibility.

The real concurrency gate lives in ``peldrun.memory.llm_contracts``
(``configure_llm_concurrency``), because it must cover indexing AND
agentic calls together -- both hit the same single-slot local runtime.
A per-indexer gate could not see the agentic calls, so it was removed
rather than left as a second, misleading knob.
"""

# ---------------------------------------------------------------------------
# GraphIndexer
# ---------------------------------------------------------------------------


class GraphIndexer:
    """Indexes memories into the knowledge graph.

    Stateless. Construct once per process and reuse.

    Example
    -------
    ::

        indexer = GraphIndexer(store=graph_store, extractor=extractor)
        # In the write pipeline (fire-and-forget):
        asyncio.create_task(indexer.index_memory(entry))
    """

    def __init__(
        self,
        store: GraphStore,
        extractor: EntityExtractor,
        *,
        enabled: bool = True,
        max_cooccurrence_pairs: int = DEFAULT_MAX_COOCCURRENCE_PAIRS,
        cooccurrence_confidence: float = (
            DEFAULT_COOCCURRENCE_CONFIDENCE
        ),
    ) -> None:
        """Construct the indexer.

        Parameters
        ----------
        store
            The graph store. Required -- an indexer without a store
            is a wiring bug that should fail at construction.
        extractor
            The entity extractor. Required for the same reason, but
            note that an extractor with ``is_configured == False``
            makes ``index_memory`` a cheap no-op, which is how the
            pipeline degrades when no model is selected.
        enabled
            Master switch. ``False`` makes ``index_memory`` return
            ``0`` immediately. Used by ``MemoryService`` when the
            operator disables graph indexing without tearing down
            the rest of the pipeline.
        max_cooccurrence_pairs
            Pair cap per memory. Non-positive means "no
            co-occurrence edges" (only memory links are written),
            which is a legitimate configuration for a deployment
            that only wants entity lookup, not traversal.
        cooccurrence_confidence
            Confidence stamped on co-occurrence edges.

        Raises
        ------
        TypeError
            When ``store`` or ``extractor`` is ``None``.
        """
        if store is None:
            raise TypeError(
                "GraphIndexer requires a GraphStore; got None."
            )
        if extractor is None:
            raise TypeError(
                "GraphIndexer requires an EntityExtractor; got None."
            )
        self._store = store
        self._extractor = extractor
        self._enabled = bool(enabled)
        self._max_pairs = max(0, int(max_cooccurrence_pairs))
        try:
            confidence = float(cooccurrence_confidence)
        except (TypeError, ValueError):
            confidence = DEFAULT_COOCCURRENCE_CONFIDENCE
        if confidence != confidence:  # NaN
            confidence = DEFAULT_COOCCURRENCE_CONFIDENCE
        self._cooccurrence_confidence = min(
            1.0, max(0.0, confidence)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def index_memory(self, memory: MemoryEntry) -> int:
        """Index ``memory`` into the graph.

        Parameters
        ----------
        memory
            A PERSISTED entry (``memory_id`` assigned). An entry
            that is not persisted cannot be linked and is rejected
            with a WARNING and a ``0`` return -- links pointing at a
            memory id that never reaches the database are pure
            garbage that ``prune_orphan_links`` would later delete
            anyway.

        Returns
        -------
        int
            The number of entities indexed for this memory.

            **Deviation from the §7.5 sketch:** the sketch says
            "count of new entities". Reporting exactly-new entities
            would require a lookup per entity before the upsert,
            doubling the query count for a value nothing consumes --
            and the upsert is deterministic, so "new vs existing" is
            not observable to any caller. The count returned is the
            number of entities this memory now contributes, which is
            what a caller actually wants to log.

        Never raises (``CancelledError`` excepted). Every failure is
        logged at WARNING and mapped to the number of entities
        already written, so a partial index is still reported
        honestly.
        """
        if not self._enabled:
            return 0
        if memory is None:
            return 0
        memory_id = getattr(memory, "memory_id", None)
        if not isinstance(memory_id, str) or not memory_id:
            logger.warning(
                "GraphIndexer: refusing to index an unpersisted "
                "memory (memory_id=%r).", memory_id,
            )
            return 0

        if not self._extractor.is_configured:
            logger.debug(
                "GraphIndexer: extractor is not configured; "
                "skipping memory %s.", memory_id,
            )
            return 0

        started = time.monotonic()
        try:
            # Serialized process-wide by ``call_structured``'s own gate
            # (``peldrun.memory.llm_contracts``). The indexer does NOT
            # carry a second gate: two layers would imply two knobs, and
            # the real limit covers indexing AND agentic calls together
            # because they share one local runtime.
            entities = await self._extractor.extract(
                getattr(memory, "content", "") or ""
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.warning(
                "GraphIndexer: extraction raised for memory %s "
                "(%s: %s); graph untouched.",
                memory_id, type(exc).__name__, exc,
            )
            return 0

        if not entities:
            # Nothing extracted. Do NOT clear existing links -- see
            # design invariant #2.
            logger.debug(
                "GraphIndexer: no entities found in memory %s; "
                "existing links preserved.", memory_id,
            )
            return 0

        persisted = await self._write_entities(memory_id, entities)
        if persisted:
            await self._write_cooccurrence(memory_id, persisted)

        logger.debug(
            "GraphIndexer: memory %s -> %d entity/entities in %.0f ms.",
            memory_id,
            len(persisted),
            (time.monotonic() - started) * 1000.0,
        )
        return len(persisted)

    async def reindex_memory(self, memory: MemoryEntry) -> int:
        """Drop this memory's links, then index it again.

        Use this when a memory's CONTENT changed: ``index_memory``
        only ADDS links (invariant #2), so an entity that vanished
        from the text would keep its stale link forever.

        Clearing happens FIRST, so a failing extraction leaves the
        memory with no links rather than a mix of stale and fresh
        ones -- a wrong-but-complete-empty state is easier to reason
        about and to repair by re-running the backfill.

        Returns the number of entities indexed (0 when extraction
        produced nothing).
        """
        if not self._enabled or memory is None:
            return 0
        memory_id = getattr(memory, "memory_id", None)
        if not isinstance(memory_id, str) or not memory_id:
            return 0
        try:
            await self._store.delete_memory_links(memory_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.warning(
                "GraphIndexer: could not clear links for memory %s "
                "(%s: %s); aborting re-index.",
                memory_id, type(exc).__name__, exc,
            )
            return 0
        return await self.index_memory(memory)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _write_entities(
        self,
        memory_id: str,
        entities: List[Entity],
    ) -> List[Entity]:
        """Upsert entities and link them to the memory.

        Returns the PERSISTED entities (ids assigned), in the
        extractor's original order, with duplicates already removed
        by the extractor. An entity that fails is omitted from the
        result so the caller does not build co-occurrence edges
        against an id that was never written.
        """
        persisted: List[Entity] = []
        for entity in entities:
            try:
                stored = await self._store.add_entity(entity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- per entity
                logger.warning(
                    "GraphIndexer: could not persist entity %r for "
                    "memory %s (%s: %s); skipping.",
                    getattr(entity, "name", None),
                    memory_id,
                    type(exc).__name__,
                    exc,
                )
                continue

            try:
                await self._store.link_memory_to_entity(
                    memory_id, stored.entity_id
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- per entity
                logger.warning(
                    "GraphIndexer: could not link entity %s to "
                    "memory %s (%s: %s); skipping.",
                    stored.entity_id,
                    memory_id,
                    type(exc).__name__,
                    exc,
                )
                continue

            persisted.append(stored)
        return persisted

    async def _write_cooccurrence(
        self,
        memory_id: str,
        entities: List[Entity],
    ) -> None:
        """Mint ``related_to`` edges between entity pairs.

        Pair generation
        ---------------
        All unordered pairs are enumerated in a canonical order --
        sorted by ``entity_id`` -- and then truncated to
        ``max_cooccurrence_pairs``. Canonical ordering is what makes
        the SAME pair from two different memories produce the SAME
        edge (see design invariant #3); truncation takes the
        lexicographically smallest id pairs, so two index runs over
        the same entity set mint the same subset.

        Failures are logged per edge and never abort the memory: a
        missing relation is a weaker index, not a broken one.
        """
        if self._max_pairs <= 0 or len(entities) < 2:
            return

        for first, second in canonical_pairs(
            entities, self._max_pairs
        ):
            try:
                relation = Relation.build(
                    first.entity_id,
                    second.entity_id,
                    _COOCCURRENCE_RELATION_TYPE,
                    source_memory_id=memory_id,
                    confidence=self._cooccurrence_confidence,
                )
            except ValueError:
                # Self-loop guard. ``canonical_pairs`` already drops
                # them; kept as a hard stop in case the contract
                # tightens later.
                continue
            try:
                await self._store.add_relation(relation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- per edge
                logger.debug(
                    "GraphIndexer: co-occurrence edge %s->%s "
                    "failed for memory %s (%s: %s).",
                    first.entity_id,
                    second.entity_id,
                    memory_id,
                    type(exc).__name__,
                    exc,
                )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        """True when this indexer will do any work."""
        return self._enabled

    @property
    def is_active(self) -> bool:
        """True when indexing will actually reach the LLM.

        Requires BOTH the master switch and a configured extractor.
        ``MemoryService`` logs this at startup so an operator can
        tell "graph enabled but no model selected" from "graph
        working".
        """
        return self._enabled and self._extractor.is_configured

    @property
    def store(self) -> GraphStore:
        """The graph store (for diagnostics and maintenance)."""
        return self._store

    @property
    def extractor(self) -> EntityExtractor:
        """The entity extractor (for diagnostics)."""
        return self._extractor

    def __repr__(self) -> str:
        return (
            f"GraphIndexer(enabled={self._enabled}, "
            f"active={self.is_active}, "
            f"max_pairs={self._max_pairs}, "
            f"extractor={self._extractor!r})"
        )


# ---------------------------------------------------------------------------
# Pair helper (exposed for tests / future strategies)
# ---------------------------------------------------------------------------


def canonical_pairs(
    entities: List[Entity],
    limit: int,
) -> List[Tuple[Entity, Entity]]:
    """Return up to ``limit`` canonical unordered pairs of entities.

    Exposed as a module function so the pairing policy can be tested
    and reused (e.g. by a future LLM-based relation extractor that
    wants to re-rank the same candidate pairs) without constructing
    an indexer.

    Canonical order means: entities sorted by ``entity_id``, then
    pairs emitted by outer/inner index. For a fixed entity set the
    output is identical across runs and across processes.
    """
    if limit <= 0 or len(entities) < 2:
        return []
    ordered = sorted(entities, key=lambda e: e.entity_id)
    pairs: List[Tuple[Entity, Entity]] = []
    for index, first in enumerate(ordered):
        if len(pairs) >= limit:
            break
        for second in ordered[index + 1:]:
            if len(pairs) >= limit:
                break
            if first.entity_id == second.entity_id:
                continue
            pairs.append((first, second))
    return pairs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_COOCCURRENCE_CONFIDENCE",
    "DEFAULT_MAX_CONCURRENCY",
    "DEFAULT_MAX_COOCCURRENCE_PAIRS",
    "GraphIndexer",
    "canonical_pairs",
]
