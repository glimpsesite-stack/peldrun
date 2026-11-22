# backend/tests/memory/test_stopword_only_query.py
"""
Regression tests for the repository-level handling of EMPTY FTS expressions.

``normalize_query(..., match='any')`` returns an empty expression in two
different situations, and the repository must treat them differently:

1. **No word characters at all** (``''``, whitespace, punctuation only).
   The caller is asking for "recent entries" -- this is what the write
   pipeline's dedupe pool and the UI list rely on. Recents are returned.

2. **Words present, but every one is a stopword** (``'thank you'``,
   ``'hello'``). There is NO retrieval intent. The answer is ZERO
   matches. Returning recents here injected unrelated memories into
   greetings and thank-yous.

``test_always_retrieve.py`` already pins that ``normalize_query`` returns
an empty expression for conversational input; this module pins what the
REPOSITORY does with it, which is where the defect lived.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omweb.memory.storage.migrations import apply_schema
from omweb.memory.storage.normalizer import normalize_query
from omweb.memory.storage.repository import SqliteMemoryRepository
from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
)

SCOPES = ["user:default"]

# Conversational inputs whose every word is a stopword.
STOPWORD_ONLY = ["thank you", "hello", "hi there", "شكرا"]
# Inputs with no word characters at all.
BLANK = ["", "   ", "؟!"]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "stopword_only.db"
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
        provenance=MemoryProvenance(source_type=MemorySourceType.EXPLICIT_USER),
    )


async def _seeded(db_path: Path) -> SqliteMemoryRepository:
    repo = SqliteMemoryRepository(db_path)
    for text in (
        "I prefer dark mode in all editors",
        "The deployment target is Hetzner with Docker Compose",
        "لدي قطة اسمها لولو",
    ):
        await repo.create(_entry(text))
    return repo


@pytest.mark.parametrize("query", STOPWORD_ONLY)
async def test_stopword_only_query_returns_nothing(
    db_path: Path, query: str
) -> None:
    # Precondition: the normalizer really does produce an empty expression,
    # so this test exercises the repository's empty-expression branch.
    assert normalize_query(query, match="any") == ""

    repo = await _seeded(db_path)
    try:
        result = await repo.search_ranked(query, SCOPES, 5, match_any=True)
        assert result == [], f"{query!r} must not inject recent memories"
    finally:
        repo.close_all()


@pytest.mark.parametrize("query", BLANK)
async def test_blank_query_still_lists_recents(
    db_path: Path, query: str
) -> None:
    # Callers such as the dedupe pool and the UI list pass an empty query
    # to mean "the most recent entries". The stopword fix must not break it.
    repo = await _seeded(db_path)
    try:
        result = await repo.search_ranked(query, SCOPES, 5, match_any=True)
        assert len(result) == 3
    finally:
        repo.close_all()


async def test_real_query_is_unaffected(db_path: Path) -> None:
    repo = await _seeded(db_path)
    try:
        result = await repo.search_ranked(
            "which docker setup", SCOPES, 5, match_any=True
        )
        assert [r[0].content for r in result] == [
            "The deployment target is Hetzner with Docker Compose"
        ]
    finally:
        repo.close_all()
