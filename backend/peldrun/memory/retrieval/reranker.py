# backend/peldrun/memory/retrieval/reranker.py
"""
PELDRUN Memory Platform - Cross-Encoder Reranker (Phase 5A).

LLM-powered relevance re-ranking for hybrid search results. Sits
BETWEEN the RRF fusion step (``HybridMemoryProvider._rrf_merge``)
and the ranker/budget-fitter, so downstream consumers see
re-ordered candidates whose scores better reflect query-document
relevance than RRF alone can achieve.

Why a reranker on top of RRF?
-----------------------------

RRF is rank-only: it rewards agreement between FTS and vector
results WITHOUT understanding what the query actually means.
Two documents that both appear at rank 1 in their respective
lists get similar RRF scores, even when one is a near-perfect
semantic match and the other merely shares keywords.

A cross-encoder (the LLM, in this case) reads the query AND each
candidate TOGETHER, so it can distinguish "user asked about X,
this document is ABOUT X" from "user asked about X, this document
mentions X in passing". This is the classic "retrieve then rerank"
pattern from IR literature.

This implementation is a "generative" cross-encoder: it asks the
LLM to score each candidate on a 0-10 scale, then sorts by that
score. Cheaper than a full pairwise cross-encoder (one call per
batch instead of N calls for N candidates), robust to the LLM's
typical output variance.

Architectural placement
-----------------------

This file lives in ``peldrun`` (domain layer) because re-ranking
is a PURE FUNCTION over (query, candidates). It depends on the
LLM ONLY through ``RerankGatewayProtocol`` -- a structural
Protocol that ``omweb.memory.extraction.gateway.MemoryModelGateway``
satisfies WITHOUT any import from ``peldrun``.

The Protocol is defined HERE (in ``peldrun``) so that:

  * ``peldrun`` never imports ``omweb`` (architectural rule).
  * Tests can inject ANY object with a matching
    ``generate_structured`` method, without touching the
    extraction gateway.
  * A future refactor that moves the gateway to a different
    module or replaces it entirely works transparently -- the
    reranker only cares about the method signature.

Graceful degradation contract
-----------------------------

EVERY failure mode returns ``candidates[:top_k]`` UNCHANGED
(original RRF ordering, original scores). The reranker NEVER
raises and NEVER makes a result WORSE than no reranker:

  * Rerank disabled in settings             -> return as-is.
  * Gateway/model not configured (NO-OP)    -> return as-is.
  * Gateway raises ``LLMTransportError``    -> return as-is.
  * ``asyncio.wait_for`` timeout fires       -> return as-is.
  * LLM returns unparseable JSON             -> return as-is.
  * LLM returns ``{"scores": []}``           -> return as-is.
  * LLM returns scores for only SOME
    candidates (partial response)            -> return as-is.
  * LLM returns indices out of range         -> return as-is.
  * Any batch fails during multi-batch
    processing                              -> return as-is.

This is the same contract that ``HybridMemoryProvider`` uses for
FTS/vector failures: degrade to "no enhancement" rather than
"broken enhancement".

Settings reload safety
----------------------

The reranker instance persists across ``MemoryService.reload_settings``
calls that only flip ``rerank_enabled`` (no model_key change ->
no rebuild). Therefore the reranker accepts an OPTIONAL
``rerank_enabled_provider: Callable[[], bool]`` and re-reads it
on EVERY ``rerank()`` call. This matches the reference
``if not self._settings.rerank_enabled: return ...`` guard while
remaining reload-safe.

``HybridMemoryProvider`` uses the ``last_rerank_succeeded``
property after each call to decide whether to set
``_last_search_mode = "hybrid+rerank"``.

Cost model
----------

Each batch of ``rerank_batch_size`` candidates costs one LLM
call. With defaults (batch_size=20, max_candidates=20) the
common case is ONE call per search. Smaller batch sizes
increase reliability (shorter prompts parse better) at the cost
of more round-trips.

The worst-case latency is ``ceil(max_candidates / batch_size) *
timeout_seconds`` -- bounded by ``rerank_timeout_seconds`` per
batch. In practice, healthy providers respond in <500ms per
batch.

Reranking is OFF by default (``retrieval.rerank_enabled=False``
in settings). When OFF, ``HybridMemoryProvider`` does not even
invoke ``rerank`` -- the search path is unchanged from Phase 4.

See also
--------

  * ``omweb/memory/manager/hybrid_provider.py``
                          -- invokes ``rerank`` after RRF fusion.
  * ``omweb/memory/extraction/gateway.py``
                          -- the LLM gateway that satisfies
                             ``RerankGatewayProtocol``.
  * ``peldrun/memory/domain/settings.py``
                          -- ``MemoryRetrievalSettings`` carries
                             the ``rerank_*`` tuning fields.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from peldrun.memory.domain import MemoryEntry


logger = logging.getLogger("peldrun.memory.retrieval.reranker")


# ---------------------------------------------------------------------------
# Module-level prompts (overridable for testing / prompt tuning)
# ---------------------------------------------------------------------------
#
# These are MODULE constants rather than class attributes so that:
#
#   * Tests can monkey-patch them (``reranker.RERANK_PROMPT = "..."``)
#     without subclassing.
#   * Prompt tuning (e.g. adding few-shot examples) is a one-line
#     edit, not a method-body refactor.
#   * The ``RERANK_PROMPT`` template is the SAME one the reference
#     §6.3 design uses -- anyone comparing the two can see they
#     are aligned at a glance.


RERANK_SYSTEM_PROMPT = (
    "You are a relevance scoring assistant for a memory "
    "retrieval system. Rate how relevant each memory is to a "
    "user's query. Return ONLY a JSON object -- no prose, no "
    "markdown fences, no explanation."
)


RERANK_PROMPT = """You are a relevance scoring assistant for a memory retrieval system.

