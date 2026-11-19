# backend/tests/memory/test_agentic_end_to_end.py
"""
Phase 6-D acceptance tests: AgenticRAGOrchestrator + end-to-end.

Covers the ten required 6D tests plus the acceptance metric the plan
sets for the whole phase:

    Required
    --------
     1. ``test_simple_question_rewrites_to_one_query``
     2. ``test_manager_project_question_returns_answer``
     3. ``test_entity_link_question_finds_indirect_memory``
     4. ``test_multi_hop_stops_at_max_hops``
     5. ``test_agentic_timeout_returns_partial``
     6. ``test_agentic_falls_back_when_disabled``
     7. ``test_graph_search_returns_indirect_memories``
     8. ``test_full_pipeline_integration``
     9. ``test_confidence_below_threshold_flagged``
    10. ``test_original_retrieve_still_fast``

    Acceptance metric
    -----------------
    ``test_multi_hop_accuracy_over_ten_questions`` -- the plan's
    "multi-hop accuracy >= 0.7 on 10 complex questions".

    What that metric actually measures HERE
    ---------------------------------------
    The LLM is scripted, so this is not a model-quality benchmark.
    Each scripted answer is returned ONLY IF the retrieved evidence
    contains the fact the answer depends on; otherwise the scripted
    model reports "insufficient evidence". The measured accuracy is
    therefore the fraction of questions for which the pipeline
    DELIVERED THE RIGHT EVIDENCE to the synthesizer -- the property
    Phase 6 actually implements and the one a regression would break.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
import pytest_asyncio

from omweb.memory.storage.graph_store import GraphStore
from omweb.memory.storage.migrations import apply_schema
from omweb.memory.storage.repository import SqliteMemoryRepository
from peldrun.memory.agentic import (
    AgenticRAGOrchestrator,
    AgenticRetrievalResult,
    EvidenceSynthesizer,
    MultiHopRetriever,
    QueryRewriter,
)
from peldrun.memory.agentic.orchestrator import (
    DEFAULT_TIMEOUT_SECONDS,
)
from peldrun.memory.domain import (
    MemoryEntry,
    MemoryMode,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
    default_settings,
)
from peldrun.memory.graph import Entity, GraphSearch, Relation


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Resp:
    """Mirrors ``LLMStructuredResponse``."""

    def __init__(self, parsed: Dict[str, Any]) -> None:
        self.parsed = parsed
        self.raw_text = ""
        self.cost_nano_usd = 0
        self.input_tokens = 0
        self.output_tokens = 0


class _StageGateway:
    """Scripted gateway that branches on the PROMPT STAGE.

    The three Phase 6 prompts are mutually distinguishable by their
    opening sentence, so one gateway can serve rewrite, gap analysis,
    and synthesis without the caller threading a stage token.
    """

    REWRITE_MARKER = "Decompose this complex question"
    GAP_MARKER = "Decide whether the evidence"
    SYNTH_MARKER = "Answer the question using ONLY the evidence"

    def __init__(
        self,
        *,
        queries: Optional[Sequence[str]] = None,
        needed: bool = False,
        next_queries: Optional[Sequence[str]] = None,
        answer: str = "",
        confidence: float = 0.0,
        citations: Optional[Sequence[int]] = None,
        delay: float = 0.0,
        required_evidence: Optional[str] = None,
        answer_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self.queries = list(queries or [])
        self.needed = needed
        self.next_queries = list(next_queries or [])
        self.answer = answer
        self.confidence = confidence
        self.citations = list(citations or [1])
        self.delay = delay
        self.required_evidence = required_evidence
        self.answer_map = answer_map or {}
        self.calls: List[Dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        prompt = kwargs.get("user_prompt", "")

        if self.REWRITE_MARKER in prompt:
            return _Resp({"rewritten_queries": self.queries})
        if self.GAP_MARKER in prompt:
            return _Resp(
                {
                    "needed": self.needed,
                    "next_queries": self.next_queries,
                }
            )
        # Synthesis.
        if self.answer_map:
            for needle, answer in self.answer_map.items():
                if needle in prompt:
                    return _Resp(
                        {"answer": answer, "confidence": 0.9,
                         "citations": [1]}
                    )
            return _Resp(
                {"answer": "", "confidence": 0.0, "citations": []}
            )
        if (
            self.required_evidence is not None
            and self.required_evidence not in prompt
        ):
            # Evidence did not arrive: report insufficiency.
            return _Resp(
                {"answer": "", "confidence": 0.0, "citations": []}
            )
        return _Resp(
            {
                "answer": self.answer,
                "confidence": self.confidence,
                "citations": self.citations,
            }
        )


class _KeywordBackend:
    """Stand-in for FTS: ranks by shared-token count.

    Deliberately crude -- it only has to be DETERMINISTIC so the
    accuracy metric measures the pipeline rather than the retriever.
    """

    def __init__(
        self,
        corpus: Sequence[MemoryEntry],
        *,
        delay: float = 0.0,
        fail_all: bool = False,
    ) -> None:
        self.corpus = list(corpus)
        self.delay = delay
        self.fail_all = fail_all
        self.queries: List[str] = []

    async def search_ranked(self, query, scope_refs, limit=5, **filters):
        self.queries.append(query)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_all:
            raise RuntimeError("backend down")
        tokens = {
            token
            for token in query.lower().replace("?", " ").split()
            if len(token) > 2
        }
        if not tokens:
            return []
        scored: List[Tuple[MemoryEntry, float]] = []
        for entry in self.corpus:
            content = entry.content.lower()
            overlap = sum(1 for token in tokens if token in content)
            if overlap:
                scored.append((entry, overlap / len(tokens)))
        scored.sort(key=lambda item: (-item[1], item[0].memory_id))
        return scored[:limit]


class _FakeGraphSearch:
    """Graph double that returns a fixed evidence list."""

    def __init__(self, hits=None, error=None) -> None:
        self.hits = list(hits or [])
        self.error = error
        self.calls = 0

    async def find_related_for_query(
        self, query, *, scope_refs=None, limit=20
    ):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.hits[:limit])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _entry(
    memory_id: str, content: str, *, scope_id: str = "p1"
) -> MemoryEntry:
    """A detached entry with a chosen id."""
    return MemoryEntry.create(
        content=content,
        memory_type=MemoryType.SEMANTIC,
        scope_type=MemoryScope.PROJECT,
        scope_id=scope_id,
        provenance=MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER
        ),
    ).model_copy(update={"memory_id": memory_id})


def _orchestrator(
    gateway: _StageGateway,
    *,
    graph_search: Any = None,
    max_hops: int = 3,
    timeout_seconds: float = 10.0,
    graph_limit: int = 10,
) -> AgenticRAGOrchestrator:
    rewriter = QueryRewriter(gateway, provider_id="p", model_id="m")
    synthesizer = EvidenceSynthesizer(
        gateway, provider_id="p", model_id="m"
    )
    retriever = MultiHopRetriever(
        rewriter, synthesizer, max_hops=max_hops
    )
    return AgenticRAGOrchestrator(
        rewriter,
        retriever,
        synthesizer,
        graph_search,
        max_hops=max_hops,
        timeout_seconds=timeout_seconds,
        graph_limit=graph_limit,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "e2e.db"
    conn = sqlite3.connect(str(path))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return path


@pytest_asyncio.fixture
async def repo(db_path: Path):
    repository = SqliteMemoryRepository(db_path)
    try:
        yield repository
    finally:
        repository.close_all()


@pytest_asyncio.fixture
async def store(repo: SqliteMemoryRepository):
    return GraphStore(repo)


# ---------------------------------------------------------------------------
# 1. Simple question
# ---------------------------------------------------------------------------


async def test_simple_question_rewrites_to_one_query() -> None:
    """An already-simple question stays a single sub-query."""
    hit = _entry("mem_a", "Coffee is preferred black.")
    gateway = _StageGateway(
        queries=["what coffee does the user prefer"],
        answer="Black coffee.",
        confidence=0.9,
    )
    orchestrator = _orchestrator(gateway)
    backend = _KeywordBackend([hit])

    result = await orchestrator.retrieve(
        "what coffee does the user prefer?",
        ["project:p1"],
        backend=backend,
    )

    assert result.hops_used == 1
    assert result.sub_queries_used == (
        "what coffee does the user prefer",
    )
    assert result.memory_ids == ["mem_a"]
    assert result.synthesized_answer is not None
    assert result.synthesized_answer.answer == "Black coffee."
    # Exactly three calls: 1 rewrite + 1 gap analysis + 1 synthesis.
    # The gap analysis still runs after hop 1 (the budget has room
    # for more hops); it returns needed=False, which stops the loop.
    assert len(gateway.calls) == 3
    assert _StageGateway.REWRITE_MARKER in gateway.calls[0]["user_prompt"]
    assert _StageGateway.GAP_MARKER in gateway.calls[1]["user_prompt"]
    assert _StageGateway.SYNTH_MARKER in gateway.calls[2]["user_prompt"]


# ---------------------------------------------------------------------------
# 2. The canonical multi-hop question
# ---------------------------------------------------------------------------


async def test_manager_project_question_returns_answer() -> None:
    """'what did my manager say about the project?' spans two hops.

    Hop 1 resolves WHO the manager is; the gap analysis then asks
    what that person said about the project, which hop 2 answers.
    """
    manager = _entry("mem_who", "Ahmed is my manager.")
    statement = _entry(
        "mem_said", "Ahmed said the Peldrun release slips to March."
    )
    backend = _KeywordBackend([manager, statement])

    gateway = _StageGateway(
        queries=["who is my manager"],
        needed=True,
        next_queries=["what did ahmed say about the project"],
        answer="Ahmed said the release slips to March.",
        confidence=0.85,
        required_evidence="slips to March",
    )
    orchestrator = _orchestrator(gateway)

    result = await orchestrator.retrieve(
        "what did my manager say about the project?",
        ["project:p1"],
        backend=backend,
    )

    assert result.hops_used == 2
    assert result.memory_ids == ["mem_who", "mem_said"]
    assert result.synthesized_answer is not None
    assert "March" in result.synthesized_answer.answer
    assert backend.queries == [
        "who is my manager",
        "what did ahmed say about the project",
    ]


# ---------------------------------------------------------------------------
# 3 + 7. Graph-provided indirect memories
# ---------------------------------------------------------------------------


async def test_entity_link_question_finds_indirect_memory() -> None:
    """An entity link surfaces a memory no keyword search would find."""
    indirect = _entry("mem_deadline", "The deadline moved to March.")
    graph = _FakeGraphSearch([(indirect, 0.5)])
    gateway = _StageGateway(
        queries=["schedule question"],
        answer="The deadline moved to March.",
        confidence=0.8,
        required_evidence="deadline moved",
    )
    orchestrator = _orchestrator(gateway, graph_search=graph)

    # The keyword backend knows NOTHING -- only the graph does.
    backend = _KeywordBackend([])
    result = await orchestrator.retrieve(
        "what did Ahmed say about the schedule?",
        ["project:p1"],
        backend=backend,
    )

    assert graph.calls == 1
    assert "mem_deadline" in result.memory_ids
    assert result.synthesized_answer is not None
    assert "March" in result.synthesized_answer.answer


async def test_graph_search_returns_indirect_memories(
    repo: SqliteMemoryRepository, store: GraphStore
) -> None:
    """A real GraphStore + GraphSearch feeds the orchestrator."""
    ahmed = await store.add_entity(Entity.build("PERSON", "Ahmed"))
    acme = await store.add_entity(Entity.build("ORG", "AcmeCorp"))

    direct = await repo.create(
        MemoryEntry.create(
            content="Ahmed is my manager at AcmeCorp.",
            memory_type=MemoryType.SEMANTIC,
            scope_type=MemoryScope.PROJECT,
            scope_id="p1",
            provenance=MemoryProvenance(
                source_type=MemorySourceType.EXPLICIT_USER
            ),
        )
    )
    two_hop = await repo.create(
        MemoryEntry.create(
            content="The Q3 budget was approved.",
            memory_type=MemoryType.SEMANTIC,
            scope_type=MemoryScope.PROJECT,
            scope_id="p1",
            provenance=MemoryProvenance(
                source_type=MemorySourceType.EXPLICIT_USER
            ),
        )
    )
    await store.link_memory_to_entity(direct.memory_id, ahmed.entity_id)
    await store.link_memory_to_entity(two_hop.memory_id, acme.entity_id)
    await store.add_relation(
        Relation.build(ahmed.entity_id, acme.entity_id, "works_at")
    )

    searcher = GraphSearch(store, max_depth=2)
    gateway = _StageGateway(
        queries=["budget"],
        answer="It was approved.",
        confidence=0.7,
        required_evidence="Q3 budget",
    )
    orchestrator = _orchestrator(gateway, graph_search=searcher)

    # Keyword backend has only the direct memory.
    backend = _KeywordBackend([direct])
    result = await orchestrator.retrieve(
        "what is the budget status for Ahmed's company?",
        ["project:p1"],
        backend=backend,
    )

    ids = result.memory_ids
    # Two-hop graph evidence arrived despite no keyword overlap.
    assert two_hop.memory_id in ids
    assert result.synthesized_answer is not None
    assert result.synthesized_answer.answer == "It was approved."


# ---------------------------------------------------------------------------
# 4. Hop ceiling
# ---------------------------------------------------------------------------


async def test_multi_hop_stops_at_max_hops() -> None:
    """The orchestrator never exceeds its hop budget."""
    corpus = [_entry(f"mem_{i}", f"fact number {i}") for i in range(6)]
    backend = _KeywordBackend(corpus)

    class _AlwaysNeedy:
        def __init__(self) -> None:
            self.n = 0

        async def generate_structured(self, **kwargs):
            self.n += 1
            prompt = kwargs["user_prompt"]
            if "Decompose" in prompt:
                return _Resp({"rewritten_queries": ["fact number 0"]})
            if "Decide whether" in prompt:
                return _Resp(
                    {
                        "needed": True,
                        "next_queries": [f"fact number {self.n}"],
                    }
                )
            return _Resp({"answer": "done", "confidence": 0.5})

    script = _AlwaysNeedy()
    orchestrator = _orchestrator(script, max_hops=2)  # type: ignore[arg-type]
    result = await orchestrator.retrieve(
        "q?", ["project:p1"], backend=backend
    )
    assert result.hops_used == 2
    assert len(backend.queries) == 2


# ---------------------------------------------------------------------------
# 5. Timeout -> partial
# ---------------------------------------------------------------------------


async def test_agentic_timeout_returns_partial() -> None:
    """A slowing hop phase still returns the graph's evidence."""
    graph_hit = _entry("mem_graph", "Graph found this.")
    graph = _FakeGraphSearch([(graph_hit, 1.0)])

    gateway = _StageGateway(
        queries=["q"],
        answer="should not be produced",
        confidence=0.9,
        delay=0.0,
    )
    orchestrator = _orchestrator(
        gateway,
        graph_search=graph,
        timeout_seconds=0.15,
    )

    # Every search takes far longer than the run budget.
    slow_backend = _KeywordBackend(
        [_entry("mem_kw", "keyword")], delay=1.0
    )
    result = await orchestrator.retrieve(
        "what did ahmed say?", ["project:p1"], backend=slow_backend
    )

    assert result.synthesized_answer is None
    assert result.hops_used == 0
    assert [e.memory_id for e, _ in result.evidence] == ["mem_graph"]


