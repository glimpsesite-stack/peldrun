# backend/peldrun/memory/agentic/__init__.py
"""
PELDRUN Memory Platform - Agentic RAG Core Package.

Public import surface for Phase 6's agentic retrieval, which turns a
complex question into a synthesized answer backed by evidence:

    QueryRewriter        -- question -> 1..3 standalone sub-queries
    EvidenceSynthesizer  -- gap analysis + grounded answer
    MultiHopRetriever    -- the bounded retrieve/judge loop

Layering
--------
``peldrun`` owns the ALGORITHMS and the CONTRACTS. ``omweb`` owns the
WIRING (``MemoryService.retrieve_agentic``, the HTTP endpoint, and
the LLM gateway injection).

Nothing in this package imports ``omweb``:

  * the LLM arrives as ``StructuredGatewayProtocol``
    (``peldrun.memory.llm_contracts``),
  * the search backend arrives as ``MemorySearchProtocol``
    (``peldrun.memory.agentic.contracts``), which
    ``MemoryService.provider`` satisfies structurally.

That is what lets the whole pipeline be tested against a two-method
double with no database and no provider.

Invariant worth repeating
-------------------------
The agentic path is NOT wired into ``MemoryService.retrieve``. That
method runs on every dispatcher turn and must stay in the
hundreds-of-milliseconds range. Agentic retrieval costs several LLM
calls by design, so it is reachable ONLY through
``retrieve_agentic`` and ``POST /api/memory/search/agentic``.
"""

from __future__ import annotations

from peldrun.memory.agentic.contracts import (
    AgenticRetrievalResult,
    GapAnalysis,
    MemorySearchProtocol,
    MultiHopResult,
    SynthesizedAnswer,
)
from peldrun.memory.agentic.evidence_synthesizer import (
    EvidenceSynthesizer,
)
from peldrun.memory.agentic.multi_hop_retriever import (
    MultiHopRetriever,
)
from peldrun.memory.agentic.orchestrator import (
    AgenticRAGOrchestrator,
)
from peldrun.memory.agentic.query_rewriter import QueryRewriter


__all__ = [
    "AgenticRAGOrchestrator",
    "AgenticRetrievalResult",
    "EvidenceSynthesizer",
    "GapAnalysis",
    "MemorySearchProtocol",
    "MultiHopResult",
    "MultiHopRetriever",
    "QueryRewriter",
    "SynthesizedAnswer",
]
