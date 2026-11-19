# backend/peldrun/memory/llm_contracts.py
"""
PELDRUN Memory Platform - Shared LLM Gateway Contracts.

The single place where the ``peldrun`` memory core declares the
NARROW slice of an LLM gateway it needs, plus the tolerant response
parser every LLM-backed component shares.

Why this module exists
----------------------

The ``peldrun`` core must never import ``omweb`` (the same rule
``peldrun.memory.domain`` enforces for storage). But nearly every
Phase 5/6 component -- the reranker, the entity extractor, the query
rewriter, the evidence synthesizer -- is LLM-backed and therefore
needs the ``MemoryModelGateway`` that lives in
``omweb.memory.extraction.gateway``.

The resolution used throughout this codebase is a **structural
Protocol**: ``peldrun`` declares the call signature it needs,
``omweb`` implements it, and neither side imports the other. The
precedent is ``RerankGatewayProtocol`` in
``peldrun/memory/retrieval/reranker.py``, which this module
generalizes so that Phase 6's three new components do not each
re-declare the same signature.

Where the concrete implementation satisfies it
----------------------------------------------
``MemoryModelGateway.generate_structured`` (``omweb``) already
matches this Protocol exactly, keyword for keyword. Satisfaction is
verified by Python's structural typing at type-check time and, in
production, by the fact that the object is simply passed in.

Why the return type is ``Any``
------------------------------
The concrete return value (``LLMStructuredResponse``) is an
``omweb`` dataclass. Naming it here would defeat the whole point of
the split. Components consume it through :func:`extract_parsed_object`,
which accepts a dataclass-with-``.parsed``, a bare ``dict``, or a
dict-like object -- so a test mock can return the simplest possible
shape.

Design invariants
-----------------

1. **Zero dependencies.** This module imports only ``typing``. No
   ``omweb``, no ``pydantic``, no logging side effects. It can be
   imported from the earliest bootstrap point.

2. **Tolerant parsing, never raising.** :func:`extract_parsed_object`
   returns ``None`` on anything it cannot understand. Callers map
   ``None`` onto their own documented degradation
   (``[]`` for extraction, ``[query]`` for rewriting, a zero-
   confidence answer for synthesis). A parser that raised would
   force every caller to wrap it in try/except -- exactly the
   boilerplate this helper removes.

3. **Read-only contracts.** No helper here mutates the response.
   ``LLMStructuredResponse`` is frozen on the ``omweb`` side for the
   same reason.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any, Dict, Optional, Protocol, runtime_checkable


_logger = logging.getLogger("peldrun.memory.llm_contracts")


# ---------------------------------------------------------------------------
# Defaults shared by every LLM-backed memory component
# ---------------------------------------------------------------------------


DEFAULT_GATEWAY_TIMEOUT_SLACK_SECONDS: int = 5
"""Seconds added to a component's own deadline before handing the
call to the gateway.

Every Phase 6 component enforces its OWN authoritative deadline with
``asyncio.wait_for``. The gateway's ``timeout_seconds`` is only a
safety net against a transport that ignores cancellation. Keeping it
slightly LOOSER than the component deadline guarantees the component
always wins the race, so the component's documented degradation
(``[]`` / ``[query]`` / zero-confidence) is what the caller observes,
not an opaque gateway timeout.

This mirrors the reranker's existing behaviour
(``int(self._timeout_seconds) + 5``), promoted to a named constant so
the three Phase 6 components cannot drift apart.
"""


# ---------------------------------------------------------------------------
# The Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StructuredGatewayProtocol(Protocol):
    """The subset of ``MemoryModelGateway`` the memory core uses.

    A conforming implementation MUST:

      * accept every keyword argument below,
      * return an object exposing a ``parsed`` mapping (or be a
        plain ``dict`` itself -- :func:`extract_parsed_object`
        accepts both),
      * raise on transport / parse failure. Components catch and
        degrade; they never propagate a provider error to a user
        request.

    The concrete implementation is
    ``omweb.memory.extraction.gateway.MemoryModelGateway``. Tests
    typically pass a tiny mock exposing the same coroutine.
    """

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        provider_id: str,
        model_id: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_output_tokens: int = ...,
        timeout_seconds: int = ...,
        connection_id: str = ...,
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


_WRAPPER_KEYS: frozenset = frozenset(
    {
        "parsed",
        "raw_text",
        "cost_nano_usd",
        "input_tokens",
        "output_tokens",
        "usage",
        "model_id",
        "provider_id",
    }
)
"""Keys that identify a DICT that mimics ``LLMStructuredResponse``.

Most test doubles return the payload directly (``{"entities": [...]}``),
but a double that copies the production shape produces
``{"parsed": {...}, "raw_text": "..."}``. Both must work, so a dict
is unwrapped when -- and only when -- BOTH conditions hold:

  1. it carries a ``"parsed"`` key whose value is a dict, and
  2. every one of its keys is a known wrapper key.