async def test_orchestrator_survives_backend_failure() -> None:
    """A failing search backend yields evidence, never an exception."""
    graph_hit = _entry("mem_graph", "Graph found this.")
    graph = _FakeGraphSearch([(graph_hit, 1.0)])
    gateway = _StageGateway(queries=["q"])
    orchestrator = _orchestrator(gateway, graph_search=graph)

    result = await orchestrator.retrieve(
        "q?",
        ["project:p1"],
        backend=_KeywordBackend([], fail_all=True),
    )
    # The hop raised, so only graph evidence remains; no synth call
    # could produce an answer from it in this run.
    assert [e.memory_id for e, _ in result.evidence] == ["mem_graph"]


async def test_orchestrator_survives_graph_failure() -> None:
    """A failing graph phase is skipped; the hops still run."""
    hit = _entry("mem_a", "keyword hit")
    graph = _FakeGraphSearch(error=RuntimeError("graph down"))
    gateway = _StageGateway(
        queries=["keyword"], answer="ok", confidence=0.8
    )
    orchestrator = _orchestrator(gateway, graph_search=graph)

    result = await orchestrator.retrieve(
        "q?", ["project:p1"], backend=_KeywordBackend([hit])
    )
    assert graph.calls == 1
    assert result.memory_ids == ["mem_a"]
    assert result.synthesized_answer is not None