QUERY:
{query}

MEMORIES:
{items}

Rate the relevance of each memory to the query on a scale of 0 to 10, where:
- 0 = completely irrelevant
- 5 = somewhat related but not directly relevant
- 10 = directly answers or is highly relevant to the query

Return ONLY a JSON object with this exact structure:
{{"scores": [{{"id": 0, "score": 8.5}}, {{"id": 1, "score": 3.0}}, ...]}}

Rules:
- One entry per memory, in the same order as listed above.
- Score must be a number between 0 and 10 (inclusive).
- Do not include any explanation or additional text outside the JSON.
"""


# ---------------------------------------------------------------------------
# Protocol: the shape of the LLM gateway this reranker consumes.
# ---------------------------------------------------------------------------
#
# Structural (duck-typed) Protocol so that:
#
#   * ``peldrun`` never imports ``omweb.memory.extraction.gateway``.
#   * Tests can inject a simple mock (no async setup, no vault,
#     no HTTP).
#   * The real gateway (``MemoryModelGateway``) satisfies the
#     Protocol AUTOMATICALLY -- its ``generate_structured`` has a
#     compatible signature (verified by the Phase 5A integration
#     test that builds the reranker against the real gateway).
#
# The return type is ``Any`` rather than a concrete dataclass
# because the gateway's ``LLMStructuredResponse`` lives in
# ``omweb`` -- the reranker only reads ``.parsed`` (a dict) and
# is otherwise agnostic.


class RerankGatewayProtocol(Protocol):
    """The subset of ``MemoryModelGateway`` this reranker uses.

    A concrete implementation MUST:

      * Accept the keyword arguments below.
      * Return an object exposing a ``parsed: Dict[str, Any]``
        attribute (or a dict itself -- the reranker accepts both,
        see ``_extract_parsed``).
      * Raise on transport / parse failure (the reranker catches
        and degrades).
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
# Constants
# ---------------------------------------------------------------------------


_DEFAULT_RERANK_MAX_CANDIDATES: int = 20
"""Upper bound on candidates sent to the LLM per rerank call.

More candidates = better ordering (the tail that wasn't scored
retains its RRF position) but more LLM cost. 20 is the sweet
spot: it comfortably covers the ``default_limit * 4`` cap that
the ranker/budget fitter downstream normally considers, so
virtually all retrievable entries get scored.

The tail (candidates beyond this cap) is appended UNSORTED after
the reranked set, preserving RRF order. It only matters when
``top_k`` exceeds the number of scored candidates.
"""


