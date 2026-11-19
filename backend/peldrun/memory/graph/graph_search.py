# backend/peldrun/memory/graph/graph_search.py
"""
PELDRUN Memory Platform - Knowledge Graph Search.

Turns a user QUERY into ranked memories by traversing the knowledge
graph: resolve the entities the query mentions, then collect the
memories attached to those entities (distance 0) and to their
neighbours (distance 1..N).

This is what lets retrieval answer "what did my manager say about the
project?" from memories that are not textually similar to the
question: the query resolves to the entity "Ahmed", his ``works_at``
edge to "AcmeCorp", and the memories linked to those nodes.

Design invariants
-----------------

1. **No LLM on the search path.**
   The §7.5 sketch resolves query entities with an LLM call. That is
   explicitly NOT what this class does, and the reason is measured:
   ``HybridMemoryProvider.search_ranked`` is invoked by
   ``MemoryService.retrieve()`` on EVERY dispatcher turn. Adding one
   LLM round-trip there would put a 300-2000 ms call in front of
   every message -- exactly the cost regression Phase 6 must avoid.

   Instead, :meth:`resolve_query_entities` generates candidate
   n-grams from the query and resolves them with ONE indexed
   ``IN (...)`` lookup (``find_entities_by_names``). It is
   deterministic, has no token cost, and costs a single SQLite
   query.

   An LLM entity extractor remains available for the AGENTIC path
   (sub-phase 6C), where a multi-second latency budget is already
   accepted. It is injected separately and never used here.

2. **Score is purely distance-based.**
   ``score = 1 / (1 + distance)``, so a directly-linked memory scores
   ``1.0``, a one-hop neighbour ``0.5``, and a two-hop neighbour
   ``0.333``. Distance is a property of the traversal, not of the
   edge, so no per-entity normalization is needed and two searches
   with different entity counts produce comparable numbers.

3. **A memory seen through several paths keeps its BEST score.**
   The same memory can be linked to two entities found in one query.
   Deduplication keeps the highest score (shortest distance), which
   is the correct reading: "there is a direct link" dominates "there
   is a two-hop link".

4. **Scope is mandatory for retrieval use.**
   ``find_related`` forwards ``scope_refs`` to the store. It defaults
   to ``None`` (no restriction) ONLY for diagnostics; the hybrid
   provider always passes the live scope set, otherwise one project's
   memories would leak into another's results.

5. **Never raises.**
   Every store interaction is wrapped. A graph failure must degrade
   to "no graph contribution", never to a failed search -- the graph
   is an ADDITIONAL recall source, and losing it must leave the
   lexical and vector results intact.

6. **Bounded.**
   ``max_depth``, ``limit``, ``memories_per_entity`` and the
   traversal caps in ``GraphStore.traverse`` together bound the work.
   A hub entity cannot turn one search into a full table scan.

See also
--------

    * ``peldrun/memory/graph/contracts.py``   -- ``Entity``/``RelationHop``
    * ``omweb/memory/storage/graph_store.py`` -- the concrete store
    * ``omweb/memory/manager/hybrid_provider.py`` -- consumes this
    * ``peldrun/memory/agentic/``             -- the LLM-based agentic
                                                 path (6C)
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Protocol, Sequence, Tuple

from peldrun.memory.domain.entry import MemoryEntry
from peldrun.memory.graph.contracts import (
    Entity,
    RelationHop,
    canonicalize_name,
)


logger = logging.getLogger(
    "peldrun.memory.graph.graph_search"
)


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------


class GraphStoreProtocol(Protocol):
    """The subset of the graph store this searcher needs.

    Declared here (in ``peldrun``) so the searcher depends on an
    INTERFACE, not on ``omweb``'s SQLite implementation. Any object
    with these three coroutines works -- including a test double.
    """

    async def find_entities_by_names(
        self,
        names: Sequence[str],
        entity_type: Optional[str] = None,
    ) -> List[Entity]: ...

    async def get_memories_for_entity(
        self,
        entity_id: str,
        *,
        limit: int = ...,
        scope_refs: Optional[Sequence[str]] = None,
    ) -> List[MemoryEntry]: ...

    async def traverse(
        self,
        start_entity_id: str,
        *,
        max_depth: int = ...,
        max_relations_per_entity: int = ...,
        max_total_relations: int = ...,
    ) -> List[RelationHop]: ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_MAX_DEPTH: int = 2
"""Default traversal depth for search.