# ---------------------------------------------------------------------------
# 6. Service fallback
# ---------------------------------------------------------------------------


async def _service(tmp_path: Path, *, agentic: bool, graph: bool):
    from omweb.memory.manager.memory_service import MemoryService
    from omweb.memory.manager.memory_settings_manager import (
        MemorySettingsManager,
    )

    base = default_settings()
    ai = base.ai.model_copy(
        update={
            "enabled": True,
            "agentic": base.ai.agentic.model_copy(
                update={
                    "provider_id": "mock",
                    "model_id": "mock-m",
                    "timeout_seconds": 10,
                }
            ),
        }
    )
    retrieval = base.retrieval.model_copy(
        update={
            "agentic_enabled": agentic,
            "graph_search_enabled": graph,
        }
    )
    settings = base.model_copy(
        update={
            "mode": MemoryMode.READ_WRITE,
            "ai": ai,
            "retrieval": retrieval,
        }
    )
    settings_path = tmp_path / "s.json"
    await MemorySettingsManager(path=settings_path).save(settings)

    gateway = _StageGateway(
        queries=["ahmed"], answer="Ahmed.", confidence=0.9
    )

    async def transport(**kwargs):
        return await gateway.generate_structured(**kwargs)

    service = MemoryService(
        db_path=tmp_path / "m.db",
        settings_path=settings_path,
        transport_provider=lambda: transport,
    )
    await service.ainitialize()
    # Attach for assertions: the service wraps the transport, so the
    # scripted gateway is not reachable from the pipeline objects.
    service._test_gateway = gateway  # type: ignore[attr-defined]
    return service


