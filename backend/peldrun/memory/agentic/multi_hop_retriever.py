# backend/peldrun/memory/agentic/multi_hop_retriever.py
"""
PELDRUN Memory Platform - Multi-Hop Retriever.

The control loop of agentic retrieval: rewrite the question, search,
judge whether the evidence is sufficient, and either stop or search
again with the gaps' suggested queries.

Flow
----

    rewrite(query) -> [q1, q2, q3]
    hop 1: search(q1..q3)                -> evidence
           identify_gaps(evidence)       -> needed? next_queries
    hop 2: search(next_queries)          -> more evidence
           identify_gaps(all evidence)   -> ...
    ...
    synthesize(query, all evidence)      -> answer

Design invariants
-----------------

1. **Hop count is bounded by ``max_hops`` AND by progress.**
   The loop stops on whichever comes first: ``max_hops`` rounds, an
   empty gap analysis, a hop that found nothing NEW, or a hop whose
   gap queries are identical to the ones just tried. The last two
   matter most in practice -- a model that keeps proposing the same
   refinement would otherwise burn every remaining hop re-running
   identical searches.

2. **Evidence is deduplicated across hops.**
   A memory found by hop 1 and again by hop 3 is ONE piece of
   evidence, keeping its FIRST (highest-priority) score. Without
   this, the cross-encoder/ranker downstream would see duplicated
   entries and the synthesis prompt would waste its budget on
   repeats.

3. **Exactly ONE synthesis call, at the end.**
   Synthesis is the most expensive call and depends on the final
   evidence set. Calling it per hop would multiply cost and produce
   answers the caller must then reconcile.

4. **A hop that returns nothing STOPS the loop.**
   If a round of searches produced no new memory, further rounds
   search the same index for refinements of the same question; the
   probability of new results is low and the cost is certain.

5. **The search backend is a Protocol, not a concrete provider.**
   ``MemorySearchProtocol`` is satisfied by
   ``MemoryService.provider`` in both its shapes (FTS-only and
   hybrid). That means the agentic path automatically inherits
   hybrid recall AND the Phase 6 graph source with no change here.

6. **Never raises.**
   Every search, every LLM judgement, and every normalization step
   maps its failure onto a documented default. An agentic request
   that cannot proceed returns the evidence it has plus no answer --
   the caller decides whether to fall back.

See also
--------

    * ``peldrun/memory/agentic/contracts.py`` -- result objects
    * ``peldrun/memory/agentic/query_rewriter.py``
    * ``peldrun/memory/agentic/evidence_synthesizer.py``
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence, Tuple

from peldrun.memory.agentic.contracts import (
    GapAnalysis,
    MemorySearchProtocol,
    MultiHopResult,
    SynthesizedAnswer,
)
from peldrun.memory.agentic.evidence_synthesizer import (
    EvidenceSynthesizer,
)
from peldrun.memory.agentic.query_rewriter import QueryRewriter
from peldrun.memory.domain.entry import MemoryEntry


logger = logging.getLogger(
    "peldrun.memory.agentic.multi_hop_retriever"
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_MAX_HOPS: int = 3
"""Maximum retrieval rounds.

Hop 1 is the rewritten question; hops 2..N chase the gaps. Three is
the practical ceiling for a memory question: beyond it, the model is
usually circling because the information simply is not stored, and
each extra hop costs ``len(queries)`` searches plus one LLM call.
"""


DEFAULT_EVIDENCE_PER_HOP: int = 5
"""Results requested per sub-query.

Five per sub-query keeps a 3-query hop at 15 candidates, matching
the window the ranker considers. Raising it multiplies the cost of
every hop.
"""


DEFAULT_MAX_EVIDENCE: int = 30
"""Absolute ceiling on accumulated evidence.