Two hops is what answers the questions Phase 6 targets: person ->
organisation -> statements, and person -> project -> statements.
"""


DEFAULT_LIMIT: int = 20
"""Default cap on returned memories."""


DEFAULT_MEMORIES_PER_ENTITY: int = 20
"""Cap on memories hydrated per entity (start node or neighbour).

An entity can be linked to thousands of memories; the fusion only
needs a handful per entity to influence the ranking.
"""


DEFAULT_MAX_RELATIONS_PER_ENTITY: int = 20
"""Forwarded to ``traverse``; see ``GraphStore`` for the rationale."""


DEFAULT_MAX_TOTAL_RELATIONS: int = 100
"""Forwarded to ``traverse``; the true explosion guard."""


_MAX_QUERY_NGRAM: int = 3
"""Longest n-gram considered when resolving query entities.

3 covers "PELDRUN Memory Platform" and person names
"Mohamed Ahmed Ali". Longer spans add candidates without adding
signal, because extracted entity names are rarely longer.
"""


_MAX_QUERY_CANDIDATES: int = 40
"""Cap on n-gram candidates sent to the single lookup query.

A 20-token query yields up to ~60 spans; 40 is plenty (longer spans
are emitted first, and those are the ones that actually match entity
names). Bounds the ``IN (...)`` parameter count.
"""


_STOPWORDS: frozenset = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "if", "then", "than",
        "that", "this", "these", "those", "there", "here", "what",
        "which", "who", "whom", "whose", "when", "where", "why",
        "how", "is", "are", "was", "were", "be", "been", "being",
        "do", "does", "did", "doing", "have", "has", "had",
        "having", "will", "would", "shall", "should", "can",
        "could", "may", "might", "must", "not", "no", "yes",
        "of", "in", "on", "at", "to", "for", "with", "about",
        "from", "by", "as", "into", "over", "after", "before",
        "my", "your", "his", "her", "its", "our", "their", "me",
        "you", "he", "she", "it", "we", "they", "them", "us",
        "i", "say", "said", "says", "tell", "told", "think",
        "thought", "know", "knew", "want", "wanted", "like",
    }
)
"""Tokens that never help resolve an entity on their own.

Only applied to SINGLE-token candidates. Multi-token spans keep
their stopwords, because "the project" style phrases are sometimes
genuine entity names and dropping the span loses the match.
"""


_TOKEN_SPLIT_RE = re.compile(r"[^\w]+", re.UNICODE)
"""Split a query into word tokens.