_DEFAULT_RERANK_BATCH_SIZE: int = 5
"""Candidates per LLM call.

Smaller batches = more calls (higher latency, more cost overhead)
but more robust parsing (shorter prompts are less likely to
trigger the model's "ignore the schema" failure mode). 5 is the
empirical sweet spot for chat-tuned models: large enough to
amortize the per-call overhead, small enough that JSON parsing
rarely fails.

Operators who prefer single-call behavior (the reference §6.3
"no batching in 5A" suggestion) set this to >= ``rerank_max_candidates``
(typically 20) via ``PATCH /api/memory/settings``.
"""


_DEFAULT_TIMEOUT_SECONDS: float = 3.0
"""Per-batch wall-clock timeout.

Applied via ``asyncio.wait_for`` around each ``generate_structured``
call. A healthily-performing provider returns in <500ms; a
provider that exceeds 3s is either overloaded (retrying will not
help) or unreachable (the connection is dead). Either way, the
reranker degrades to RRF ordering rather than holding the user's
search response hostage.
"""


_DEFAULT_MAX_OUTPUT_TOKENS: int = 800
"""Completion-token cap for one batch's scoring response.

Scoring 5 candidates on a 0-10 scale produces ~10-20 output
tokens per candidate plus JSON scaffolding -- 800 tokens is
generous (matches the §6.3 reference) and bounds cost if the
model rambles before emitting JSON.
"""


_MAX_MEMORY_CONTENT_CHARS: int = 300
"""Per-candidate content truncation in the prompt.

A single memory can be arbitrarily long (user-pasted code, log
dumps, etc.). Truncating to 300 chars keeps the prompt small
(5 candidates × 300 chars = ~1500 chars/batch = ~400 tokens) and
rarely loses signal -- the FIRST 300 chars of a memory are almost
always its most identity-bearing.

The reference §6.3 uses ``[:200]``; 300 is the middle ground
between "compact" and "identity-bearing".
"""


# ---------------------------------------------------------------------------
# CrossEncoderReranker
# ---------------------------------------------------------------------------


