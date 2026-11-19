# backend/peldrun/memory/agentic/query_rewriter.py
"""
PELDRUN Memory Platform - Agentic Query Rewriter.

Decomposes one complex question into 1..3 standalone sub-queries that
a memory search can answer independently.

Why decomposition
-----------------
A memory index matches TEXT. The question "what did my manager say
about the project?" contains no entity the index knows: "my manager"
and "the project" are indirect references whose resolution is the
job of a multi-hop retrieval, not of a similarity search. Splitting
the question into "who is the user's manager" and "what is the user's
current project" turns it into two searches that CAN succeed, and
whose results then feed the next hop.

Design invariants
-----------------

1. **Never returns an empty list.**
   The output of this class is the direct input of the retrieval
   loop. An empty list would make the loop run zero hops and return
   "no evidence" for a question that may well have an answer. Every
   failure path therefore returns ``[query]`` -- the original
   question, unchanged. That is a strictly better fallback than any
   rewrite, because it is exactly what the non-agentic path would
   have searched.

2. **Bounded in both directions.**
   At most ``max_sub_queries`` (default 3) queries, because every
   sub-query multiplies the hop's search calls. And at most
   ``max_query_chars`` per sub-query, because a model that echoes
   the whole prompt as a "query" would produce an unusably long
   search string.

3. **Deduplicated, order-preserving.**
   Models routinely return the same sub-query twice, or return the
   original question alongside its own decomposition. Duplicates are
   removed case-insensitively while preserving first-seen order, so
   the hop performs each distinct search exactly once.

4. **No hard-coded question analysis.**
   There is no "if the query contains 'manager' then ..." rule
   anywhere in this module. The decomposition is entirely the
   model's job; this class only validates and bounds its output.
   A heuristic layer here would silently override the model for
   exactly the phrasings the developer happened to think of.

See also
--------

    * ``peldrun/memory/agentic/multi_hop_retriever.py`` -- the caller
    * ``peldrun/memory/agentic/evidence_synthesizer.py`` -- gap
      analysis produces the sub-queries for hops 2..N
    * ``peldrun/memory/llm_contracts.py`` -- ``call_structured``
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from peldrun.memory.llm_contracts import (
    StructuredGatewayProtocol,
    call_structured,
    first_present,
)


logger = logging.getLogger(
    "peldrun.memory.agentic.query_rewriter"
)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

REWRITE_PROMPT: str = """Decompose this complex question into 1-3 simple sub-queries.

Original question: {query}

Return ONLY JSON, in this shape (replace the placeholders, do not copy them):
{{"rewritten_queries": [<one standalone query>]}}

Rules:
- Each sub-query must be standalone and searchable on its own.
- Resolve indirect references into explicit ones ("my manager" -> "who is my manager").
- Maximum 3 sub-queries.
- Never emit placeholder text such as "sub-query 1" or "query 1".
- If the question is already simple, return it unchanged as the only item.
- Do NOT answer the question. Only rewrite it.
"""


SYSTEM_PROMPT: str = (
    "You decompose questions into standalone search queries and "
    "return only JSON."
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


DEFAULT_MAX_SUB_QUERIES: int = 3
"""Upper bound on sub-queries per rewrite.

Three covers the realistic breadth of a memory question (who / what
/ when) while keeping a hop's search count bounded. Every extra
sub-query costs one additional backend search on EVERY hop.
"""


DEFAULT_MAX_QUERY_CHARS: int = 300
"""Per-sub-query length cap.