Bounds both the synthesis prompt and memory use. When reached, the
loop stops: further hops could only add entries the synthesizer
would never see.
"""


_EMPTY_QUERY_MARKER: str = ""


# ---------------------------------------------------------------------------
# MultiHopRetriever
# ---------------------------------------------------------------------------


class MultiHopRetriever:
    """Iterative, gap-driven retrieval.

    Stateless; safe to share across concurrent requests (the loop's
    state is entirely local to one ``retrieve`` call).
    """

    def __init__(
        self,
        rewriter: QueryRewriter,
        synthesizer: EvidenceSynthesizer,
        *,
        max_hops: int = DEFAULT_MAX_HOPS,
        evidence_per_hop: int = DEFAULT_EVIDENCE_PER_HOP,
        max_evidence: int = DEFAULT_MAX_EVIDENCE,
    ) -> None:
        """Construct the retriever.

        Parameters
        ----------
        rewriter
            Decomposes the original question. Required.
        synthesizer
            Provides gap analysis and the final answer. Required.
        max_hops
            Maximum retrieval rounds; clamped to ``>= 1`` so the
            loop always performs at least one search.
        evidence_per_hop
            Results requested per sub-query per hop.
        max_evidence
            Ceiling on accumulated evidence.

        Raises
        ------
        TypeError
            When ``rewriter`` or ``synthesizer`` is ``None`` -- both
            are structurally required by the loop.
        """
        if rewriter is None:
            raise TypeError(
                "MultiHopRetriever requires a QueryRewriter; got None."
            )
        if synthesizer is None:
            raise TypeError(
                "MultiHopRetriever requires an EvidenceSynthesizer; "
                "got None."
            )
        self._rewriter = rewriter
        self._synthesizer = synthesizer
        self._max_hops = max(1, int(max_hops))
        self._evidence_per_hop = max(1, int(evidence_per_hop))
        self._max_evidence = max(1, int(max_evidence))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        original_query: str,
        scope_refs: Sequence[str],
        *,
        backend: Optional[MemorySearchProtocol] = None,
        seed_evidence: Optional[
            Sequence[Tuple[MemoryEntry, float]]
        ] = None,
        max_hops: Optional[int] = None,
    ) -> MultiHopResult:
        """Run the multi-hop loop and synthesize an answer.

        Parameters
        ----------
        original_query
            The user's question.
        scope_refs
            Canonical scope references. An EMPTY sequence means
            "nothing is searchable" and returns an empty result
            without calling the backend or the LLM -- an empty scope
            set is never widened to "all scopes".
        backend
            The search implementation (``MemoryService.provider``).
            ``None`` yields an empty result rather than an
            exception; a caller that forgot to pass one gets
            "nothing found", which is a visible, debuggable outcome.
        seed_evidence
            Evidence collected BEFORE the loop runs -- used by
            ``AgenticRAGOrchestrator`` to inject knowledge-graph
            results. Seeded entries participate in deduplication (a
            hop that finds the same memory does not add it twice)
            and, crucially, in the FINAL SYNTHESIS -- so the answer
            sees graph evidence without a second LLM call. A seed
            entry that fails to normalize is dropped.
        max_hops
            Per-call override for the constructed ``max_hops``.
            Implemented as a local effective value rather than by
            mutating the instance, because the same retriever object
            serves concurrent requests and a mutation would leak
            one request's budget into another's.

        Returns
        -------
        MultiHopResult
            Always well-formed. ``synthesized_answer`` is ``None``
            only when there was no evidence at all (seed included)
            to synthesize from.
        """
        query = (
            original_query.strip()
            if isinstance(original_query, str)
            else ""
        )
        if not query or not scope_refs or backend is None:
            return MultiHopResult()

        effective_max_hops = self._max_hops
        if max_hops is not None:
            try:
                effective_max_hops = max(1, int(max_hops))
            except (TypeError, ValueError):
                effective_max_hops = self._max_hops

        # ---- Seed evidence (graph results, or a caller's head start)
        evidence: List[Tuple[MemoryEntry, float]] = []
        seen_ids: set = set()
        for item in seed_evidence or ():
            try:
                entry, score = item
            except (TypeError, ValueError):
                continue
            if entry is None or not getattr(entry, "memory_id", None):
                continue
            if entry.memory_id in seen_ids:
                continue
            seen_ids.add(entry.memory_id)
            evidence.append((entry, float(score)))
            if len(evidence) >= self._max_evidence:
                break

        # ---- Hop 1: the rewritten question ----------------------
        queries: List[str] = await self._rewriter.rewrite(query)
        queries = _normalize_queries(queries) or [query]

        queries_used: List[str] = []
        hops_used = 0
        tried_query_sets: List[frozenset] = []

        for hop in range(effective_max_hops):
            if len(evidence) >= self._max_evidence:
                break
            if not queries:
                break
            current_set = frozenset(q.casefold() for q in queries)
            if current_set in tried_query_sets:
                # The gap analysis proposed the same queries again.
                # Re-running identical searches cannot help.
                logger.debug(
                    "MultiHopRetriever: gap analysis repeated the "
                    "previous query set; stopping at hop %d.",
                    hop + 1,
                )
                break
            tried_query_sets.append(current_set)

            hops_used = hop + 1
            new_evidence = await self._search_hop(
                queries, scope_refs, backend, seen_ids
            )
            for sub_query in queries:
                if sub_query not in queries_used:
                    queries_used.append(sub_query)

            if not new_evidence:
                # Nothing new. Further hops would search the same
                # index for refinements of the same question.
                logger.debug(
                    "MultiHopRetriever: hop %d produced no new "
                    "evidence; stopping.", hop + 1,
                )
                break

            evidence.extend(new_evidence)

            if len(evidence) >= self._max_evidence:
                break
            if hop + 1 >= effective_max_hops:
                break

            gaps: GapAnalysis = (
                await self._synthesizer.identify_gaps(query, evidence)
            )
            if not gaps.should_continue:
                break
            queries = _normalize_queries(list(gaps.next_queries))
            if not queries:
                break

        # ---- Synthesize once, from the final evidence -----------
        answer: Optional[SynthesizedAnswer] = None
        if evidence:
            answer = await self._synthesizer.synthesize(query, evidence)
            if not answer.is_substantive:
                # Keep the object (confidence/citations may still be
                # meaningful) but do not claim an answer exists.
                answer = answer if answer.citations else None

        return MultiHopResult(
            evidence=tuple(evidence),
            hops_used=hops_used,
            sub_queries_used=tuple(queries_used),
            synthesized_answer=answer,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _search_hop(
        self,
        queries: Sequence[str],
        scope_refs: Sequence[str],
        backend: MemorySearchProtocol,
        seen_ids: set,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Search every query once; return only NEW evidence.

        ``seen_ids`` is mutated in place so the caller's dedupe set
        stays authoritative across hops. A per-query failure is
        logged and skipped -- one bad query must not abort the hop.
        """
        collected: List[Tuple[MemoryEntry, float]] = []
        for sub_query in queries:
            remaining = self._evidence_per_hop
            if not remaining:
                continue
            try:
                hits = await backend.search_ranked(
                    sub_query,
                    list(scope_refs),
                    limit=self._evidence_per_hop,
                    # FTS operator: the sub-queries are natural
                    # language ("what did ahmed say about the
                    # project"), and implicit AND across their tokens
                    # matches almost nothing. `match_any` makes the
                    # backend OR the terms (with prefix wildcards), so
                    # a hop can actually find evidence. Precision is
                    # recovered downstream by the ranker and by the
                    # synthesizer's citations.
                    match_any=True,
                    # Skip the Phase 5 LLM reranker on hops. Evidence
                    # selection here already happens ACROSS hops via
                    # fusion, so re-scoring every hop's candidates
                    # adds no accuracy while multiplying completions:
                    # ceil(candidates / batch_size) per sub-query
                    # (~36 per 3-hop run at the defaults). On a local
                    # model that amplification starved the agentic
                    # calls and produced the rerank-timeout storm.
                    no_rerank=True,
                )
            except Exception as exc:  # noqa: BLE001 -- per query
                logger.warning(
                    "MultiHopRetriever: search failed for %r "
                    "(%s: %s); skipping this sub-query.",
                    sub_query,
                    type(exc).__name__,
                    exc,
                )
                continue
            if not hits:
                continue
            for entry, score in hits:
                if entry is None or not getattr(
                    entry, "memory_id", None
                ):
                    continue
                if entry.memory_id in seen_ids:
                    continue
                seen_ids.add(entry.memory_id)
                collected.append((entry, float(score)))
                if len(collected) >= self._max_evidence:
                    return collected
        return collected

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def max_hops(self) -> int:
        """The configured hop ceiling."""
        return self._max_hops

    @property
    def evidence_per_hop(self) -> int:
        """Results requested per sub-query."""
        return self._evidence_per_hop

    @property
    def rewriter(self) -> QueryRewriter:
        """The injected rewriter."""
        return self._rewriter

    @property
    def synthesizer(self) -> EvidenceSynthesizer:
        """The injected synthesizer."""
        return self._synthesizer

    def __repr__(self) -> str:
        return (
            f"MultiHopRetriever(max_hops={self._max_hops}, "
            f"evidence_per_hop={self._evidence_per_hop}, "
            f"max_evidence={self._max_evidence})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_queries(queries: Sequence[Any]) -> List[str]:
    """Trim, drop empties, and deduplicate a query list.

    Comparison is casefolded so "Who is Ahmed" and "who is ahmed"
    count as one query -- re-searching a case variant returns the
    same rows (FTS folds case) and would waste a hop's budget.
    """
    cleaned: List[str] = []
    seen: set = set()
    for item in queries:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_EVIDENCE_PER_HOP",
    "DEFAULT_MAX_EVIDENCE",
    "DEFAULT_MAX_HOPS",
    "MultiHopRetriever",
]
