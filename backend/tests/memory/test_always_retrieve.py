# backend/tests/memory/test_always_retrieve.py
"""
Regression tests for Phase 6's "always retrieve" behaviour.

Two independent defects made stored memories invisible during a normal
chat turn, and both are pinned here:

1. **The intent gate.** ``classify_route`` returns ``NO_NEED`` for any
   query that does not look like an explicit recall request, and
   ``build_memory_context`` then returns an empty context WITHOUT
   searching. Its patterns are English-centric, so in an Arabic
   deployment almost every real question was gated out.

2. **Implicit AND in FTS.** ``normalize_query`` produced
   space-separated tokens, which FTS5 reads as AND: every word of a
   natural question had to appear in ONE memory. Arabic made it worse
   -- ``بالقهوه`` is a different token from the stored ``القهوه``.

The tests below assert the fix for each, and that the OLD behaviour is
preserved verbatim when ``always_retrieve`` is switched off (so a
deployment that wants the strict gate keeps it).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio

from omweb.memory.manager.memory_service import MemoryService
from omweb.memory.storage.migrations import apply_schema
from omweb.memory.storage.normalizer import normalize_query
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


# ---------------------------------------------------------------------------
# normalize_query
# ---------------------------------------------------------------------------


def test_normalize_query_legacy_and_mode_unchanged() -> None:
    """The default mode is byte-for-byte the historical output."""
    assert normalize_query("black coffee") == "black coffee"
    assert normalize_query("احب القهوه") == "احب القهوه"
    assert normalize_query("") == ""
    assert normalize_query(None) == ""


def test_normalize_query_any_mode_or_and_prefixes() -> None:
    """``match='any'`` ORs prefix-wildcarded CONTENT words."""
    expression = normalize_query("do I like coffee", match="any")
    # "do" and "i" are function words: OR-ing them would match every
    # memory that merely contains "i", so they are dropped.
    assert expression == "like* OR coffee*"
    assert "do*" not in expression

    # Arabic: the article-prefixed form contributes the stripped
    # variant too, which is what makes بالقهوه reachable.
    arabic = normalize_query("ما رايك بالقهوه", match="any")
    assert "بالقهوه*" in arabic
    assert "قهوه*" in arabic
    assert " OR " in arabic


def test_normalize_query_any_mode_drops_pure_conversation() -> None:
    """A message with no content word yields an empty expression.

    This is the difference between "searched and found nothing" and
    "injected the most recent memories into a thank-you". Empty means
    zero matches, which means nothing reaches the prompt.
    """
    for conversational in (
        "thank you",
        "hello",
        "hi there",
        "شكرا",
    ):
        assert normalize_query(conversational, match="any") == "", conversational

    # A farewell keeps its residual content words ("see", "next",
    # "time"). They are legitimate search terms in other sentences
    # ("what time is my meeting"), so they are NOT stopwords -- the
    # guarantee here is only that such words never make the query
    # match unrelated memories, which is measured against a real
    # database elsewhere. What must hold universally: no function
    # word leaks into the expression.
    farewell = normalize_query("ok i will see you next time", match="any")
    assert "i*" not in farewell
    assert "you*" not in farewell
    assert "ok*" not in farewell

    # But a real question keeps its content words.
    assert normalize_query("what do I drink", match="any") == "drink*"


def test_normalize_query_any_mode_is_injection_safe() -> None:
    """User text never becomes FTS syntax: tokens are ``\\w+`` only."""
    expression = normalize_query(
        'foo OR bar; DROP TABLE -- "x"', match="any"
    )
    assert '"' not in expression
    assert "(" not in expression
    # The only uppercase OR is the joiner this module emits.
    assert expression.upper().count(" OR ") == expression.count(" OR ") - 0
    for forbidden in (";", "--", "DROP TABLE"):
        assert forbidden not in expression


def test_normalize_query_rejects_unknown_mode() -> None:
    """A typo in the operator must fail loudly, not pick a default."""
    with pytest.raises(ValueError):
        normalize_query("x", match="bogus")


# ---------------------------------------------------------------------------
# Repository: AND vs ANY
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "always.db"
    conn = sqlite3.connect(str(path))
    try:
        apply_schema(conn)
    finally:
        conn.close()
    return path


def _entry(content: str) -> MemoryEntry:
    return MemoryEntry.create(
        content=content,
        memory_type=MemoryType.SEMANTIC,
        scope_type=MemoryScope.USER,
        scope_id="default",
        provenance=MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER
        ),
    )


async def test_repository_and_vs_any_matching(db_path: Path) -> None:
    """A natural question matches under ``match_any`` only.

    The stored text contains "coffee" but not "do", "like", or "i",
    so strict AND returns nothing while OR finds it.
    """
    repo = SqliteMemoryRepository(db_path)
    try:
        await repo.create(_entry("I really enjoy drinking black coffee."))
        await repo.create(_entry("احب شرب القهوه السوداء"))

        strict = await repo.search_ranked(
            "do I like coffee", ["user:default"], 5
        )
        assert strict == [], "AND mode should find nothing here"

        loose = await repo.search_ranked(
            "do I like coffee", ["user:default"], 5, match_any=True
        )
        assert len(loose) == 1
        assert "coffee" in loose[0][0].content

        # Arabic with an attached preposition: only reachable via the
        # stripped variant.
        arabic_strict = await repo.search_ranked(
            "بالقهوه", ["user:default"], 5
        )
        assert arabic_strict == []

        arabic_loose = await repo.search_ranked(
            "بالقهوه", ["user:default"], 5, match_any=True
        )
        assert len(arabic_loose) == 1
    finally:
        repo.close_all()


async def test_match_any_still_respects_scope(db_path: Path) -> None:
    """Broadened matching must not widen the scope boundary."""
    repo = SqliteMemoryRepository(db_path)
    try:
        await repo.create(_entry("coffee is my favourite drink"))
        await repo.create(
            MemoryEntry.create(
                content="coffee notes for project alpha",
                memory_type=MemoryType.SEMANTIC,
                scope_type=MemoryScope.PROJECT,
                scope_id="alpha",
                provenance=MemoryProvenance(
                    source_type=MemorySourceType.EXPLICIT_USER
                ),
            )
        )

        hits = await repo.search_ranked(
            "coffee", ["user:default"], 10, match_any=True
        )
        assert len(hits) == 1
        assert hits[0][0].scope_id == "default"
    finally:
        repo.close_all()


# ---------------------------------------------------------------------------
# MemoryService: the gate
# ---------------------------------------------------------------------------


async def _service(tmp_path: Path, *, always_retrieve: bool):
    from omweb.memory.manager.memory_settings_manager import (
        MemorySettingsManager,
    )

    base = default_settings()
    retrieval = base.retrieval.model_copy(
        update={"always_retrieve": always_retrieve}
    )
    settings = base.model_copy(
        update={
            "mode": MemoryMode.READ_WRITE,
            "retrieval": retrieval,
        }
    )
    settings_path = tmp_path / "s.json"
    await MemorySettingsManager(path=settings_path).save(settings)

    service = MemoryService(
        db_path=tmp_path / "m.db", settings_path=settings_path
    )
    await service.ainitialize()
    return service


async def test_always_retrieve_beats_the_intent_gate(
    tmp_path: Path,
) -> None:
    """A plain Arabic question now reaches the model's context."""
    service = await _service(tmp_path, always_retrieve=True)
    try:
        await service.provider.create(_entry("انا احب اشرب القهوه"))

        context = await service.retrieve(
            query="ما رايك بالقهوه",
            chat_id=None,
            project_id=None,
            user_id="default",
        )
        assert len(context.memories) == 1
        # DIRECTED, not during_execution (that is the tool path).
        assert context.router_decision == "directed"
    finally:
        await service.aclose()