class CrossEncoderReranker:
    """LLM-based relevance re-ranker for hybrid search results.

    See the module docstring for the design rationale and the
    graceful degradation contract. This class is STATELESS after
    construction EXCEPT for ``_last_rerank_succeeded`` (a
    diagnostic flag). Each ``rerank`` call owns its own local
    variables.

    Lifecycle
    ---------
    Constructed by ``MemoryService._build_hybrid_provider``
    (omweb side) and passed to ``HybridMemoryProvider.__init__``.
    Closed implicitly when the hybrid provider is torn down in
    ``_close_embedding``. The reranker holds NO owned resources
    of its own -- the gateway is shared with the extraction
    pipeline, so we do NOT close it here.

    Threading / asyncio
    -------------------
    All methods are ``async``. The reranker NEVER spawns
    background tasks -- every ``rerank`` call is a fully awaited
    coroutine. This makes the calling convention simple: the
    caller awaits the rerank, gets the result, done.

    Concurrency note
    ----------------
    ``_last_rerank_succeeded`` is per-instance state, not
    per-call. Concurrent ``rerank`` calls on the same instance
    will race on this flag (last-writer-wins). This is the same
    diagnostic-only caveat as ``HybridMemoryProvider._last_search_mode``:
    the flag informs UI/diagnostics, not control flow. If a
    future use case needs per-call fidelity, refactor to return
    ``(results, did_rerank)`` -- a breaking change to the
    reference signature, deferred.
    """

    def __init__(
        self,
        *,
        gateway: Optional[RerankGatewayProtocol] = None,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        rerank_max_candidates: int = _DEFAULT_RERANK_MAX_CANDIDATES,
        rerank_batch_size: int = _DEFAULT_RERANK_BATCH_SIZE,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        rerank_enabled_provider: Optional[Callable[[], bool]] = None,
        connection_id: str = "memory_rerank",
    ) -> None:
        """Construct a reranker.

        Parameters
        ----------
        gateway
            The LLM gateway (satisfying ``RerankGatewayProtocol``).
            When ``None``, the reranker is a NO-OP: every
            ``rerank`` call returns ``candidates[:top_k]``
            unchanged. This is the correct behavior for the
            "settings enabled but model not configured" case --
            the operator gets the Phase 4 behavior without an
            error log.

        provider_id, model_id
            The LLM to invoke. When either is ``None`` or empty,
            the reranker is also a NO-OP. The orchestrator
            (``_build_hybrid_provider``) is responsible for
            resolving these from settings; passing ``None`` here
            means "the operator hasn't picked a model yet".

        api_key, base_url
            Optional credentials overrides forwarded to the
            gateway. ``None`` means "gateway resolves from the
            vault" (the normal production path).

        rerank_max_candidates
            Upper bound on candidates sent to the LLM per rerank
            call. Candidates beyond this cap are appended to the
            result WITHOUT scoring (preserving RRF order).
            Corresponds to ``settings.retrieval.rerank_max_candidates``.
            Invalid values are clamped to the default.

        rerank_batch_size
            Candidates per LLM call. Controls the number of
            LLM round-trips per rerank. Corresponds to
            ``settings.retrieval.rerank_batch_size``. Invalid
            values are clamped to the default.

        timeout_seconds
            Per-batch wall-clock timeout. Applied via
            ``asyncio.wait_for``. Corresponds to
            ``settings.retrieval.rerank_timeout_seconds``.
            Invalid values are clamped to the default.

        max_output_tokens
            Completion-token cap per batch. Must be a positive
            int; invalid values are clamped to the default.

        rerank_enabled_provider
            Optional callable that returns the CURRENT
            ``rerank_enabled`` flag from settings. When supplied,
            ``rerank()`` calls it BEFORE every attempt and
            returns the input unchanged when it returns False.
            This makes the reranker reload-safe: settings flips
            (True -> False) take effect immediately without
            rebuilding the pipeline.

            When ``None`` (the default), the reranker assumes it
            is always enabled -- this is correct for tests and
            for the case where the operator has already
            satisfied the enabled check before constructing
            this instance.

        connection_id
            Diagnostic identifier for the accounting ledger.
            ``"memory_rerank"`` by default -- distinct from
            ``"memory_extraction"`` so operators can attribute
            cost to the reranker specifically.
        """
        # ---- Store raw inputs ----------------------------------
        self._gateway: Optional[RerankGatewayProtocol] = gateway
        self._provider_id: Optional[str] = (
            provider_id.strip()
            if isinstance(provider_id, str) and provider_id.strip()
            else None
        )
        self._model_id: Optional[str] = (
            model_id.strip()
            if isinstance(model_id, str) and model_id.strip()
            else None
        )
        self._api_key: Optional[str] = api_key
        self._base_url: Optional[str] = base_url
        self._connection_id: str = connection_id
        self._rerank_enabled_provider: Optional[
            Callable[[], bool]
        ] = rerank_enabled_provider

        # ---- Clamp tuning parameters ---------------------------
        # Defensive: if a caller passes a wrong type / negative
        # value, fall back to defaults rather than propagate a
        # confusing failure into the search path. A misconfigured
        # reranker degrades silently -- it NEVER breaks the
        # search.
        self._rerank_max_candidates: int = self._clamp_int(
            rerank_max_candidates,
            _DEFAULT_RERANK_MAX_CANDIDATES,
            minimum=1,
        )
        self._rerank_batch_size: int = self._clamp_int(
            rerank_batch_size,
            _DEFAULT_RERANK_BATCH_SIZE,
            minimum=1,
        )
        self._timeout_seconds: float = self._clamp_float(
            timeout_seconds, _DEFAULT_TIMEOUT_SECONDS, minimum=0.1,
        )
        self._max_output_tokens: int = self._clamp_int(
            max_output_tokens, _DEFAULT_MAX_OUTPUT_TOKENS, minimum=64,
        )

        # ---- Precompute the "is this reranker useful?" flag ----
        # When any of the required pieces is missing, we are a
        # NO-OP. ``rerank`` short-circuits on this flag, so the
        # per-call overhead is one boolean check.
        self._is_active: bool = (
            self._gateway is not None
            and self._provider_id is not None
            and self._model_id is not None
        )

        # Diagnostic flag updated at the end of every rerank()
        # call: True = LLM returned usable scores (reranking
        # actually applied); False = fell back to RRF ordering.
        # Read by HybridMemoryProvider to decide whether to set
        # ``_last_search_mode = "hybrid+rerank"``.
        self._last_rerank_succeeded: bool = False

        if not self._is_active:
            logger.info(
                "CrossEncoderReranker constructed in NO-OP mode "
                "(gateway=%s, provider_id=%s, model_id=%s). "
                "Calls to rerank() return input unchanged.",
                "set" if gateway is not None else "None",
                self._provider_id or "None",
                self._model_id or "None",
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def rerank(
        self,
        query: str,
        candidates: List[Tuple[MemoryEntry, float]],
        top_k: int = 10,
    ) -> List[Tuple[MemoryEntry, float]]:
        """Re-rank ``candidates`` by LLM-scored query relevance.

        Parameters
        ----------
        query
            The user's search query. Empty / whitespace queries
            return ``candidates[:top_k]`` unchanged (there is
            nothing to score relevance AGAINST).

        candidates
            The RRF-fused ``(entry, score)`` list, ordered by
            ``score`` descending. Scores are RRF-normalized to
            ``[0, 1]`` by the hybrid provider -- this method
            does NOT trust that scale and re-normalizes to
            ``[0, 1]`` based on the LLM's own scores.

        top_k
            Number of candidates to return. Defaults to 10.
            Values ``<= 0`` return an empty list (defensive).
            Values ``> len(candidates)`` are clamped to the
            actual length after reranking.

        Returns
        -------
        List[Tuple[MemoryEntry, float]]
            At most ``top_k`` entries. When reranking succeeds:
            ordered by LLM score (descending), with original
            RRF position as tiebreaker (stable sort). Scores
            are normalized to ``[0, 1]``. When reranking fails
            OR the reranker is in NO-OP mode: identical to
            ``candidates[:top_k]`` (original RRF order and
            scores preserved).

        Contract
        --------
        * NEVER raises. Every failure path returns the input
          truncated to ``top_k``.
        * Preserves entry identity: the returned entries are
          the SAME objects (not copies) as the input.
        * Stable ordering: equal LLM scores retain original
          RRF ordering (Python's stable sort guarantees this).
        * ``last_rerank_succeeded`` is updated at the END of
          every call (True = reranked, False = fell back).
        """
        # Reset the diagnostic flag at the START of every call.
        # Every return path below must leave it consistent with
        # what the caller actually received.
        self._last_rerank_succeeded = False

        # ---- Guard clauses ------------------------------------
        if not candidates or top_k <= 0:
            return []

        # NO-OP mode: gateway or model not configured.
        if not self._is_active:
            return list(candidates[:top_k])

        # Reload-safe: re-read the enabled flag from settings.
        if not self._is_rerank_enabled():
            logger.debug(
                "CrossEncoderReranker: rerank_enabled=False in "
                "settings; returning input unchanged."
            )
            return list(candidates[:top_k])

        # Nothing to rerank: single candidate, or empty query.
        if (
            len(candidates) <= 1
            or not isinstance(query, str)
            or not query.strip()
        ):
            return list(candidates[:top_k])

        # ---- Split: scored portion + unscored tail ------------
        #
        # Only the top ``rerank_max_candidates`` entries are sent
        # to the LLM. The tail (candidates[20:]) retains its RRF
        # order and is appended AFTER the reranked set. When
        # ``top_k`` is larger than the scored count, the tail
        # fills the remainder -- so a caller asking for
        # ``top_k=30`` with ``rerank_max_candidates=20`` still
        # gets 30 results (first 20 reranked by LLM, last 10 in
        # RRF order).
        n_to_score = min(len(candidates), self._rerank_max_candidates)
        to_score: List[Tuple[MemoryEntry, float]] = list(
            candidates[:n_to_score]
        )
        tail: List[Tuple[MemoryEntry, float]] = list(
            candidates[n_to_score:]
        )

        # ---- Score each batch (with per-batch timeout) --------
        #
        # ``all_scores[i]`` corresponds to ``to_score[i]``.
        # ``None`` means "batch failed for this slot" -- we
        # immediately fall back to returning the input as-is
        # on the FIRST failure. Partial reranking (some batches
        # succeeded, some failed) is NOT attempted: mixing LLM
        # scores with RRF scores produces an inconsistent
        # ordering that is worse than either alone.
        all_scores: List[Optional[float]] = [None] * n_to_score

        try:
            for batch_start in range(
                0, n_to_score, self._rerank_batch_size
            ):
                batch_end = min(
                    batch_start + self._rerank_batch_size,
                    n_to_score,
                )
                batch: List[Tuple[MemoryEntry, float]] = (
                    to_score[batch_start:batch_end]
                )

                batch_scores = await asyncio.wait_for(
                    self._score_batch(query, batch),
                    timeout=self._timeout_seconds,
                )

                if batch_scores is None or (
                    len(batch_scores) != len(batch)
                ):
                    # Batch failed (gateway error, timeout,
                    # malformed JSON, missing scores).
                    logger.warning(
                        "CrossEncoderReranker: batch %d-%d "
                        "returned unusable scores; falling "
                        "back to RRF ordering for this "
                        "rerank call.",
                        batch_start, batch_end,
                    )
                    return list(candidates[:top_k])

                for local_idx, raw_score in enumerate(batch_scores):
                    all_scores[batch_start + local_idx] = raw_score

        except asyncio.TimeoutError:
            logger.warning(
                "CrossEncoderReranker: rerank timed out after "
                "%.1fs (query=%r). Falling back to RRF ordering.",
                self._timeout_seconds, query[:60],
            )
            return list(candidates[:top_k])

        except asyncio.CancelledError:
            # Caller cancelled (e.g. request aborted). Propagate
            # so the cancellation is visible -- do NOT swallow.
            raise

        except Exception as exc:
            logger.warning(
                "CrossEncoderReranker: rerank raised "
                "unexpectedly (%s: %s). Falling back to RRF "
                "ordering.",
                type(exc).__name__, exc,
            )
            return list(candidates[:top_k])

        # ---- Sanity check: did we get a score for every item? -
        if any(s is None for s in all_scores):
            logger.warning(
                "CrossEncoderReranker: rerank produced "
                "incomplete scores (%d/%d); falling back to "
                "RRF ordering.",
                sum(1 for s in all_scores if s is not None),
                n_to_score,
            )
            return list(candidates[:top_k])

        # ---- Sort: new score desc, original RRF position asc --
        #
        # ``scored`` carries (original_index, entry, new_score).
        # The sort key is a tuple so equal scores preserve the
        # input order (stable). ``original_index`` breaks ties
        # explicitly, which is redundant with stable sort but
        # makes the intent obvious.
        scored: List[Tuple[int, MemoryEntry, float]] = []
        for idx, (entry, _original_score) in enumerate(to_score):
            raw = all_scores[idx]
            # ``raw`` is typed Optional[float] but the sanity
            # check above guarantees non-None. Explicit cast
            # for mypy.
            assert raw is not None
            # Normalize the 0-10 scale to [0, 1]. Clamp
            # defensively in case the model returns an
            # out-of-range value (e.g. 15 for a very relevant
            # match, or -1 for a very irrelevant one).
            normalized = max(0.0, min(1.0, float(raw) / 10.0))
            scored.append((idx, entry, normalized))

        scored.sort(key=lambda item: (-item[2], item[0]))

        # ---- Reassemble: reranked head + original tail --------
        result: List[Tuple[MemoryEntry, float]] = [
            (entry, new_score) for _idx, entry, new_score in scored
        ]
        result.extend(tail)

        # Mark success BEFORE returning -- the caller reads
        # this via ``last_rerank_succeeded``.
        self._last_rerank_succeeded = True

        logger.debug(
            "CrossEncoderReranker: reranked %d candidate(s), "
            "returning top %d (query=%r).",
            len(scored), min(top_k, len(result)), query[:60],
        )

        return result[:top_k]

    # ------------------------------------------------------------------
    # Internal: batch scoring
    # ------------------------------------------------------------------

    async def _score_batch(
        self,
        query: str,
        batch: List[Tuple[MemoryEntry, float]],
    ) -> Optional[List[float]]:
        """Score one batch of candidates via the LLM.

        Returns a list of raw scores (``0-10`` scale) with the
        SAME length and order as ``batch``. Returns ``None`` on
        any failure -- the caller treats ``None`` as "batch
        failed" and falls back to RRF.

        This method NEVER raises (except ``CancelledError`` which
        propagates for correct asyncio semantics). All failure
        modes are logged at WARNING and mapped to ``None``.
        """
        try:
            system_prompt, user_prompt = self._build_prompt(
                query, batch,
            )
            response = await self._gateway.generate_structured(  # type: ignore[union-attr]
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                provider_id=self._provider_id,  # type: ignore[arg-type]
                model_id=self._model_id,  # type: ignore[arg-type]
                api_key=self._api_key,
                base_url=self._base_url,
                max_output_tokens=self._max_output_tokens,
                # The gateway's timeout is intentionally LOOSER
                # than the reranker's own -- our ``asyncio.wait_for``
                # is the authoritative deadline; the gateway's
                # timeout is only a safety net against a runaway
                # transport that ignores our cancellation.
                timeout_seconds=(
                    int(self._timeout_seconds) + 5
                ),
                connection_id=self._connection_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "CrossEncoderReranker: gateway call failed "
                "(%s: %s).",
                type(exc).__name__, exc,
            )
            return None

        parsed = self._extract_parsed(response)
        if parsed is None:
            return None

        scores_raw = parsed.get("scores")
        if not isinstance(scores_raw, list):
            logger.warning(
                "CrossEncoderReranker: LLM response missing "
                "'scores' list (got %r).",
                type(scores_raw).__name__,
            )
            return None

        # ---- Build index -> score map -------------------------
        #
        # The LLM returns an array of {"id": int, "score":
        # number} objects (the reference prompt uses "id"; we
        # ALSO tolerate "index" for backward compatibility with
        # a future prompt variant). We accept:
        #   * extra keys (ignored)
        #   * out-of-order items (map keyed by id/index)
        #   * duplicate ids (last one wins -- defensive)
        # We REJECT:
        #   * missing ids (partial response)
        #   * non-int ids
        #   * non-numeric scores
        #   * out-of-range ids
        index_to_score: Dict[int, float] = {}
        for item in scores_raw:
            if not isinstance(item, dict):
                continue
            idx = item.get("id")
            if idx is None:
                idx = item.get("index")  # tolerant alias
            score = item.get("score")
            if (
                not isinstance(idx, int)
                or isinstance(idx, bool)
                or idx < 0
                or idx >= len(batch)
            ):
                continue
            if not isinstance(score, (int, float)):
                continue
            if isinstance(score, bool):  # bool is int subclass
                continue
            index_to_score[idx] = float(score)

        if len(index_to_score) != len(batch):
            logger.warning(
                "CrossEncoderReranker: incomplete score map "
                "(%d/%d indices covered).",
                len(index_to_score), len(batch),
            )
            return None

        # Return in batch order.
        return [
            index_to_score[i] for i in range(len(batch))
        ]

    @staticmethod
    def _extract_parsed(response: Any) -> Optional[Dict[str, Any]]:
        """Return the parsed JSON dict from a gateway response.

        The gateway returns ``LLMStructuredResponse`` (dataclass
        with ``.parsed``). But this method also accepts:

          * a raw ``dict`` (test mocks often return the parsed
            payload directly),
          * any object with a ``.parsed`` attribute that is a
            ``dict``.

        Returns ``None`` when no usable dict can be extracted.
        """
        if isinstance(response, dict):
            return response
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, dict):
            return parsed
        logger.warning(
            "CrossEncoderReranker: gateway response has no "
            "usable '.parsed' dict (got %r).",
            type(response).__name__,
        )
        return None

    # ------------------------------------------------------------------
    # Internal: prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        query: str,
        batch: List[Tuple[MemoryEntry, float]],
    ) -> Tuple[str, str]:
        """Build the (system, user) prompts for one batch.

        Uses the module-level ``RERANK_SYSTEM_PROMPT`` and
        ``RERANK_PROMPT`` constants (see their docstrings).
        Tests can monkey-patch those constants to inject
        alternate prompt text without touching this method.

        Returns
        -------
        (system_prompt, user_prompt)
            Two strings suitable for ``generate_structured``.
        """
        items_lines: List[str] = []
        for local_idx, (entry, _rrf_score) in enumerate(batch):
            content = (entry.content or "").strip()
            # Collapse newlines/whitespace so a pasted code
            # block or log dump does not derail the model's
            # formatting expectations.
            content = " ".join(content.split())
            if len(content) > _MAX_MEMORY_CONTENT_CHARS:
                content = (
                    content[: _MAX_MEMORY_CONTENT_CHARS - 3] + "..."
                )
            items_lines.append(f"{local_idx}. {content}")

        user_prompt = RERANK_PROMPT.format(
            query=query.strip(),
            items="\n".join(items_lines),
        )
        return RERANK_SYSTEM_PROMPT, user_prompt

    # ------------------------------------------------------------------
    # Internal: enabled-flag resolution
    # ------------------------------------------------------------------

    def _is_rerank_enabled(self) -> bool:
        """Return the CURRENT ``rerank_enabled`` flag.

        - When ``rerank_enabled_provider`` is ``None``, returns
          ``True`` (assume enabled -- matches the case where the
          operator already checked before constructing us).

        - Otherwise calls the provider. On any exception, returns
          ``True`` (fail-open: a broken settings reader must not
          silently disable a feature the operator enabled).

        Reload-safe: the provider is a CALLABLE, so a settings
        change between ``rerank`` calls is observed automatically.
        """
        if self._rerank_enabled_provider is None:
            return True
        try:
            return bool(self._rerank_enabled_provider())
        except Exception as exc:
            logger.warning(
                "CrossEncoderReranker: rerank_enabled_provider "
                "raised (%s: %s); assuming enabled.",
                type(exc).__name__, exc,
            )
            return True

    # ------------------------------------------------------------------
    # Internal: parameter clamping
    # ------------------------------------------------------------------

    @staticmethod
    def _clamp_int(
        value: Any, default: int, *, minimum: int = 1,
    ) -> int:
        """Coerce ``value`` to an int >= ``minimum`` or return ``default``."""
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            return default
        if ivalue < minimum:
            return default
        return ivalue

    @staticmethod
    def _clamp_float(
        value: Any, default: float, *, minimum: float = 0.1,
    ) -> float:
        """Coerce ``value`` to a float >= ``minimum`` or return ``default``."""
        try:
            fvalue = float(value)
        except (TypeError, ValueError):
            return default
        if fvalue < minimum:
            return default
        return fvalue

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """True when this reranker will actually call the LLM.

        False in NO-OP mode (gateway/model not configured).
        Note this does NOT account for the ``rerank_enabled``
        flag -- use ``last_rerank_succeeded`` (post-call) or
        check ``_is_rerank_enabled()`` (pre-call) for that.
        """
        return self._is_active

    @property
    def last_rerank_succeeded(self) -> bool:
        """Whether the MOST RECENT ``rerank`` call actually
        produced LLM-based re-ordering.

        ``True`` when the LLM returned complete, usable scores
        and the result is sorted by those scores.
        ``False`` when the call degraded to RRF ordering
        (timeout, parse failure, disabled, NO-OP, empty input,
        or any other fallback path).

        Read by ``HybridMemoryProvider.search_ranked`` after
        each ``rerank`` call: when True, it sets
        ``_last_search_mode = "hybrid+rerank"``; otherwise it
        keeps ``"hybrid"``. This is the mechanism that makes
        the diagnostic accurate.

        Initialized to ``False``; set to ``True`` at the END
        of a successful ``rerank`` call. Every fallback return
        path leaves it ``False``.

        Concurrency caveat: per-instance, not per-call. See
        the class docstring.
        """
        return self._last_rerank_succeeded

    @property
    def rerank_max_candidates(self) -> int:
        """Max candidates sent to the LLM per rerank call."""
        return self._rerank_max_candidates

    @property
    def rerank_batch_size(self) -> int:
        """Candidates per LLM call."""
        return self._rerank_batch_size

    @property
    def timeout_seconds(self) -> float:
        """Per-batch wall-clock timeout."""
        return self._timeout_seconds

    def __repr__(self) -> str:
        if self._is_active:
            return (
                f"CrossEncoderReranker("
                f"provider={self._provider_id!r}, "
                f"model={self._model_id!r}, "
                f"batch_size={self._rerank_batch_size}, "
                f"max_candidates={self._rerank_max_candidates}, "
                f"timeout={self._timeout_seconds}s)"
            )
        return "CrossEncoderReranker(NO-OP)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CrossEncoderReranker",
    "RerankGatewayProtocol",
    "RERANK_PROMPT",
    "RERANK_SYSTEM_PROMPT",
]