Condition 2 is what keeps a genuine parsed object safe: an LLM
answering ``{"parsed": "..."}`` as an actual entity payload, or any
dict carrying business keys alongside ``"parsed"``, is returned
verbatim rather than being silently gutted.
"""


def extract_parsed_object(response: Any) -> Optional[Dict[str, Any]]:
    """Return the parsed JSON object from a gateway response.

    Accepted shapes
    ---------------
      1. ``LLMStructuredResponse`` (or any object with a ``parsed``
         attribute that is a ``dict``) -- the production shape.
      2. A bare ``dict`` -- what most test mocks return. If that
         dict is a MIMIC of the production wrapper (see
         ``_WRAPPER_KEYS``), its ``"parsed"`` value is unwrapped.
      3. An object with an ``as_dict()`` method returning a ``dict``
         -- tolerated for future gateway variants.

    Rejected shapes (return ``None``)
    ---------------------------------
      * ``None``,
      * a non-dict ``parsed`` (a list, a string, ``None``),
      * anything else.

    Why the attribute is probed BEFORE the dict case
    ------------------------------------------------
    A dataclass is not a mapping; calling ``dict()`` on it raises
    ``TypeError``. The explicit attribute probe is the only reliable
    way to accept a dataclass and a mapping in one function.

    Parameters
    ----------
    response
        Whatever the gateway returned.

    Returns
    -------
    Optional[Dict[str, Any]]
        The parsed object, or ``None`` when the shape is unusable.
        Callers treat ``None`` as "the LLM call produced nothing
        structured" and apply their own documented fallback.
    """
    if response is None:
        return None

    # Shape 1: `.parsed` holding a dict (the production dataclass).
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed

    # Shape 2: the response IS the parsed object, or a dict mimic
    # of the production wrapper.
    if isinstance(response, dict):
        candidate = response.get("parsed")
        if isinstance(candidate, dict) and set(response) <= _WRAPPER_KEYS:
            return candidate
        return response

    # Shape 3: an explicit converter.
    as_dict = getattr(response, "as_dict", None)
    if callable(as_dict):
        try:
            converted = as_dict()
        except Exception:  # noqa: BLE001 -- defensive, never raises
            return None
        if isinstance(converted, dict):
            return converted

    return None


def first_present(
    mapping: Optional[Dict[str, Any]],
    *keys: str,
) -> Any:
    """Return the first non-``None`` value among ``keys``.

    Models routinely rename a field between prompt revisions
    (``"name"`` vs ``"text"``, ``"type"`` vs ``"label"``). Every
    component in Phase 6 reads its fields through this helper so a
    single alias list covers the variation, instead of scattering
    ``or``-chains through the parsing code.

    Returns ``None`` when ``mapping`` is ``None`` or no key is
    present with a non-``None`` value. An empty string IS returned
    (it is a present-but-empty value); callers that care strip it.
    """
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value is not None:
                return value
    return None


# ---------------------------------------------------------------------------
# Process-wide serialization gate
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS
# ---------------
# Every Phase 6 LLM call funnels through ``call_structured``: entity
# extraction (graph indexing), query rewriting, gap analysis, and
# evidence synthesis. Each of those is a ``/chat/completions`` request.
#
# A local runtime (LM Studio, Ollama, llama.cpp server) serves ONE
# generation at a time. When two requests arrive together it does not
# queue them politely -- it interleaves their token streams, and the
# output becomes character soup:
#
#     {"{"{"rewnerewrittenededritten_":_queries truequeries":,": [" ...
#
# That is not a parsing problem to work around; there is no correct JSON
# in that stream. The only cure is to stop issuing overlapping calls.
#
# Sources of overlap in practice:
#   * the composer fires a new agentic run on each debounced pause while
#     the previous run is still generating,
#   * the write pipeline dispatches one indexing task per written
#     memory,
#   * a backfill tick can overlap an agentic run.
#
# The gate below serializes ALL of them, whatever their origin.
#
# Why a per-loop lock
# -------------------
# A single module-level ``asyncio.Lock`` would bind to the first event
# loop that awaits it and then raise "bound to a different event loop"
# for every subsequent test that calls ``asyncio.run``. The lock is
# therefore created lazily PER RUNNING LOOP and stored in a
# ``WeakKeyDictionary`` so finished loops are collected.

_LLM_GATES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

_LLM_LIMIT: int = 1
"""Maximum simultaneous memory LLM calls (process-wide).

``1`` is the safe default: every Phase 6 component talks to a CHAT
completion endpoint, and most local runtimes (LM Studio, Ollama,
llama.cpp server) serve one generation at a time. Two overlapping
calls do not queue -- the provider interleaves their token streams and
both payloads become unparseable.