async def test_agentic_falls_back_when_disabled(
    tmp_path: Path,
) -> None:
    """Disabled agentic returns the standard context, not an error."""
    service = await _service(tmp_path, agentic=False, graph=False)
    try:
        result = await service.retrieve_agentic(
            query="what did my manager say?",
            chat_id=None,
            project_id="p1",
        )
        assert isinstance(result, AgenticRetrievalResult)
        assert result.is_available is False
        assert result.search_mode == "agentic+fallback"
        assert result.error == "disabled"
        assert result.fallback_context is not None
    finally:
        await service.aclose()


# ---------------------------------------------------------------------------
# 8. Full pipeline integration
# ---------------------------------------------------------------------------


async def test_full_pipeline_integration(tmp_path: Path) -> None:
    """Agentic + graph wired into the real service end to end."""
    service = await _service(tmp_path, agentic=True, graph=True)
    try:
        assert service.agentic_enabled is True
        assert service.agentic_orchestrator is not None
        assert service.agentic_graph_enabled is True

        # Populate the graph directly (deterministic; no LLM).
        store = service.graph_store
        assert store is not None
        ahmed = await store.add_entity(Entity.build("PERSON", "Ahmed"))
        entry = await service.provider.create(
            MemoryEntry.create(
                content="Ahmed is my manager and he approved the plan.",
                memory_type=MemoryType.SEMANTIC,
                scope_type=MemoryScope.PROJECT,
                scope_id="p1",
                provenance=MemoryProvenance(
                    source_type=MemorySourceType.EXPLICIT_USER
                ),
            )
        )
        await store.link_memory_to_entity(entry.memory_id, ahmed.entity_id)

        result = await service.retrieve_agentic(
            query="what did Ahmed do about the plan?",
            chat_id=None,
            project_id="p1",
        )

        assert result.is_available is True
        assert result.search_mode == "agentic"
        assert result.fallback_context is None
        assert result.hops_used >= 1
        assert result.elapsed_ms >= 0
        # The graph link made the memory reachable.
        assert entry.memory_id in result.memory_ids
        assert result.synthesized_answer is not None
        assert result.synthesized_answer.answer == "Ahmed."
        assert result.confidence_threshold_met is True
    finally:
        await service.aclose()