``\\w`` is unicode-aware, so Arabic, Cyrillic, and Latin text all
tokenize correctly. CJK text has no spaces and therefore yields one
long token -- acceptable: a CJK entity name is usually the whole
token anyway.
"""


# ---------------------------------------------------------------------------
# GraphSearch
# ---------------------------------------------------------------------------


class GraphSearch:
    """Depth-N graph retrieval over an injected ``GraphStoreProtocol``.

    Stateless; safe to share across concurrent searches.
    """

    def __init__(
        self,
        store: GraphStoreProtocol,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        limit: int = DEFAULT_LIMIT,
        memories_per_entity: int = DEFAULT_MEMORIES_PER_ENTITY,
        max_relations_per_entity: int = (
            DEFAULT_MAX_RELATIONS_PER_ENTITY
        ),
        max_total_relations: int = DEFAULT_MAX_TOTAL_RELATIONS,
        min_query_entities: int = 1,
        max_query_candidates: int = _MAX_QUERY_CANDIDATES,
    ) -> None:
        """Construct the searcher.

        Parameters
        ----------
        store
            Anything satisfying ``GraphStoreProtocol``.
        max_depth
            Traversal depth. ``1`` = direct neighbours only.
            ``0`` or negative disables neighbour expansion (only
            memories linked straight to a query entity are returned).
        limit
            Default cap on returned memories.
        memories_per_entity
            Hydration cap per entity.
        max_relations_per_entity, max_total_relations
            Forwarded to ``traverse``.
        min_query_entities
            How many entities a query must resolve before the graph
            participates. ``1`` (default) means "any known entity is
            enough". Raising it makes graph contribution stricter,
            which is useful when a deployment's graph is sparse and
            single-token matches are mostly noise.
        max_query_candidates
            Cap on n-gram candidates per lookup.

        Raises
        ------
        TypeError
            When ``store`` is ``None``. A searcher with no store can
            only ever return an empty list, which is a wiring bug
            worth surfacing early.
        """
        if store is None:
            raise TypeError(
                "GraphSearch requires a GraphStore; got None."
            )
        self._store = store
        self._max_depth = max(0, int(max_depth))
        self._limit = max(1, int(limit))
        self._memories_per_entity = max(1, int(memories_per_entity))
        self._max_relations_per_entity = max(
            1, int(max_relations_per_entity)
        )
        self._max_total_relations = max(1, int(max_total_relations))
        self._min_query_entities = max(1, int(min_query_entities))
        self._max_query_candidates = max(
            1, int(max_query_candidates)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def find_related(
        self,
        entity_name: str,
        *,
        max_depth: Optional[int] = None,
        limit: Optional[int] = None,
        scope_refs: Optional[Sequence[str]] = None,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Return ``(entry, score)`` for memories related to a name.

        Parameters
        ----------
        entity_name
            A surface form ("Ahmed", "AcmeCorp"). Folded internally,
            so case and whitespace do not matter.
        max_depth
            Override for ``self._max_depth``.
        limit
            Override for ``self._limit``.
        scope_refs
            Scope restriction forwarded to the store. ``None`` means
            NO restriction -- diagnostics only. Retrieval callers
            MUST pass the live scope set.

        Returns
        -------
        List[Tuple[MemoryEntry, float]]
            Ordered by score desc, then ``memory_id`` asc (stable).
            Empty when the name is unknown, the store fails, or
            nothing is linked. An unknown entity is the COMMON case
            and is not logged above DEBUG.
        """
        entities = await self._resolve_names([entity_name])
        if not entities:
            return []
        return await self._collect(
            entities,
            max_depth=max_depth,
            limit=limit,
            scope_refs=scope_refs,
        )

    async def find_related_for_query(
        self,
        query: str,
        *,
        scope_refs: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
        max_depth: Optional[int] = None,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Resolve entities from a natural-language query, then search.

        The hybrid provider calls this on every ``search_ranked``
        invocation when graph search is enabled. See invariant #1 for
        why it does NOT use the LLM.

        Returns ``[]`` when the query mentions no known entity --
        which is the expected outcome for most queries, so it is
        logged at DEBUG only.
        """
        entities = await self.resolve_query_entities(query)
        if len(entities) < self._min_query_entities:
            return []
        return await self._collect(
            entities,
            max_depth=max_depth,
            limit=limit,
            scope_refs=scope_refs,
        )

    async def resolve_query_entities(
        self,
        query: str,
        *,
        limit: int = 3,
    ) -> List[Entity]:
        """Return the persisted entities a query mentions.

        Candidate generation
        --------------------
        The query is tokenized and 1..``_MAX_QUERY_NGRAM``-token spans
        are produced, LONGEST FIRST. Longest-first matters: for
        "PELDRUN Memory Platform" the 3-gram matches the project
        entity, and emitting the 1-grams first would spend the
        candidate budget on "platform", "memory", "peldrun"
        separately.

        Single-token candidates that are stopwords, or shorter than
        two characters, are dropped. Multi-token spans keep their
        stopwords (see ``_STOPWORDS``).

        All surviving candidates go to the store in ONE query.

        Returns at most ``limit`` entities, in candidate order, which
        makes the resolution deterministic for a given graph.
        """
        candidates = _query_ngrams(
            query, max_candidates=self._max_query_candidates
        )
        if not candidates:
            return []
        found = await self._resolve_names(candidates)
        if not found:
            return []
        # Preserve candidate order: the entity resolved from the
        # longest span is the most specific match for the query.
        order: Dict[str, int] = {}
        for index, candidate in enumerate(candidates):
            key = canonicalize_name(candidate)
            if key and key not in order:
                order[key] = index
        found.sort(
            key=lambda entity: (
                order.get(entity.canonical_name, len(order)),
                entity.entity_id,
            )
        )
        capped = max(1, int(limit))
        return found[:capped] if len(found) > capped else found

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _resolve_names(
        self, names: Sequence[str]
    ) -> List[Entity]:
        """Look up several names in one query; ``[]`` on any failure."""
        cleaned = [
            name for name in names
            if isinstance(name, str) and name.strip()
        ]
        if not cleaned:
            return []
        try:
            return await self._store.find_entities_by_names(cleaned)
        except Exception as exc:  # noqa: BLE001 -- degrade only
            logger.warning(
                "GraphSearch: entity lookup failed (%s: %s); "
                "no graph contribution for this search.",
                type(exc).__name__,
                exc,
            )
            return []

    async def _collect(
        self,
        entities: Sequence[Entity],
        *,
        max_depth: Optional[int],
        limit: Optional[int],
        scope_refs: Optional[Sequence[str]],
    ) -> List[Tuple[MemoryEntry, float]]:
        """Gather and score memories for the resolved entities.

        Algorithm
        ---------
        For each resolved entity:

          1. hydrate its DIRECTLY linked memories  -> score ``1.0``,
          2. traverse its neighbourhood, then hydrate each discovered
             far endpoint -> score ``1 / (1 + distance)``.

        A memory discovered twice keeps its highest score (invariant
        #3). The walk stops early once ``limit`` unique memories are
        collected, so a dense graph cannot make one search expensive.
        """
        depth = self._max_depth if max_depth is None else max(
            0, int(max_depth)
        )
        cap = self._limit if limit is None else max(1, int(limit))

        scores: Dict[str, float] = {}
        entries: Dict[str, MemoryEntry] = {}

        for entity in entities:
            if len(scores) >= cap:
                break

            direct = await self._memories(
                entity.entity_id, scope_refs
            )
            for entry in direct:
                _record(entry, 1.0, scores, entries)

            if depth <= 0 or len(scores) >= cap:
                continue

            hops = await self._traverse(entity.entity_id, depth)
            for hop in hops:
                if len(scores) >= cap:
                    break
                far_end = _far_endpoint(hop, entity.entity_id)
                if far_end is None:
                    continue
                memories = await self._memories(far_end, scope_refs)
                hop_score = 1.0 / (1.0 + float(hop.distance))
                for entry in memories:
                    _record(entry, hop_score, scores, entries)

        ordered = sorted(
            scores.items(),
            key=lambda item: (-item[1], item[0]),
        )[:cap]
        return [(entries[memory_id], score) for memory_id, score in ordered]

    async def _memories(
        self,
        entity_id: str,
        scope_refs: Optional[Sequence[str]],
    ) -> List[MemoryEntry]:
        """Hydrate one entity's memories; ``[]`` on any failure."""
        try:
            return await self._store.get_memories_for_entity(
                entity_id,
                limit=self._memories_per_entity,
                scope_refs=scope_refs,
            )
        except Exception as exc:  # noqa: BLE001 -- degrade only
            logger.warning(
                "GraphSearch: memory hydration failed for entity %r "
                "(%s: %s).", entity_id, type(exc).__name__, exc,
            )
            return []

    async def _traverse(
        self, entity_id: str, depth: int
    ) -> List[RelationHop]:
        """Traverse from one entity; ``[]`` on any failure."""
        try:
            return await self._store.traverse(
                entity_id,
                max_depth=depth,
                max_relations_per_entity=(
                    self._max_relations_per_entity
                ),
                max_total_relations=self._max_total_relations,
            )
        except Exception as exc:  # noqa: BLE001 -- degrade only
            logger.warning(
                "GraphSearch: traversal failed for entity %r "
                "(%s: %s).", entity_id, type(exc).__name__, exc,
            )
            return []

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def store(self) -> GraphStoreProtocol:
        """The injected store (for diagnostics and tests)."""
        return self._store

    @property
    def max_depth(self) -> int:
        """The configured default traversal depth."""
        return self._max_depth

    @property
    def limit(self) -> int:
        """The configured default result cap."""
        return self._limit

    def __repr__(self) -> str:
        return (
            f"GraphSearch(max_depth={self._max_depth}, "
            f"limit={self._limit}, "
            f"memories_per_entity={self._memories_per_entity})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(
    entry: MemoryEntry,
    score: float,
    scores: Dict[str, float],
    entries: Dict[str, MemoryEntry],
) -> None:
    """Record ``entry`` at ``score`` if it improves on a previous hit."""
    if entry is None or not entry.memory_id:
        return
    existing = scores.get(entry.memory_id)
    if existing is not None and existing >= score:
        return
    scores[entry.memory_id] = score
    entries[entry.memory_id] = entry


def _far_endpoint(
    hop: RelationHop, start_entity_id: str
) -> Optional[str]:
    """Return the endpoint of ``hop`` that is NOT the start entity.

    ``GraphStore.traverse`` already returns the edge plus its
    distance; this recovers the node the walk ARRIVED at. When the
    start entity is neither endpoint (impossible with a correct
    traversal, but cheap to guard) the target is used.
    """
    relation = hop.relation
    if relation.source_entity_id == start_entity_id:
        return relation.target_entity_id
    if relation.target_entity_id == start_entity_id:
        return relation.source_entity_id
    return relation.target_entity_id


def _query_ngrams(
    query: str,
    *,
    max_candidates: int = _MAX_QUERY_CANDIDATES,
) -> List[str]:
    """Generate entity-name candidates from a natural-language query.

    Emission order
    --------------
    Longest span first (3-gram, then 2-gram, then 1-gram). WITHIN a
    span length, spans whose first and last tokens are both
    non-stopwords come first: for "the Peldrun Memory Platform
    rocks", the 3-grams are "the peldrun memory",
    "peldrun memory platform", and "memory platform rocks". All three
    are looked up, but the middle one is the phrase an entity is
    actually named after, so it must not be crowded out if the
    candidate budget is exhausted. Stopword-bounded spans are still
    emitted (an entity really can be called "The Beatles"), just
    later.

    Filtering
    ---------
      * non-strings / blank queries -> ``[]``,
      * tokens of length < 2 dropped from single-token candidates
        (a lone "a" or "I" never names an entity),
      * single-token stopwords dropped,
      * a multi-token span whose tokens are ALL stopwords dropped
        ("of the"),
      * duplicates removed case-insensitively,
      * output capped at ``max_candidates``.

    Returns the candidates as lower-cased display strings; the store
    folds them again, which is idempotent.
    """
    if not isinstance(query, str):
        return []
    tokens = [
        token for token in _TOKEN_SPLIT_RE.split(query.lower())
        if token
    ]
    if not tokens:
        return []

    candidates: List[str] = []
    seen: set = set()

    def _accept(span_tokens: List[str]) -> None:
        if len(candidates) >= max_candidates:
            return
        if all(token in _STOPWORDS for token in span_tokens):
            return
        if len(span_tokens) == 1:
            only = span_tokens[0]
            if len(only) < 2 or only in _STOPWORDS:
                return
        phrase = " ".join(span_tokens)
        if phrase in seen:
            return
        seen.add(phrase)
        candidates.append(phrase)

    for span_length in range(
        min(_MAX_QUERY_NGRAM, len(tokens)), 0, -1
    ):
        if len(candidates) >= max_candidates:
            break
        # Rank spans of this length: fewer stopword edges first, then
        # left-to-right, so the most entity-like phrase leads.
        spans: List[Tuple[int, int, List[str]]] = []
        for start in range(0, len(tokens) - span_length + 1):
            span = tokens[start:start + span_length]
            edges = int(span[0] in _STOPWORDS) + int(
                span[-1] in _STOPWORDS
            )
            spans.append((edges, start, span))
        spans.sort(key=lambda item: (item[0], item[1]))
        for _edges, _start, span in spans:
            if len(candidates) >= max_candidates:
                return candidates
            _accept(span)

    return candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_DEPTH",
    "GraphSearch",
    "GraphStoreProtocol",
]