Configured from ``MemoryAISettings.llm_max_concurrency``; raise it only
for a hosted API with real concurrency headroom.
"""


def configure_llm_concurrency(limit: int) -> None:
    """Set the process-wide memory-LLM concurrency limit.

    Called by ``MemoryService`` on initialize and on every settings
    reload, so the operator can widen the gate for a hosted provider
    without a restart.

    Per-loop gates created under the PREVIOUS limit are discarded; they
    are recreated lazily on the next call. A call already waiting on an
    old gate still completes -- only new arrivals use the new width.
    """
    global _LLM_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = 1
    _LLM_LIMIT = max(1, value)
    _LLM_GATES.clear()


def current_llm_concurrency() -> int:
    """Return the configured process-wide concurrency limit."""
    return _LLM_LIMIT


def _llm_gate() -> "asyncio.Semaphore":
    """Return the concurrency gate for the CURRENT event loop."""
    loop = asyncio.get_running_loop()
    gate = _LLM_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(_LLM_LIMIT)
        _LLM_GATES[loop] = gate
    return gate


def memory_llm_gate() -> "asyncio.Semaphore":
    """Public accessor for the shared memory-LLM gate.

    For components that call a gateway DIRECTLY instead of going
    through :func:`call_structured` -- notably the Phase 5 reranker,
    whose batching and degradation contract predate this module.

    Correct usage::

        async with memory_llm_gate():
            response = await gateway.generate_structured(...)

    Semantics under contention match ``call_structured``: the caller
    QUEUES. A caller with its own outer deadline (the reranker's 3s
    ``wait_for``) will therefore time out while queued, which is the
    correct outcome -- degrading to RRF ordering is far better than
    interleaving a second generation into a single-slot runtime.
    """
    return _llm_gate()


async def call_structured(
    gateway: Optional["StructuredGatewayProtocol"],
    *,
    system_prompt: str,
    user_prompt: str,
    provider_id: Optional[str],
    model_id: Optional[str],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_output_tokens: int = 800,
    timeout_seconds: float = 5.0,
    connection_id: str = "memory_agentic",
) -> Optional[Dict[str, Any]]:
    """Run one structured LLM call with a bounded deadline.

    This is the shared execution primitive behind every Phase 6
    component (entity extraction, query rewriting, evidence
    synthesis, gap analysis). It centralizes four rules that are
    easy to get subtly wrong per-call-site:

      1. **The caller's deadline wins.** ``asyncio.wait_for`` wraps
         the call with ``timeout_seconds``; the gateway receives
         ``timeout_seconds + DEFAULT_GATEWAY_TIMEOUT_SLACK_SECONDS``
         so its own safety-net timeout can never fire first and
         replace this function's documented ``None`` with an opaque
         provider error.
      2. **``CancelledError`` propagates.** Swallowing cancellation
         leaves zombie tasks that shutdown timeouts cannot clear.
      3. **Every other exception becomes ``None``** with a WARNING --
         never an exception to the caller.
      4. **Unparseable responses become ``None``** rather than an
         empty dict, so "the model answered nothing" and "the model
         answered an empty object" stay distinguishable.

    Returns
    -------
    Optional[Dict[str, Any]]
        The parsed JSON object, or ``None`` for every failure mode
        (no gateway, unresolved model, timeout, transport error,
        unparseable payload). Callers map ``None`` onto their own
        documented degradation.
    """
    if gateway is None:
        return None
    provider = (provider_id or "").strip()
    model = (model_id or "").strip()
    if not provider or not model:
        return None

    try:
        # Serialize against every other memory LLM call in this
        # process. See the module note above: overlapping calls against
        # a single-slot local runtime produce interleaved garbage, not
        # queued answers.
        #
        # The lock is acquired with the SAME deadline as the call, so a
        # wedged request cannot block every later one forever.
        async with _llm_gate():
            response = await asyncio.wait_for(
                gateway.generate_structured(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    provider_id=provider,
                    model_id=model,
                    api_key=api_key,
                    base_url=base_url,
                    max_output_tokens=max_output_tokens,
                    timeout_seconds=(
                        int(timeout_seconds)
                        + DEFAULT_GATEWAY_TIMEOUT_SLACK_SECONDS
                    ),
                    connection_id=connection_id,
                ),
                timeout=max(0.001, float(timeout_seconds)),
            )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        _logger.warning(
            "LLM call timed out after %ss (connection=%r, "
            "provider=%r, model=%r).",
            timeout_seconds,
            connection_id,
            provider,
            model,
        )
        return None
    except Exception as exc:  # noqa: BLE001 -- documented contract
        _logger.warning(
            "LLM call failed (connection=%r, %s: %s).",
            connection_id,
            type(exc).__name__,
            exc,
        )
        return None

    parsed = extract_parsed_object(response)
    if parsed is None:
        _logger.warning(
            "LLM response had no parsable JSON object "
            "(connection=%r).",
            connection_id,
        )
    return parsed


__all__ = [
    "DEFAULT_GATEWAY_TIMEOUT_SLACK_SECONDS",
    "StructuredGatewayProtocol",
    "call_structured",
    "configure_llm_concurrency",
    "current_llm_concurrency",
    "extract_parsed_object",
    "first_present",
]