# ---------------------------------------------------------------------------
# 9. Confidence threshold
# ---------------------------------------------------------------------------


async def test_confidence_below_threshold_flagged(
    tmp_path: Path,
) -> None:
    """A low-confidence answer is RETURNED, but flagged."""
    service = await _service(tmp_path, agentic=True, graph=False)
    try:
        # Patch the synthesizer's gateway response to be uncertain.
        from peldrun.memory.agentic.evidence_synthesizer import (
            EvidenceSynthesizer,
        )

        uncertain = _StageGateway(
            queries=["ahmed"], answer="Maybe Ahmed.", confidence=0.2
        )
        service._agentic_synthesizer._gateway = uncertain  # type: ignore[attr-defined]
        service._agentic_retriever._synthesizer = (  # type: ignore[attr-defined]
            service._agentic_synthesizer
        )
        service._agentic_orchestrator._synthesizer = (  # type: ignore[attr-defined]
            service._agentic_synthesizer
        )

        await service.provider.create(
            MemoryEntry.create(
                content="Ahmed might be the manager.",
                memory_type=MemoryType.SEMANTIC,
                scope_type=MemoryScope.PROJECT,
                scope_id="p1",
                provenance=MemoryProvenance(
                    source_type=MemorySourceType.EXPLICIT_USER
                ),
            )
        )

        result = await service.retrieve_agentic(
            query="who is ahmed?",
            chat_id=None,
            project_id="p1",
        )
        # The answer is not discarded -- only labelled.
        assert result.is_available is True
        assert result.confidence == pytest.approx(0.2)
        assert result.confidence_threshold_met is False
        assert result.synthesized_answer is not None
        assert result.synthesized_answer.answer == "Maybe Ahmed."
    finally:
        await service.aclose()


