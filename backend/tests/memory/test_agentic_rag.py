# backend/tests/memory/test_agentic_rag.py
"""
Phase 6-C acceptance tests: agentic RAG core.

Covers the five required 6C tests plus the guarantees the sub-phase
promises:

    Required
    --------
    1. ``test_query_rewriter_produces_sub_queries``
    2. ``test_query_rewriter_falls_back_on_llm_failure``
    3. ``test_evidence_synthesizer_returns_answer``
    4. ``test_multi_hop_stops_when_no_gaps``
    5. ``test_retrieve_agentic_falls_back_when_disabled``

    Supporting
    ----------
    * confidence normalization (percentage scale, rounding slop)
    * citations are range-checked against the rendered evidence
    * the multi-hop loop deduplicates evidence across hops
    * the loop stops on ``max_hops``, on repeated gap queries, and on
      a hop that finds nothing new
    * ``search_ranked`` failures on one sub-query do not abort the hop
    * ``retrieve_agentic`` never touches ``retrieve``'s behaviour
    * the HTTP endpoint returns a well-formed payload on both the
      agentic and the fallback path

No real LLM is involved: the gateway is a scripted double, which is
what makes the loop's control flow assertable exactly rather than
probabilistically.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
import pytest_asyncio

from peldrun.memory.agentic import (
    AgenticRetrievalResult,
    EvidenceSynthesizer,
    GapAnalysis,
    MultiHopRetriever,
    QueryRewriter,
    SynthesizedAnswer,
)
from peldrun.memory.agentic.contracts import coerce_confidence
from peldrun.memory.domain import (
    MemoryEntry,
    MemoryMode,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
    default_settings,
)
from peldrun.memory.llm_contracts import (
    call_structured,
    extract_parsed_object,
)


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


class _ScriptedGateway:
    """Returns queued payloads in order; raises queued errors.

    ``payloads`` may contain dicts (parsed responses) or exception
    instances. Once the queue is exhausted the last entry repeats,
    which keeps multi-hop tests terse.
    """

    def __init__(self, payloads: Sequence[Any]) -> None:
        self._payloads = list(payloads)
        self.calls: List[Dict[str, Any]] = []

    async def generate_structured(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._payloads:
            return _Resp({})
        item = (
            self._payloads.pop(0)
            if len(self._payloads) > 1
            else self._payloads[0]
        )
        if isinstance(item, BaseException):
            raise item
        return _Resp(item)


class _FakeBackend:
    """``MemorySearchProtocol`` double keyed by sub-query."""

    def __init__(
        self,
        results: Optional[
            Dict[str, List[Tuple[MemoryEntry, float]]]
        ] = None,
        *,
        default: Optional[List[Tuple[MemoryEntry, float]]] = None,
        error_for: Optional[Sequence[str]] = None,
    ) -> None:
        self.results = results or {}
        self.default = default or []
        self.error_for = set(error_for or ())
        self.queries: List[str] = []

    async def search_ranked(
        self, query, scope_refs, limit=5, **filters
    ):
        self.queries.append(query)
        if query in self.error_for:
            raise RuntimeError(f"search failed for {query!r}")
        hits = self.results.get(query, self.default)
        return list(hits[:limit])


def _entry(memory_id: str, content: str = "x") -> MemoryEntry:
    """A detached entry with a chosen id (no database needed)."""
    return MemoryEntry.create(
        content=content,
        memory_type=MemoryType.SEMANTIC,
        scope_type=MemoryScope.PROJECT,
        scope_id="p1",
        provenance=MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER
        ),
    ).model_copy(update={"memory_id": memory_id})


# ---------------------------------------------------------------------------
# 1. Query rewriting
# ---------------------------------------------------------------------------


async def test_query_rewriter_produces_sub_queries() -> None:
    """A complex question becomes 2-3 standalone sub-queries."""
    gateway = _ScriptedGateway(
        [
            {
                "rewritten_queries": [
                    "who is the user's manager",
                    "what is the user's current project",
                    "manager statements about the project",
                ]
            }
        ]
    )
    rewriter = QueryRewriter(
        gateway, provider_id="p", model_id="m"
    )

    queries = await rewriter.rewrite(
        "what did my manager say about the project?"
    )
    assert queries == [
        "who is the user's manager",
        "what is the user's current project",
        "manager statements about the project",
    ]
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert call["provider_id"] == "p"
    assert call["model_id"] == "m"
    assert "what did my manager say" in call["user_prompt"]


async def test_query_rewriter_bounds_and_dedupes() -> None:
    """At most 3 queries, dups collapsed, blanks dropped."""
    gateway = _ScriptedGateway(
        [
            {
                "rewritten_queries": [
                    "who is ahmed",
                    "WHO is Ahmed",
                    "   ",
                    "what is peldrun",
                    "where does ahmed work",
                    "extra one",
                ]
            }
        ]
    )
    rewriter = QueryRewriter(
        gateway, provider_id="p", model_id="m", max_sub_queries=3
    )
    queries = await rewriter.rewrite("q")
    assert len(queries) == 3
    assert queries[0] == "who is ahmed"
    assert "extra one" not in queries


async def test_query_rewriter_falls_back_on_llm_failure() -> None:
    """Every failure path returns the ORIGINAL query unchanged."""
    original = "what did my manager say about the project?"

    # Transport failure.
    boom = QueryRewriter(
        _ScriptedGateway([RuntimeError("provider down")]),
        provider_id="p",
        model_id="m",
    )
    assert await boom.rewrite(original) == [original]

    # Timeout on the rewriter's own deadline.
    slow = QueryRewriter(
        _ScriptedGateway([{"rewritten_queries": ["x"]}]),
        provider_id="p",
        model_id="m",
        timeout_seconds=0.01,
    )

    class _Slow:
        async def generate_structured(self, **kwargs):
            await asyncio.sleep(1.0)
            return _Resp({"rewritten_queries": ["x"]})

    slow._gateway = _Slow()  # type: ignore[attr-defined]
    assert await slow.rewrite(original) == [original]

    # Malformed payloads.
    for payload in (
        {"rewritten_queries": "not-a-list"},
        {"unexpected": 1},
        {},
        {"rewritten_queries": [1, 2, 3]},
        {"rewritten_queries": []},
    ):
        rewriter = QueryRewriter(
            _ScriptedGateway([payload]),
            provider_id="p",
            model_id="m",
        )
        assert await rewriter.rewrite(original) == [original]

    # No gateway / unresolved model / empty query.
    assert (
        await QueryRewriter(None, provider_id="p", model_id="m").rewrite(
            original
        )
        == [original]
    )
    assert (
        await QueryRewriter(
            _ScriptedGateway([{}]), provider_id=None, model_id=None
        ).rewrite(original)
        == [original]
    )
    assert (
        await QueryRewriter(
            _ScriptedGateway([{}]), provider_id="p", model_id="m"
        ).rewrite("   ")
        == []
    )


# ---------------------------------------------------------------------------
# 2 + 3. Synthesis
# ---------------------------------------------------------------------------


async def test_evidence_synthesizer_returns_answer() -> None:
    """The synthesizer parses the answer, confidence, and citations."""
    evidence = [
        (_entry("mem_a", "The deadline moved to March."), 0.9),
        (_entry("mem_b", "Ahmed owns the release."), 0.7),
    ]
    gateway = _ScriptedGateway(
        [
            {
                "answer": "The deadline moved to March.",
                "confidence": 0.85,
                "citations": [1, 2, 2],
            }
        ]
    )
    synthesizer = EvidenceSynthesizer(
        gateway, provider_id="p", model_id="m"
    )

    answer = await synthesizer.synthesize("when is the deadline?", evidence)
    assert answer.is_substantive is True
    assert answer.answer == "The deadline moved to March."
    assert answer.confidence == pytest.approx(0.85)
    # Duplicate citation collapsed, both in range.
    assert answer.citations == (1, 2)

    prompt = gateway.calls[0]["user_prompt"]
    assert "[1]" in prompt and "[2]" in prompt
    assert "The deadline moved to March." in prompt


async def test_synthesizer_drops_out_of_range_citations() -> None:
    """Citation indices outside the rendered range are discarded."""
    evidence = [(_entry("mem_a", "only one"), 0.5)]
    gateway = _ScriptedGateway(
        [
            {
                "answer": "one thing",
                "confidence": "0.9",
                "citations": [0, 1, 2, 99, "x", True, -1],
            }
        ]
    )
    synthesizer = EvidenceSynthesizer(
        gateway, provider_id="p", model_id="m"
    )
    answer = await synthesizer.synthesize("q?", evidence)
    assert answer.citations == (1,)
    assert answer.confidence == pytest.approx(0.9)


async def test_synthesizer_graceful_on_failure() -> None:
    """Every failure yields the empty, zero-confidence answer."""
    evidence = [(_entry("mem_a", "text"), 0.5)]

    failing = EvidenceSynthesizer(
        _ScriptedGateway([RuntimeError("down")]),
        provider_id="p",
        model_id="m",
    )
    empty = await failing.synthesize("q?", evidence)
    assert empty.answer == ""
    assert empty.confidence == 0.0
    assert empty.is_substantive is False
    assert empty.citations == ()

    # No evidence at all: never calls the LLM.
    gateway = _ScriptedGateway([{"answer": "should not be used"}])
    synthesizer = EvidenceSynthesizer(
        gateway, provider_id="p", model_id="m"
    )
    assert (await synthesizer.synthesize("q?", [])).is_substantive is False
    assert gateway.calls == []

    # Unconfigured: also no call.
    assert (
        await EvidenceSynthesizer(
            gateway, provider_id=None, model_id=None
        ).synthesize("q?", evidence)
    ).is_substantive is False

    # Non-substantive answer text is rejected.
    blank = EvidenceSynthesizer(
        _ScriptedGateway([{"answer": "   ", "confidence": 0.9}]),
        provider_id="p",
        model_id="m",
    )
    assert (await blank.synthesize("q?", evidence)).answer == ""


def test_confidence_normalization() -> None:
    """Percentage and slop values normalize identically everywhere."""
    assert coerce_confidence(0.85) == pytest.approx(0.85)
    assert coerce_confidence("0.9") == pytest.approx(0.9)
    assert coerce_confidence("95%") == pytest.approx(0.95)
    assert coerce_confidence(95) == pytest.approx(0.95)
    assert coerce_confidence(1.04) == 1.0
    assert coerce_confidence(1.999) == 1.0
    assert coerce_confidence(2.0) == pytest.approx(0.02)
    assert coerce_confidence(250) == 1.0
    assert coerce_confidence(-3) == 0.0
    assert coerce_confidence(True) == 0.0
    assert coerce_confidence("nonsense") == 0.0
    assert coerce_confidence(None) == 0.0
    assert coerce_confidence(float("nan")) == 0.0

    # The dataclass applies the same rule.
    assert SynthesizedAnswer(
        answer="x", confidence=95
    ).confidence == pytest.approx(0.95)


async def test_gap_analysis_parsing() -> None:
    """Gap analysis parses ``needed``/``next_queries`` and normalizes."""
    evidence = [(_entry("mem_a", "text"), 0.5)]

    need_more = EvidenceSynthesizer(
        _ScriptedGateway(
            [{"needed": True, "next_queries": ["who is ahmed"]}]
        ),
        provider_id="p",
        model_id="m",
    )
    gaps = await need_more.identify_gaps("q?", evidence)
    assert gaps.needed is True
    assert gaps.should_continue is True
    assert gaps.next_queries == ("who is ahmed",)

    # "needed" with no queries is normalized to False.
    contradiction = EvidenceSynthesizer(
        _ScriptedGateway([{"needed": True, "next_queries": []}]),
        provider_id="p",
        model_id="m",
    )
    gaps2 = await contradiction.identify_gaps("q?", evidence)
    assert gaps2.needed is False
    assert gaps2.should_continue is False

    # Failure -> stop.
    broken = EvidenceSynthesizer(
        _ScriptedGateway([RuntimeError("down")]),
        provider_id="p",
        model_id="m",
    )
    assert (await broken.identify_gaps("q?", evidence)).needed is False


# ---------------------------------------------------------------------------
# 4. Multi-hop loop
# ---------------------------------------------------------------------------


def _retriever(
    payloads: Sequence[Any],
    *,
    max_hops: int = 3,
    evidence_per_hop: int = 5,
) -> MultiHopRetriever:
    gateway = _ScriptedGateway(payloads)
    return MultiHopRetriever(
        QueryRewriter(gateway, provider_id="p", model_id="m"),
        EvidenceSynthesizer(gateway, provider_id="p", model_id="m"),
        max_hops=max_hops,
        evidence_per_hop=evidence_per_hop,
    )


async def test_multi_hop_stops_when_no_gaps() -> None:
    """One hop + ``needed=False`` means exactly one search round."""
    hit = _entry("mem_a", "the answer")
    backend = _FakeBackend(
        {"who is my manager": [(hit, 0.9)]}, default=[]
    )
    retriever = _retriever(
        [
            {"rewritten_queries": ["who is my manager"]},
            {"needed": False, "next_queries": []},
            {"answer": "Your manager is Ahmed.", "confidence": 0.9},
        ]
    )

    result = await retriever.retrieve(
        "who is my manager?", ["project:p1"], backend=backend
    )

    assert result.hops_used == 1
    assert backend.queries == ["who is my manager"]
    assert result.memory_ids == ["mem_a"]
    assert result.synthesized_answer is not None
    assert result.synthesized_answer.answer == "Your manager is Ahmed."


async def test_multi_hop_uses_gap_queries_for_second_hop() -> None:
    """``needed=True`` makes the loop search the suggested queries."""
    first = _entry("mem_a", "manager is Ahmed")
    second = _entry("mem_b", "Ahmed said the project slips")
    backend = _FakeBackend(
        {
            "who is my manager": [(first, 0.9)],
            "what did ahmed say about the project": [(second, 0.8)],
        },
        default=[],
    )
    retriever = _retriever(
        [
            {"rewritten_queries": ["who is my manager"]},
            {
                "needed": True,
                "next_queries": ["what did ahmed say about the project"],
            },
            {"needed": False, "next_queries": []},
            {"answer": "It slips.", "confidence": 0.8},
        ]
    )

    result = await retriever.retrieve(
        "what did my manager say about the project?",
        ["project:p1"],
        backend=backend,
    )

    assert result.hops_used == 2
    assert backend.queries == [
        "who is my manager",
        "what did ahmed say about the project",
    ]
    assert result.memory_ids == ["mem_a", "mem_b"]
    assert len(result.sub_queries_used) == 2


async def test_multi_hop_stops_at_max_hops() -> None:
    """The loop never exceeds ``max_hops`` even if gaps persist."""
    hits = {
        f"q{i}": [(_entry(f"mem_{i}"), 0.9)] for i in range(10)
    }
    backend = _FakeBackend(hits, default=[])

    class _Counter:
        """One shared script across rewriter + synthesizer.

        Both components share the SAME gateway in production, so the
        test must too; separate counters would hand the synthesizer
        the rewriter's first response.
        """

        def __init__(self) -> None:
            self.n = 0

        async def generate_structured(self, **kwargs):
            self.n += 1
            if self.n == 1:
                # Rewrite.
                return _Resp({"rewritten_queries": ["q0"]})
            if self.n < 4:
                # Gap analyses after hops 1 and 2. With
                # max_hops=3 the loop breaks BEFORE asking again.
                return _Resp(
                    {"needed": True, "next_queries": [f"q{self.n - 1}"]}
                )
            # Synthesis.
            return _Resp({"answer": "done", "confidence": 0.5})

    script = _Counter()
    retriever = MultiHopRetriever(
        QueryRewriter(script, provider_id="p", model_id="m"),
        EvidenceSynthesizer(script, provider_id="p", model_id="m"),
        max_hops=3,
    )
    result = await retriever.retrieve(
        "q?", ["project:p1"], backend=backend
    )
    assert result.hops_used == 3
    assert backend.queries == ["q0", "q1", "q2"]
    assert result.synthesized_answer is not None
    assert result.synthesized_answer.answer == "done"


async def test_multi_hop_stops_on_repeated_gap_queries() -> None:
    """Proposing the SAME queries again ends the loop early."""
    backend = _FakeBackend(
        {"q0": [(_entry("mem_a"), 0.9)]}, default=[]
    )

    class _Repeater:
        def __init__(self) -> None:
            self.n = 0

        async def generate_structured(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return _Resp({"rewritten_queries": ["q0"]})
            if self.n < 3:
                return _Resp(
                    {"needed": True, "next_queries": ["q0"]}
                )
            return _Resp({"answer": "a", "confidence": 0.5})

    retriever = MultiHopRetriever(
        QueryRewriter(_Repeater(), provider_id="p", model_id="m"),
        EvidenceSynthesizer(_Repeater(), provider_id="p", model_id="m"),
        max_hops=5,
    )
    result = await retriever.retrieve(
        "q?", ["project:p1"], backend=backend
    )
    # Hop 1 -> gap says "q0" again -> identical set -> stop.
    assert result.hops_used == 1
    assert backend.queries == ["q0"]


async def test_multi_hop_stops_when_hop_finds_nothing_new() -> None:
    """A hop with no NEW evidence ends the loop."""
    hit = _entry("mem_a")
    backend = _FakeBackend(
        {"q0": [(hit, 0.9)], "q1": [(hit, 0.9)]}, default=[]
    )

    class _Script:
        def __init__(self) -> None:
            self.n = 0

        async def generate_structured(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return _Resp({"rewritten_queries": ["q0"]})
            if self.n == 2:
                return _Resp(
                    {"needed": True, "next_queries": ["q1"]}
                )
            return _Resp({"answer": "a", "confidence": 0.5})

    script = _Script()
    retriever = MultiHopRetriever(
        QueryRewriter(script, provider_id="p", model_id="m"),
        EvidenceSynthesizer(script, provider_id="p", model_id="m"),
        max_hops=4,
    )
    result = await retriever.retrieve(
        "q?", ["project:p1"], backend=backend
    )
    # Hop 1 found mem_a; hop 2 searched q1 and found only mem_a
    # again -> no NEW evidence -> stop at 2 hops.
    assert result.hops_used == 2
    assert backend.queries == ["q0", "q1"]
    assert result.memory_ids == ["mem_a"]  # deduped
    assert result.synthesized_answer is not None


async def test_multi_hop_survives_a_failing_subquery() -> None:
    """One raising sub-query must not abort the hop."""
    good = _entry("mem_good")
    backend = _FakeBackend(
        {"q1": [(good, 0.9)]}, default=[], error_for=["q0"]
    )
    retriever = _retriever(
        [
            {"rewritten_queries": ["q0", "q1"]},
            {"needed": False, "next_queries": []},
            {"answer": "a", "confidence": 0.5},
        ]
    )
    result = await retriever.retrieve(
        "q?", ["project:p1"], backend=backend
    )
    assert result.memory_ids == ["mem_good"]
    assert backend.queries == ["q0", "q1"]


async def test_multi_hop_empty_inputs() -> None:
    """Empty query / empty scope / no backend short-circuit cleanly."""
    retriever = _retriever([{"rewritten_queries": ["q"]}])
    backend = _FakeBackend(default=[])

    assert (await retriever.retrieve("", ["project:p1"], backend=backend)
            ).hops_used == 0
    assert (await retriever.retrieve("q?", [], backend=backend)
            ).hops_used == 0
    assert (await retriever.retrieve("q?", ["project:p1"], backend=None)
            ).hops_used == 0
    assert backend.queries == []


# ---------------------------------------------------------------------------
# 5. Service fallback
# ---------------------------------------------------------------------------


async def _service(tmp_path: Path, *, agentic: bool):
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
        update={"agentic_enabled": agentic}
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

    async def transport(**kwargs):
        return _Resp(
            {
                "rewritten_queries": ["who is ahmed"],
                "needed": False,
                "next_queries": [],
                "answer": "",
                "confidence": 0.0,
            }
        )

    service = MemoryService(
        db_path=tmp_path / "m.db",
        settings_path=settings_path,
        transport_provider=lambda: transport,
    )
    await service.ainitialize()
    return service


async def test_retrieve_agentic_falls_back_when_disabled(
    tmp_path: Path,
) -> None:
    """Disabled agentic returns the standard context, never an error."""
    service = await _service(tmp_path, agentic=False)
    try:
        assert service.agentic_enabled is False
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
        assert result.evidence == ()
        assert result.synthesized_answer is None
    finally:
        await service.aclose()


async def test_retrieve_agentic_runs_when_enabled(
    tmp_path: Path,
) -> None:
    """Enabled agentic produces a well-formed result and no fallback."""
    service = await _service(tmp_path, agentic=True)
    try:
        assert service.agentic_enabled is True
        assert service.agentic_retriever is not None
        result = await service.retrieve_agentic(
            query="who is ahmed?",
            chat_id=None,
            project_id="p1",
        )
        assert result.is_available is True
        assert result.search_mode == "agentic"
        assert result.error is None
        assert result.fallback_context is None
        assert result.hops_used >= 1
        assert result.elapsed_ms >= 0
    finally:
        await service.aclose()
        assert service.agentic_enabled is False


async def test_retrieve_unchanged_by_agentic(tmp_path: Path) -> None:
    """``retrieve`` behaves identically with agentic on and off."""
    off = await _service(tmp_path / "off", agentic=False)
    on = await _service(tmp_path / "on", agentic=True)
    try:
        off_ctx = await off.retrieve(
            query="vector search", chat_id=None, project_id="p1"
        )
        on_ctx = await on.retrieve(
            query="vector search", chat_id=None, project_id="p1"
        )
        assert len(on_ctx.memories) == len(off_ctx.memories)
        assert on_ctx.router_decision == off_ctx.router_decision
        assert on_ctx.query == off_ctx.query
    finally:
        await off.aclose()
        await on.aclose()


async def test_retrieve_agentic_requires_initialization() -> None:
    """Calling before ``ainitialize`` raises the documented error."""
    from omweb.memory.manager.memory_service import MemoryService

    service = MemoryService()
    with pytest.raises(RuntimeError):
        await service.retrieve_agentic(
            query="q", chat_id=None, project_id=None
        )


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


async def test_agentic_endpoint_fallback_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint returns 200 with ``fallback`` when agentic is off."""
    from omweb.memory.api import memory_agentic as api

    service = await _service(tmp_path, agentic=False)
    try:
        monkeypatch.setattr(
            "omweb.memory.get_memory_service", lambda: service
        )
        response = await api.search_agentic_endpoint(
            api._AgenticSearchBody(
                query="what did my manager say?", project_id="p1"
            )
        )
        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["ok"] is True
        assert payload["status"] == "ok"
        assert payload["search_mode"] == "agentic+fallback"
        assert payload["is_available"] is False
        assert payload["error"] == "disabled"
        assert payload["synthesized_answer"] == ""
        assert payload["evidence"] == []
        assert payload["fallback"] is not None
        assert "memories" in payload["fallback"]
    finally:
        await service.aclose()


