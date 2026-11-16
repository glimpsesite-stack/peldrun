# backend/check_vector_search.py
"""Smoke test for SqliteVecProvider with in-memory fakes.

Run: python check_vector_search.py
"""
import asyncio
from typing import Dict, List, Tuple

from peldrun.memory.domain.entry import (
    MemoryEntry,
    MemoryProvenance,
)
from peldrun.memory.domain.enums import (
    MemoryScope,
    MemorySourceType,
)
from peldrun.memory.embeddings.contracts import EmbeddingResult
from peldrun.memory.retrieval.vector_search import SqliteVecProvider


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Returns a deterministic 4-dim vector derived from text length."""

    def __init__(self, fail_dims: bool = False):
        self._fail_dims = fail_dims

    async def embed_batch(self, texts, model_id):
        dims = 8 if self._fail_dims else 4
        # Deterministic vector: all components derived from length.
        base = float(len(texts[0]) % 10 + 1) / 10.0
        vec = [base + (i * 0.01) for i in range(dims)]
        return EmbeddingResult(
            vectors=[vec for _ in texts],
            model_id=model_id,
            model_version=model_id,
            input_tokens=len(texts) * 3,
            cost_nano_usd=0,
        )

    @property
    def dimensions(self) -> int:
        return 8 if self._fail_dims else 4

    @property
    def model_version(self) -> str:
        return "test"

    async def close(self) -> None:
        pass


class _FakeVectorBackend:
    """Returns a fixed set of hits, filtered by min_similarity."""

    def __init__(self, hits: List[Tuple[str, float]]):
        self._hits = hits
        self.call_count = 0

    async def search_in_namespace(
        self, *, query_vector, dimension, limit, min_similarity
    ):
        self.call_count += 1
        return [(m, s) for m, s in self._hits if s >= min_similarity]


class _FakeHydrationBackend:
    """Returns MemoryEntry rows for a subset of the requested ids."""

    def __init__(self, entries_by_id: Dict[str, MemoryEntry]):
        self._entries = entries_by_id

    async def get_entries_by_ids(self, memory_ids, scope_refs):
        result = []
        for mid in memory_ids:
            entry = self._entries.get(mid)
            if entry is not None:
                result.append(entry)
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(content: str, scope_id: str = "proj_1") -> MemoryEntry:
    """Build a MemoryEntry with an AUTO-GENERATED memory_id.

    NOTE: MemoryEntry.create() generates its own memory_id
    (``mem_<hex>``); the caller must read ``entry.memory_id``
    AFTER creation. This helper does NOT accept a memory_id
    argument precisely because the caller cannot override the
    one create() produces -- using the auto-generated value
    avoids any drift between what the fakes reference and what
    the entry actually carries.
    """
    return MemoryEntry.create(
        content=content,
        memory_type="semantic",
        scope_type=MemoryScope.PROJECT,
        scope_id=scope_id,
        provenance=MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def _test_happy_path() -> None:
    entry_a = _make_entry("content_a")
    entry_b = _make_entry("content_b")
    # Read the auto-generated ids AFTER creation.
    id_a = entry_a.memory_id
    id_b = entry_b.memory_id

    provider = SqliteVecProvider(
        embedding_provider=_FakeEmbedder(),
        vector_backend=_FakeVectorBackend([
            (id_a, 0.95),
            (id_b, 0.60),
        ]),
        hydration_backend=_FakeHydrationBackend({
            id_a: entry_a,
            id_b: entry_b,
        }),
        model_id="fake-emb",
        model_version="v1",
        dimensions=4,
    )

    results = await provider.search(
        "hello world", ["project:proj_1"], limit=10, min_similarity=0.5
    )
    assert len(results) == 2, f"expected 2 results, got {len(results)}"
    assert results[0][0].memory_id == id_a
    assert results[0][1] == 0.95
    assert results[1][0].memory_id == id_b
    assert results[1][1] == 0.60
    print("OK happy path: 2 hits ordered by similarity")


async def _test_min_similarity_filter() -> None:
    entry_a = _make_entry("content_a")
    entry_b = _make_entry("content_b")
    id_a = entry_a.memory_id
    id_b = entry_b.memory_id

    provider = SqliteVecProvider(
        embedding_provider=_FakeEmbedder(),
        vector_backend=_FakeVectorBackend([
            (id_a, 0.90),
            (id_b, 0.30),
        ]),
        hydration_backend=_FakeHydrationBackend({
            id_a: entry_a,
            id_b: entry_b,
        }),
        model_id="fake-emb",
        model_version="v1",
        dimensions=4,
    )

    results = await provider.search(
        "query", ["project:proj_1"], min_similarity=0.5
    )
    assert len(results) == 1, f"expected 1, got {len(results)}"
    assert results[0][0].memory_id == id_a
    print("OK min_similarity filter: kept only 0.90")


async def _test_hydration_skip_missing() -> None:
    entry_a = _make_entry("content_a")
    id_a = entry_a.memory_id

    provider = SqliteVecProvider(
        embedding_provider=_FakeEmbedder(),
        vector_backend=_FakeVectorBackend([
            (id_a, 0.90),
            ("mem_deleted_abc", 0.80),  # not in hydration backend
        ]),
        hydration_backend=_FakeHydrationBackend({
            id_a: entry_a,
        }),
        model_id="fake-emb",
        model_version="v1",
        dimensions=4,
    )

    results = await provider.search(
        "query", ["project:proj_1"], min_similarity=0.0
    )
    assert len(results) == 1, f"expected 1, got {len(results)}"
    assert results[0][0].memory_id == id_a
    print("OK hydration skip: missing entry dropped silently")


async def _test_empty_query() -> None:
    backend = _FakeVectorBackend([])
    provider = SqliteVecProvider(
        embedding_provider=_FakeEmbedder(),
        vector_backend=backend,
        hydration_backend=_FakeHydrationBackend({}),
        model_id="fake-emb",
        model_version="v1",
        dimensions=4,
    )
    results = await provider.search("   ", ["project:proj_1"])
    assert results == []
    assert backend.call_count == 0, (
        f"backend must not be called, was {backend.call_count}"
    )
    print("OK empty query: no backend call, [] returned")


async def _test_empty_scopes() -> None:
    backend = _FakeVectorBackend([])
    provider = SqliteVecProvider(
        embedding_provider=_FakeEmbedder(),
        vector_backend=backend,
        hydration_backend=_FakeHydrationBackend({}),
        model_id="fake-emb",
        model_version="v1",
        dimensions=4,
    )
    results = await provider.search("hello", [])
    assert results == []
    assert backend.call_count == 0
    print("OK empty scopes: no backend call, [] returned")


async def _test_dimension_mismatch() -> None:
    provider = SqliteVecProvider(
        embedding_provider=_FakeEmbedder(fail_dims=True),  # returns 8
        vector_backend=_FakeVectorBackend([]),
        hydration_backend=_FakeHydrationBackend({}),
        model_id="fake-emb",
        model_version="v1",
        dimensions=4,  # provider expects 4
    )
    try:
        await provider.search("hello", ["project:proj_1"])
        assert False, "expected EmbeddingDimensionMismatchError"
    except Exception as e:
        assert "dimension" in str(e).lower(), (
            f"expected 'dimension' in message, got: {e}"
        )
        print("OK dimension mismatch: raised with message")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _main() -> None:
    await _test_happy_path()
    await _test_min_similarity_filter()
    await _test_hydration_skip_missing()
    await _test_empty_query()
    await _test_empty_scopes()
    await _test_dimension_mismatch()
    print("DONE")


if __name__ == "__main__":
    asyncio.run(_main())