# ---------------------------------------------------------------------------
# 10. retrieve() performance
# ---------------------------------------------------------------------------


async def test_original_retrieve_still_fast(tmp_path: Path) -> None:
    """``retrieve`` stays millisecond-scale with Phase 6 fully on."""
    service = await _service(tmp_path, agentic=True, graph=True)
    try:
        for index in range(20):
            await service.provider.create(
                MemoryEntry.create(
                    content=(
                        f"vector search note {index} about Ahmed"
                    ),
                    memory_type=MemoryType.SEMANTIC,
                    scope_type=MemoryScope.PROJECT,
                    scope_id="p1",
                    provenance=MemoryProvenance(
                        source_type=MemorySourceType.EXPLICIT_USER
                    ),
                )
            )

        # Warm the connection/schema so the measurement excludes
        # first-call setup (which is a startup cost, not a per-turn
        # cost).
        await service.retrieve(
            query="remember the vector search note",
            chat_id=None,
            project_id="p1",
        )

        durations: List[float] = []
        for _ in range(5):
            started = time.perf_counter()
            await service.retrieve(
                query="remember the vector search note",
                chat_id=None,
                project_id="p1",
            )
            durations.append(time.perf_counter() - started)

        best = min(durations)
        # The plan's target is ~500ms; <100ms is the 6D criterion.
        assert best < 0.1, f"retrieve() took {best * 1000:.0f} ms"
        # Nothing on the fast path consulted the LLM.
        assert service._test_gateway.calls == []  # type: ignore[attr-defined]
    finally:
        await service.aclose()


# ---------------------------------------------------------------------------
# Acceptance metric: multi-hop accuracy on 10 questions
# ---------------------------------------------------------------------------


_CORPUS: Tuple[Tuple[str, str], ...] = (
    (
        "mem_manager",
        "Ahmed is my manager at AcmeCorp and he reports to Sara.",
    ),
    (
        "mem_release",
        "Ahmed said the Peldrun Memory release slips to March.",
    ),
    (
        "mem_recall",
        "Peldrun Memory uses vector search for recall.",
    ),
    (
        "mem_updates",
        "My manager prefers written updates every Friday.",
    ),
    (
        "mem_budget",
        "AcmeCorp approved the Q3 budget for the platform team.",
    ),
    (
        "mem_role",
        "The user works as a programmer on backend systems.",
    ),
    (
        "mem_product",
        "Peldrun Memory is a knowledge platform for agents.",
    ),
    (
        "mem_team",
        "Ahmed manages the platform team of five engineers.",
    ),
    (
        "mem_tools",
        "The team uses Python and SQLite for persistence.",
    ),
    (
        "mem_deadlines",
        "Deadlines are tracked in the shared calendar.",
    ),
)


