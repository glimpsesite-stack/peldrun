# backend/peldrun/memory/agentic/orchestrator.py
"""
PELDRUN Memory Platform - Agentic RAG Orchestrator.

Composes the three Phase 6 agentic pieces into the single pipeline
that ``MemoryService.retrieve_agentic`` drives:

    QueryRewriter        -> decomposes the question
    GraphSearch          -> entity-linked evidence (no LLM)
    MultiHopRetriever    -> rewritten multi-hop search + synthesis

Why an orchestrator instead of folding this into the retriever
----------------------------------------------------------------
The retriever owns the HOP LOOP. The graph is not a hop: it needs no
rewrite, costs no LLM call, and produces evidence for a DIFFERENT
reason (entity linkage rather than textual similarity). Keeping the
composition here means:

  * the retriever stays testable with a plain search backend,
  * the graph stays optional -- with no graph, the orchestrator IS
    the retriever,
  * the merge policy (dedupe + best score) lives in exactly one
    place.

Graph-before-hops, not in parallel
----------------------------------
The graph phase runs BEFORE the hop loop and its evidence is passed
in as ``seed_evidence``. That ordering is deliberate:

  * the final synthesis sees the graph evidence, so no second
    synthesis call is needed to incorporate it,
  * the graph phase costs one indexed SQLite query plus bounded
    traversals (tens of milliseconds), so it does not meaningfully
    delay the multi-second LLM budget,
  * running it in parallel would force a choice between wasting a
    synthesis call or answering without the graph's contribution.

Partial results on timeout
--------------------------
``MultiHopRetriever`` is a single await, so a timeout would
otherwise discard everything it had gathered. The orchestrator
captures the GRAPH phase's evidence as soon as it completes and
returns it -- with no synthesized answer -- when the hop loop
overruns. Partial evidence lets the caller fall back to the raw
memories, which is strictly better than an empty result.

Design invariants
-----------------

1. **Never raises.** Every failure maps onto a well-formed
   ``MultiHopResult``. ``CancelledError`` propagates.
2. **The graph is optional and non-fatal.** A graph failure is
   logged and the run continues without it.
3. **The timeout belongs to the orchestrator.** ``MemoryService``
   wraps the call with a slightly LOOSER backstop so this
   component's partial-result path wins the race.
4. **``max_hops`` is per-call.** A shared orchestrator serves
   concurrent requests; the override is threaded into the retriever
   as a local effective value, never stored.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional, Sequence, Tuple

from peldrun.memory.agentic.contracts import (
    MemorySearchProtocol,
    MultiHopResult,
)
from peldrun.memory.agentic.evidence_synthesizer import (
    EvidenceSynthesizer,
)
from peldrun.memory.agentic.multi_hop_retriever import (
    MultiHopRetriever,
)
from peldrun.memory.agentic.query_rewriter import QueryRewriter
from peldrun.memory.domain.entry import MemoryEntry


logger = logging.getLogger(
    "peldrun.memory.agentic.orchestrator"
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_TIMEOUT_SECONDS: float = 30.0
"""Wall-clock budget for one full agentic run.

Matches the §7.4 budget for a 3-hop run: one rewrite, up to three
gap analyses, and one synthesis comfortably fit in 30s against a
healthy provider, and a slower provider is better served by a
partial answer than by holding the request open.
"""


DEFAULT_GRAPH_LIMIT: int = 10
"""Memories the graph phase may contribute to one run.

Kept smaller than ``MultiHopRetriever.evidence_per_hop`` because
graph evidence is seeded as a head start, not as a search round:
a handful of entity-linked memories is enough to steer synthesis,
and a large seed would crowd out the hop's textual matches.
"""


_GRAPH_HOP_WEIGHT: float = 1.0
"""Nominal score assigned to graph evidence.