async def test_gate_off_preserves_old_no_need_behaviour(
    tmp_path: Path,
) -> None:
    """With the flag off, a non-recall query still retrieves nothing."""
    service = await _service(tmp_path, always_retrieve=False)
    try:
        await service.provider.create(_entry("انا احب اشرب القهوه"))

        gated = await service.retrieve(
            query="ما رايك بالقهوه",
            chat_id=None,
            project_id=None,
            user_id="default",
        )
        assert gated.memories == []
        assert gated.router_decision == "no_need"

        # An explicit recall request still works under the old gate.
        directed = await service.retrieve(
            query="what did we decide about coffee?",
            chat_id=None,
            project_id=None,
            user_id="default",
        )
        assert len(directed.memories) == 1
        assert directed.router_decision == "directed"
    finally:
        await service.aclose()


async def test_always_retrieve_never_widens_scope(tmp_path: Path) -> None:
    """The flag changes WHEN we search, never WHAT is visible."""
    service = await _service(tmp_path, always_retrieve=True)
    try:
        await service.provider.create(_entry("coffee at user scope"))
        context = await service.retrieve(
            query="coffee",
            chat_id=None,
            project_id=None,
            user_id="default",
        )
        assert context.scope_refs == ["user:default"]
    finally:
        await service.aclose()


async def test_always_retrieve_default_is_on() -> None:
    """The shipped default is the behaviour the deployment needs."""
    assert default_settings().retrieval.always_retrieve is True