_BENCHMARK: Tuple[Tuple[str, str, str, str], ...] = (
    # (question, rewrite query, required evidence, answer)
    (
        "who is my manager?",
        "who is my manager",
        "Ahmed is my manager",
        "Ahmed is your manager.",
    ),
    (
        "what did my manager say about the release?",
        "release slips March",
        "release slips to March",
        "It slips to March.",
    ),
    (
        "what does Peldrun Memory use for recall?",
        "Peldrun Memory vector search",
        "vector search for recall",
        "Vector search.",
    ),
    (
        "how often does my manager want updates?",
        "manager written updates Friday",
        "updates every Friday",
        "Every Friday.",
    ),
    (
        "was the budget approved?",
        "AcmeCorp Q3 budget approved",
        "approved the Q3 budget",
        "Yes, it was approved.",
    ),
    (
        "what is my job?",
        "user works as programmer",
        "works as a programmer",
        "A programmer.",
    ),
    (
        "what is Peldrun Memory?",
        "Peldrun Memory knowledge platform",
        "knowledge platform for agents",
        "A knowledge platform for agents.",
    ),
    (
        "who manages the platform team?",
        "platform team manages",
        "manages the platform team",
        "Ahmed manages it.",
    ),
    (
        "what tools does the team use?",
        "team uses Python SQLite",
        "uses Python and SQLite",
        "Python and SQLite.",
    ),
    (
        "where are deadlines tracked?",
        "deadlines tracked shared calendar",
        "tracked in the shared calendar",
        "In the shared calendar.",
    ),
)


async def test_multi_hop_accuracy_over_ten_questions() -> None:
    """The plan's acceptance metric: accuracy >= 0.7 over 10 questions.

    Each scripted synthesis returns its answer ONLY when the
    retrieved evidence contains the fact the answer depends on.
    Accuracy therefore measures whether the pipeline delivered the
    right evidence -- the property Phase 6 implements.
    """
    corpus = [_entry(memory_id, content) for memory_id, content in _CORPUS]
    backend = _KeywordBackend(corpus)

    correct = 0
    failures: List[str] = []

    for question, rewrite_query, required, answer in _BENCHMARK:
        gateway = _StageGateway(
            queries=[rewrite_query],
            answer=answer,
            confidence=0.85,
            required_evidence=required,
        )
        orchestrator = _orchestrator(gateway)
        result = await orchestrator.retrieve(
            question, ["project:p1"], backend=backend
        )
        synthesized = (
            result.synthesized_answer.answer
            if result.synthesized_answer is not None
            else ""
        )
        if synthesized == answer:
            correct += 1
        else:
            failures.append(
                f"{question!r}: got {synthesized!r}, "
                f"evidence={result.memory_ids}"
            )

    accuracy = correct / len(_BENCHMARK)
    assert accuracy >= 0.7, (
        f"multi-hop accuracy {accuracy:.2f} < 0.70; "
        f"failures: {failures}"
    )
    assert correct == len(_BENCHMARK), (
        f"expected all benchmark questions to pass, "
        f"failures: {failures}"
    )