A genuine search query is a sentence, not a paragraph. 300 chars is
generous for the former and truncates a model that decides to copy
the prompt back.
"""


_CONNECTION_ID: str = "memory_query_rewriter"


# ---------------------------------------------------------------------------
# QueryRewriter
# ---------------------------------------------------------------------------


class QueryRewriter:
    """Decomposes a question into standalone sub-queries.

    Stateless; safe to share across concurrent requests.
    """

    def __init__(
        self,
        gateway: Optional[StructuredGatewayProtocol],
        *,
        provider_id: Optional[str],
        model_id: Optional[str],
        max_output_tokens: int = 300,
        timeout_seconds: float = 5.0,
        max_sub_queries: int = DEFAULT_MAX_SUB_QUERIES,
        max_query_chars: int = DEFAULT_MAX_QUERY_CHARS,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        connection_id: str = _CONNECTION_ID,
    ) -> None:
        """Construct the rewriter.

        Parameters
        ----------
        gateway
            Anything satisfying ``StructuredGatewayProtocol``.
            ``None`` makes every ``rewrite`` a no-op that returns
            ``[query]``.
        provider_id, model_id
            The decomposition model. Either being falsy disables the
            LLM call (same degradation as ``gateway=None``).
        max_output_tokens
            300 fits three short sub-queries plus JSON scaffolding.
        timeout_seconds
            The rewriter's own deadline. A rewrite is on the critical
            path of an agentic request, so it is deliberately the
            TIGHTEST deadline of the three components.
        max_sub_queries, max_query_chars
            Output bounds; non-positive values are corrected to the
            defaults rather than raising, because they arrive from a
            hand-edited settings file.
        api_key, base_url
            Optional overrides; ``None`` lets the gateway resolve
            credentials from the model vault.
        connection_id
            Cost/log attribution label.
        """
        self._gateway = gateway
        self._provider_id = (provider_id or "").strip()
        self._model_id = (model_id or "").strip()
        self._max_output_tokens = _positive_int(
            max_output_tokens, 300
        )
        self._timeout_seconds = _positive_float(
            timeout_seconds, 5.0
        )
        self._max_sub_queries = _positive_int(
            max_sub_queries, DEFAULT_MAX_SUB_QUERIES
        )
        self._max_query_chars = _positive_int(
            max_query_chars, DEFAULT_MAX_QUERY_CHARS
        )
        self._api_key = api_key
        self._base_url = base_url
        self._connection_id = (
            connection_id or _CONNECTION_ID
        ).strip() or _CONNECTION_ID

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def rewrite(self, query: str) -> List[str]:
        """Return 1..``max_sub_queries`` standalone sub-queries.

        Returns ``[query]`` (the original, unchanged) for EVERY
        failure mode: empty input, no gateway, unresolved model,
        timeout, transport error, unparseable JSON, a non-list
        payload, or a list with no usable strings. See invariant #1.

        The original question is the last-resort fallback because it
        is exactly what a non-agentic search would have used -- so a
        rewriter outage degrades the agentic path to the standard
        one, never to "search nothing".
        """
        text = query if isinstance(query, str) else ""
        text = text.strip()
        if not text:
            return []

        fallback = [text]

        if not self.is_configured:
            logger.debug(
                "QueryRewriter: not configured; using the original "
                "query."
            )
            return fallback

        parsed = await call_structured(
            self._gateway,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=REWRITE_PROMPT.format(query=text),
            provider_id=self._provider_id,
            model_id=self._model_id,
            api_key=self._api_key,
            base_url=self._base_url,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout_seconds,
            connection_id=self._connection_id,
        )
        if parsed is None:
            return fallback

        raw = first_present(
            parsed, "rewritten_queries", "queries", "sub_queries"
        )
        if not isinstance(raw, list):
            logger.debug(
                "QueryRewriter: response had no usable query list "
                "(got %s); using the original query.",
                type(raw).__name__,
            )
            return fallback

        cleaned = self._clean(raw)
        if not cleaned:
            return fallback

        # If the model returned the original question among others,
        # keep it first: it is the highest-precision search.
        original_key = text.casefold()
        if any(item.casefold() == original_key for item in cleaned):
            cleaned = [text] + [
                item
                for item in cleaned
                if item.casefold() != original_key
            ]
            cleaned = cleaned[: self._max_sub_queries]

        return cleaned

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _clean(self, raw: List[Any]) -> List[str]:
        """Normalize, bound, and deduplicate the model's query list."""
        cleaned: List[str] = []
        seen: set = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            text = " ".join(item.split())
            if not text:
                continue
            if len(text) > self._max_query_chars:
                text = text[: self._max_query_chars].rstrip()
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
            if len(cleaned) >= self._max_sub_queries:
                break
        return cleaned

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        """True when a gateway and a resolvable model are present."""
        return (
            self._gateway is not None
            and bool(self._provider_id)
            and bool(self._model_id)
        )

    @property
    def provider_id(self) -> str:
        """The resolved provider id (empty string when unset)."""
        return self._provider_id

    @property
    def model_id(self) -> str:
        """The resolved model id (empty string when unset)."""
        return self._model_id

    @property
    def timeout_seconds(self) -> float:
        """The per-rewrite deadline."""
        return self._timeout_seconds

    @property
    def max_sub_queries(self) -> int:
        """The sub-query cap."""
        return self._max_sub_queries

    def __repr__(self) -> str:
        return (
            f"QueryRewriter(provider={self._provider_id!r}, "
            f"model={self._model_id!r}, "
            f"max_sub_queries={self._max_sub_queries}, "
            f"configured={self.is_configured})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _positive_int(value: Any, default: int) -> int:
    """Coerce to a positive int, falling back to ``default``."""
    if isinstance(value, bool):
        return default
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return numeric if numeric > 0 else default


def _positive_float(value: Any, default: float) -> float:
    """Coerce to a positive float, falling back to ``default``."""
    if isinstance(value, bool):
        return default
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if numeric != numeric or numeric <= 0.0:
        return default
    return numeric


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_MAX_QUERY_CHARS",
    "DEFAULT_MAX_SUB_QUERIES",
    "REWRITE_PROMPT",
    "QueryRewriter",
]