async def test_agentic_endpoint_agentic_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint returns the agentic shape when enabled."""
    from omweb.memory.api import memory_agentic as api

    service = await _service(tmp_path, agentic=True)
    try:
        monkeypatch.setattr(
            "omweb.memory.get_memory_service", lambda: service
        )
        response = await api.search_agentic_endpoint(
            api._AgenticSearchBody(
                query="who is ahmed?", project_id="p1", max_hops=1
            )
        )
        assert response.status_code == 200
        payload = json.loads(response.body)
        assert payload["search_mode"] == "agentic"
        assert payload["is_available"] is True
        assert payload["fallback"] is None
        assert payload["hops_used"] == 1
        assert isinstance(payload["sub_queries_used"], list)
        assert isinstance(payload["evidence"], list)
        assert payload["confidence_threshold_met"] in (True, False)
    finally:
        await service.aclose()


async def test_agentic_status_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status endpoint reports configuration without an LLM call."""
    from omweb.memory.api import memory_agentic as api

    service = await _service(tmp_path, agentic=True)
    try:
        monkeypatch.setattr(
            "omweb.memory.get_memory_service", lambda: service
        )
        response = await api.agentic_status_endpoint()
        payload = json.loads(response.body)
        assert payload["status"] == "ok"
        assert payload["enabled"] is True
        assert payload["configured"] is True
        assert payload["available"] is True
        assert payload["max_hops"] == 3
    finally:
        await service.aclose()
