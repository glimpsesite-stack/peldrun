# backend/tests/memory/test_phase1b_reasoning_disable.py
"""Verify that reasoning-disablement params reach the wire.

The extraction gateway (``MemoryModelGateway`` with
``force_openai_compat=True``) must:

  1. Call ``OpenAICompatProvider.generate`` with
     ``extra_body`` matching ``MemoryAIExtractionSettings.
     extraction_extra_body``.
  2. Also pass ``reasoning_effort="none"`` so that
     ``AsyncLLMClient.chat_completion`` forwards extra_body
     to the SDK.
  3. Skip the extra_body entirely when the field is empty
     (non-reasoning models).
  4. Route through ``OpenAICompatProvider`` -- never
     ``provider_factory.create``.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from omweb.memory.extraction.gateway import (
    LLMStructuredResponse,
    MemoryModelGateway,
)


class _CapturingProvider:
    """Captures every call to OpenAICompatProvider.generate."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def generate(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        # Return a minimal LLMResponse-like object. The gateway
        # only reads ``.content`` and ``.usage``.
        from peldrun.llm.client import TokenUsage
        from peldrun.llm.client import LLMResponse
        return LLMResponse(
            content='{"candidates": []}',
            usage=TokenUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
            ),
        )

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_reasoning_disabled_via_extra_body():
    """extra_body from settings is forwarded to the provider call."""
    captured = _CapturingProvider()

    with patch(
        "peldrun.llm.providers.openai_compat.OpenAICompatProvider",
        return_value=captured,
    ):
        gateway = MemoryModelGateway(
            force_openai_compat=True,
            extraction_extra_body={
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        # _transport_via_openai_compat uses _resolve_credentials
        # which reads models_vault.json. Pass explicit creds so
        # tests don't depend on disk.
        await gateway._default_transport(
            system_prompt="sys",
            user_prompt="user",
            provider_id="test_provider",
            model_id="test_model",
            api_key="test-key",
            base_url="http://localhost:0000/v1",
            max_output_tokens=800,
            timeout_seconds=10,
            connection_id="test_conn",
        )

    assert len(captured.calls) == 1
    call = captured.calls[0]
    assert call["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert call["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_empty_extra_body_skips_reasoning_kwargs():
    """Empty extra_body -> no reasoning_effort, no extra_body kwargs."""
    captured = _CapturingProvider()

    with patch(
        "peldrun.llm.providers.openai_compat.OpenAICompatProvider",
        return_value=captured,
    ):
        gateway = MemoryModelGateway(
            force_openai_compat=True,
            extraction_extra_body={},
        )
        await gateway._default_transport(
            system_prompt="sys",
            user_prompt="user",
            provider_id="test_provider",
            model_id="test_model",
            api_key="test-key",
            base_url="http://localhost:0000/v1",
            max_output_tokens=800,
            timeout_seconds=10,
            connection_id="test_conn",
        )

    assert len(captured.calls) == 1
    call = captured.calls[0]
    assert "extra_body" not in call
    assert "reasoning_effort" not in call
