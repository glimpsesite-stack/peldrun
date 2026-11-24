# backend/tests/memory/test_search_preview_parity.py
"""
The composer's "Similar memories" panel is a PREVIEW of what the model will
receive. It calls ``POST /api/memory/search``; the chat calls
``MemoryService.retrieve`` (``always_retrieve=True``: OR matching, no recency
fallback). If the two disagree, the panel shows memories the model never
sees (and hides ones it does).

Regression for: with ``allow_recency_fallback=True`` and AND matching, every
multi-word draft -- and every greeting -- listed the three NEWEST memories
as "similar", whatever the user typed.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omweb.memory.api import memory_crud
from omweb.memory.api.memory_crud import _SearchBody, search_endpoint
from omweb.memory.storage.normalizer import normalize_for_storage
from peldrun.memory.domain import (
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
)
from peldrun.memory.domain.scope import resolve_scope_refs
from peldrun.memory.domain.settings import MemorySettings
from peldrun.memory.providers.sqlite_fts import SqliteFtsProvider
from peldrun.memory.services.context_builder import build_memory_context
from peldrun.memory.services.router import Route, RouteDecision

# Oldest -> newest; the same memories as in the bug report screenshot.
MEMORIES = [
    "اسمي أحمد وأعمل مهندس برمجيات",
    "انا احب اشرب القهوه",
    "ملف المرفق",
    "احب شرب القهوه السوداء",
]
DRAFTS = [
    "السلام عليكم",
    "thank you",
    "ما هي قهوتي المفضلة؟",
    "هل تحب شرب القهوه السوداء بدون سكر",
    "ماذا احب ان اشرب",
]


@pytest.fixture
async def provider(tmp_path: Path):
    prov = SqliteFtsProvider(db_path=tmp_path / "preview.db")
    await prov.open()
    for text in MEMORIES:
        await prov.create(
            MemoryEntry.create(
                content=text,
                memory_type=MemoryType.SEMANTIC,
                scope_type=MemoryScope.USER,
                scope_id="default",
                provenance=MemoryProvenance(
                    source_type=MemorySourceType.EXPLICIT_USER
                ),
                normalized_content=normalize_for_storage(text),
            )
        )
    yield prov
    await prov.close()


@pytest.fixture
def api(provider, monkeypatch):
    service = SimpleNamespace(
        settings=MemorySettings(), provider=provider, last_search_mode="fts"
    )
    monkeypatch.setattr(memory_crud, "_get_service_or_500", lambda: service)
    return service


async def _preview(query: str, **extra) -> list[str]:
    body = _SearchBody(query=query, chat_id="c1", user_id="default", limit=5, **extra)
    resp = await search_endpoint(body)
    assert resp.status_code == 200
    return [r["content"] for r in json.loads(resp.body)["results"]]


async def _chat_injection(provider, query: str) -> list[str]:
    ctx = await build_memory_context(
        query=query,
        scope_refs=resolve_scope_refs(
            chat_id="c1", project_id=None, user_id="default"
        ),
        route=Route(decision=RouteDecision.DIRECTED, reason="parity"),
        provider=provider,
        settings=MemorySettings().model_copy(update={"default_limit": 5}),
        allow_recency_fallback=False,
        match_any=True,
    )
    return [m.content for m in ctx.memories]


@pytest.mark.parametrize("query", ["السلام عليكم", "thank you"])
async def test_greeting_preview_is_empty(api, query: str) -> None:
    assert await _preview(query) == []


async def test_preview_does_not_list_unrelated_recents(api) -> None:
    got = await _preview("ما هي قهوتي المفضلة؟")
    assert "ملف المرفق" not in got
    assert got, "a real coffee question must still find the coffee memories"


@pytest.mark.parametrize("query", DRAFTS)
async def test_preview_equals_what_the_model_receives(
    api, provider, query: str
) -> None:
    assert sorted(await _preview(query)) == sorted(
        await _chat_injection(provider, query)
    )


async def test_recency_listing_is_still_available_as_explicit_opt_in(api) -> None:
    got = await _preview("السلام عليكم", allow_recency_fallback=True)
    assert len(got) == 3
