# backend/tests/memory/test_settings_contract.py
"""Settings contract: a live tuning field must REACH the runtime (C2).

The audit's root cause was that a field NAME appearing in the source
proves nothing: ``rrf_k`` existed in the hybrid provider while the value
from settings was never passed. Substring search therefore cannot gate
this defect. Two stronger checks live here:

  * EFFECT -- build the runtime object from two different settings values
    and require the observable runtime property to differ.
  * WIRING -- parse the actual ``HybridMemoryProvider(...)`` call site with
    ``ast`` and require each keyword's value expression to read from
    ``settings.retrieval``. A hardcoded default (``rrf_k=60``) is rejected.

Phase 4 extends the EFFECTS table as further fields are wired; a field
that is intentionally inert must be listed with a reason, not silently
ignored.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from omweb.memory.manager.hybrid_provider import HybridMemoryProvider
from peldrun.memory.domain.settings import MemorySettings

BACKEND = Path(__file__).resolve().parents[2]

HYBRID_FIELDS = ("rrf_k", "min_vector_similarity", "mode")


class _StubFts:
    """Minimal lexical side: the contract test never searches."""

    is_open = True

    async def search_ranked(self, query, scope_refs, limit=5, **filters):
        return []


class _StubVec:
    """Minimal vector side: the contract test never embeds."""

    async def search(self, **kwargs):
        return []


def _with(**retrieval_overrides: Any) -> MemorySettings:
    base = MemorySettings()
    return base.model_copy(
        update={
            "retrieval": base.retrieval.model_copy(
                update=retrieval_overrides
            )
        }
    )


def _hybrid_from(settings: MemorySettings) -> HybridMemoryProvider:
    """Build the hybrid with the SAME kwargs MemoryService forwards."""
    return HybridMemoryProvider(
        fts_provider=_StubFts(),
        vec_provider=_StubVec(),
        rrf_k=settings.retrieval.rrf_k,
        min_vector_similarity=settings.retrieval.min_vector_similarity,
        mode=settings.retrieval.mode,
    )


def assert_hybrid_forwards_settings() -> None:
    """AST proof that the live call site forwards the settings values."""
    src = (BACKEND / "omweb/memory/manager/memory_service.py").read_text(
        encoding="utf-8"
    )
    calls = [
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "HybridMemoryProvider"
    ]
    assert calls, "HybridMemoryProvider is never constructed in memory_service"
    forwarded = {
        kw.arg: ast.unparse(kw.value)
        for call in calls
        for kw in call.keywords
        if kw.arg
    }
    for field in HYBRID_FIELDS:
        assert field in forwarded, f"{field} not forwarded to the hybrid"
        assert "settings.retrieval" in forwarded[field], (
            f"{field} is not read from settings: {forwarded[field]}"
        )


# (settings key, runtime accessor, two distinct valid values)
EFFECTS: List[Tuple[str, Any, Any, Any]] = [
    ("retrieval.rrf_k", lambda h: h.rrf_k, 30, 90),
    (
        "retrieval.min_vector_similarity",
        lambda h: h.min_vector_similarity,
        0.2,
        0.8,
    ),
    ("retrieval.mode", lambda h: h.mode, "lexical", "hybrid"),
]


@pytest.mark.parametrize(
    "key,read,a,b", EFFECTS, ids=[e[0] for e in EFFECTS]
)
def test_setting_reaches_runtime(key, read, a, b):
    field = key.split(".", 1)[1]
    low = read(_hybrid_from(_with(**{field: a})))
    high = read(_hybrid_from(_with(**{field: b})))
    assert low != high, f"{key} has no effect on the runtime ({low!r})"


def test_hybrid_call_site_forwards_settings():
    assert_hybrid_forwards_settings()