async def test_enabling_agentic_via_settings_reload_builds_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: enabling agentic must NOT require a restart.

    The bug this pins down
    ----------------------
    ``_initialize_agentic`` used to run only from ``ainitialize``.
    Saving the model through the settings UI goes
    PATCH /settings -> ``MemoryService.reload_settings()``, which
    re-evaluated extraction, embedding, and the graph -- but not the
    agentic pipeline. So ``_agentic_orchestrator`` stayed ``None``
    while ``retrieval.agentic_enabled`` was already ``True``, and
    ``/agentic/status`` reported ``configured: false`` forever, on
    every save, until the process was restarted.

    The user-visible symptom was a settings banner telling them to
    "save a model" immediately after they had saved one.

    What this asserts
    -----------------
      1. A service started with agentic OFF reports unavailable.
      2. Writing new settings to disk + ``reload_settings()`` (the
         exact sequence the PATCH endpoint performs) builds the
         pipeline in-process.
      3. ``/agentic/status`` agrees: available, configured, and
         `model_resolved` all flip without a restart.
      4. Turning it back off tears the pipeline down again.
    """
    from omweb.memory.api import memory_agentic as agentic_api
    from omweb.memory.manager.memory_service import MemoryService
    from omweb.memory.manager.memory_settings_manager import (
        MemorySettingsManager,
    )

    settings_path = tmp_path / "s.json"
    db_path = tmp_path / "m.db"
    manager = MemorySettingsManager(path=settings_path)

    def _settings(*, agentic_on: bool):
        base = default_settings()
        ai = base.ai.model_copy(
            update={
                "enabled": True,
                "agentic": base.ai.agentic.model_copy(
                    update={
                        "provider_id": "mock",
                        "model_id": "mock-m",
                        "timeout_seconds": 10,
                    }
                ),
            }
        )
        retrieval = base.retrieval.model_copy(
            update={"agentic_enabled": agentic_on}
        )
        return base.model_copy(
            update={
                "mode": MemoryMode.READ_WRITE,
                "ai": ai,
                "retrieval": retrieval,
            }
        )

    async def transport(**kwargs):
        return _Resp({"answer": "", "confidence": 0.0})

    # ---- 1. Boot with agentic OFF ------------------------------
    await manager.save(_settings(agentic_on=False))
    service = MemoryService(
        db_path=db_path,
        settings_path=settings_path,
        transport_provider=lambda: transport,
    )
    await service.ainitialize()
    try:
        assert service.agentic_enabled is False
        assert service.agentic_orchestrator is None

        # ---- 2. Enable it exactly like the PATCH endpoint -------
        await manager.save(_settings(agentic_on=True))
        reloaded = await service.reload_settings()
        assert reloaded.retrieval.agentic_enabled is True

        # The pipeline must exist WITHOUT a restart.
        assert service.agentic_enabled is True, (
            "reload_settings did not build the agentic pipeline; "
            "the UI would keep reporting 'configured: false' after "
            "a successful save."
        )
        assert service.agentic_orchestrator is not None
        assert service.agentic_retriever is not None

        # ---- 3. /agentic/status agrees --------------------------
        # The endpoint resolves the process-wide singleton; point it
        # at this isolated service.
        monkeypatch.setattr(
            "omweb.memory.get_memory_service", lambda: service
        )
        response = await agentic_api.agentic_status_endpoint()
        payload = json.loads(response.body)
        assert payload["enabled"] is True
        assert payload["configured"] is True
        assert payload["available"] is True
        assert payload["ai_enabled"] is True
        assert payload["model_resolved"] is True

        # ---- 4. Turning it off tears down ----------------------
        await manager.save(_settings(agentic_on=False))
        await service.reload_settings()
        assert service.agentic_enabled is False
        assert service.agentic_orchestrator is None
    finally:
        await service.aclose()


async def test_agentic_status_distinguishes_ai_off_from_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: `configured: false` has two causes, reported apart.

    With ``ai.enabled == False`` no model can ever resolve, so a
    status payload that only said "configured: false" forced the UI
    to guess -- and it guessed "save a model", which was wrong and
    unactionable.
    """
    from omweb.memory.api import memory_agentic as agentic_api
    from omweb.memory.manager.memory_service import MemoryService
    from omweb.memory.manager.memory_settings_manager import (
        MemorySettingsManager,
    )

    base = default_settings()
    ai = base.ai.model_copy(
        update={
            "enabled": False,
            "agentic": base.ai.agentic.model_copy(
                update={"provider_id": None, "model_id": None}
            ),
        }
    )
    retrieval = base.retrieval.model_copy(
        update={"agentic_enabled": True}
    )
    settings = base.model_copy(
        update={
            "mode": MemoryMode.READ_WRITE,
            "ai": ai,
            "retrieval": retrieval,
        }
    )

    settings_path = tmp_path / "s.json"
    await MemorySettingsManager(path=settings_path).save(settings)
    service = MemoryService(
        db_path=tmp_path / "m.db", settings_path=settings_path
    )
    await service.ainitialize()
    try:
        assert service.agentic_enabled is False
        monkeypatch.setattr(
            "omweb.memory.get_memory_service", lambda: service
        )
        payload = json.loads(
            (await agentic_api.agentic_status_endpoint()).body
        )
        assert payload["enabled"] is True
        assert payload["configured"] is False
        assert payload["available"] is False
        # The distinguishing signals:
        assert payload["ai_enabled"] is False
    finally:
        await service.aclose()


async def test_documented_defaults() -> None:
    gateway = _StageGateway(queries=["q"])
    orchestrator = _orchestrator(gateway)
    assert orchestrator.timeout_seconds == 10.0
    assert orchestrator.max_hops == 3
    assert orchestrator.graph_enabled is False
    assert DEFAULT_TIMEOUT_SECONDS == 30.0
    assert "graph=off" in repr(orchestrator)