The graph phase's own scores (``1 / (1 + distance)``) are preserved
when available; this constant is only the fallback for a graph
result that carries no score. ``1.0`` ranks seeded evidence equal to
a direct hit, which is correct: an entity-linked memory IS a direct
match for the question's subject.
"""


# ---------------------------------------------------------------------------
# AgenticRAGOrchestrator
# ---------------------------------------------------------------------------


class AgenticRAGOrchestrator:
    """Full agentic pipeline: rewrite, graph seed, multi-hop, answer.

    Stateless; safe to share across concurrent requests.
    """

    def __init__(
        self,
        rewriter: QueryRewriter,
        retriever: MultiHopRetriever,
        synthesizer: EvidenceSynthesizer,
        graph_search: Optional[Any] = None,
        *,
        max_hops: int = 3,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        graph_limit: int = DEFAULT_GRAPH_LIMIT,
    ) -> None:
        """Construct the orchestrator.

        Parameters
        ----------
        rewriter
            Injecting the rewriter here (rather than only inside the
            retriever) lets a future phase rewrite once and reuse the
            sub-queries across graph and hops. It is currently
            passed through to the retriever, which owns the call.
        retriever
            The hop loop. Required.
        synthesizer
            Passed through; kept on this object so diagnostics can
            report which synthesizer produced the answer (the
            retriever's own reference is the one actually used).
        graph_search
            An object with
            ``find_related_for_query(query, scope_refs=..., limit=...)``
            -- ``peldrun.memory.graph.GraphSearch`` in production.
            ``None`` disables the graph phase entirely.
        max_hops
            Default hop budget; per-call overridable at ``retrieve``.
        timeout_seconds
            Run-wide ceiling. Applied by ``retrieve``; ``MemoryService``
            uses a looser backstop so this layer's partial-result path
            wins.
        graph_limit
            Cap on graph evidence injected as a seed.

        Raises
        ------
        TypeError
            When ``rewriter``, ``retriever``, or ``synthesizer`` is
            ``None``.
        """
        if rewriter is None:
            raise TypeError(
                "AgenticRAGOrchestrator requires a QueryRewriter; "
                "got None."
            )
        if retriever is None:
            raise TypeError(
                "AgenticRAGOrchestrator requires a MultiHopRetriever; "
                "got None."
            )
        if synthesizer is None:
            raise TypeError(
                "AgenticRAGOrchestrator requires an "
                "EvidenceSynthesizer; got None."
            )
        self._rewriter = rewriter
        self._retriever = retriever
        self._synthesizer = synthesizer
        self._graph_search = graph_search
        self._max_hops = max(1, int(max_hops))
        self._timeout_seconds = max(
            1.0, float(timeout_seconds)
        )
        self._graph_limit = max(0, int(graph_limit))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        scope_refs: Sequence[str],
        *,
        backend: Optional[MemorySearchProtocol] = None,
        max_hops: Optional[int] = None,
    ) -> MultiHopResult:
        """Run the full agentic pipeline.

        Returns
        -------
        MultiHopResult
            ``evidence`` includes graph-seeded memories (best score
            wins on duplicates). ``hops_used`` counts SEARCH hops
            only -- the graph phase is not a hop. When the run times
            out after the graph phase completed, the returned result
            carries the graph evidence and no synthesized answer.

        Never raises (``CancelledError`` excepted).
        """
        text = query.strip() if isinstance(query, str) else ""
        if not text or not scope_refs or backend is None:
            return MultiHopResult()

        hops = self._max_hops
        if max_hops is not None:
            try:
                hops = max(1, int(max_hops))
            except (TypeError, ValueError):
                hops = self._max_hops

        # Evidence captured OUTSIDE the timed coroutine so a timeout
        # can still return it. See "Partial results on timeout".
        graph_evidence: List[Tuple[MemoryEntry, float]] = []

        async def _run() -> MultiHopResult:
            seeded = await self._graph_phase(text, scope_refs)
            graph_evidence.extend(seeded)
            return await self._retriever.retrieve(
                text,
                scope_refs,
                backend=backend,
                seed_evidence=seeded,
                max_hops=hops,
            )

        try:
            return await asyncio.wait_for(
                _run(), timeout=self._timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "AgenticRAGOrchestrator: run timed out after %.1fs "
                "(query=%r, hops=%d); returning %d partial "
                "evidence item(s).",
                self._timeout_seconds,
                text[:60],
                hops,
                len(graph_evidence),
            )
            return MultiHopResult(
                evidence=tuple(graph_evidence),
                hops_used=0,
                sub_queries_used=(),
                synthesized_answer=None,
            )
        except Exception as exc:  # noqa: BLE001 -- documented
            logger.warning(
                "AgenticRAGOrchestrator: pipeline failed (%s: %s); "
                "returning %d partial evidence item(s).",
                type(exc).__name__,
                exc,
                len(graph_evidence),
            )
            return MultiHopResult(
                evidence=tuple(graph_evidence),
                hops_used=0,
                sub_queries_used=(),
                synthesized_answer=None,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _graph_phase(
        self,
        query: str,
        scope_refs: Sequence[str],
    ) -> List[Tuple[MemoryEntry, float]]:
        """Collect entity-linked evidence for ``query``.

        Returns ``[]`` when the graph is disabled, the graph is
        empty, the query mentions no known entity (the common
        case), or the graph raises. None of those is an error: the
        graph is an ADDITIONAL source, and its absence simply
        leaves the multi-hop path alone.
        """
        if self._graph_search is None or self._graph_limit <= 0:
            return []
        try:
            hits = await self._graph_search.find_related_for_query(
                query,
                scope_refs=list(scope_refs),
                limit=self._graph_limit,
            )
        except Exception as exc:  # noqa: BLE001 -- non-fatal
            logger.warning(
                "AgenticRAGOrchestrator: graph phase failed "
                "(%s: %s); continuing without graph evidence.",
                type(exc).__name__,
                exc,
            )
            return []

        seeded: List[Tuple[MemoryEntry, float]] = []
        seen: set = set()
        for item in hits or ():
            try:
                entry, score = item
            except (TypeError, ValueError):
                continue
            if entry is None or not getattr(entry, "memory_id", None):
                continue
            if entry.memory_id in seen:
                continue
            seen.add(entry.memory_id)
            try:
                numeric = float(score)
            except (TypeError, ValueError):
                numeric = _GRAPH_HOP_WEIGHT
            if numeric != numeric:  # NaN
                numeric = _GRAPH_HOP_WEIGHT
            seeded.append((entry, numeric))
            if len(seeded) >= self._graph_limit:
                break
        return seeded

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def retriever(self) -> MultiHopRetriever:
        """The hop loop (used by ``MemoryService`` for diagnostics)."""
        return self._retriever

    @property
    def rewriter(self) -> QueryRewriter:
        """The injected rewriter."""
        return self._rewriter

    @property
    def synthesizer(self) -> EvidenceSynthesizer:
        """The injected synthesizer."""
        return self._synthesizer

    @property
    def graph_search(self) -> Optional[Any]:
        """The injected graph searcher, or ``None``."""
        return self._graph_search

    @property
    def timeout_seconds(self) -> float:
        """The run-wide ceiling."""
        return self._timeout_seconds

    @property
    def max_hops(self) -> int:
        """The default hop budget."""
        return self._max_hops

    @property
    def graph_enabled(self) -> bool:
        """True when the graph phase will run."""
        return self._graph_search is not None and self._graph_limit > 0

    def __repr__(self) -> str:
        return (
            f"AgenticRAGOrchestrator(max_hops={self._max_hops}, "
            f"timeout={self._timeout_seconds}s, "
            f"graph={'on' if self.graph_enabled else 'off'}, "
            f"graph_limit={self._graph_limit})"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_GRAPH_LIMIT",
    "DEFAULT_TIMEOUT_SECONDS",
    "AgenticRAGOrchestrator",
]
