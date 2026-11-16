# backend/peldrun/memory/retrieval/fusion.py
"""
PELDRUN Memory Platform - Hybrid Search Fusion Strategies.

Two algorithms for merging ranked result lists from heterogeneous
sources (lexical FTS + vector similarity), each with different
properties and failure modes:

  * **Reciprocal Rank Fusion (RRF)** -- rank-based, scale-free.
    Combines lists by summing ``1 / (k + rank)`` for every item.
    Ignores the numeric score entirely; only the POSITION in each
    list matters. Because it never compares raw scores from one
    source to raw scores from another, it works even when the
    two sources produce scores on incompatible scales (BM25
    returns negative unbounded values; cosine similarity lives
    in [0, 1]).

  * **Weighted Fusion** -- score-based, needs normalization.
    Combines lists by min-max normalizing each list's scores into
    [0, 1] and then computing a weighted average. More expressive
    than RRF when both sources are calibrated (e.g., both return
    probabilities), but fragile when they are not: a single
    outlier score in either list can compress every other item
    toward 0 via the min-max floor.

The plan (§5.6) uses RRF as the default for hybrid mode because
the lexical (BM25) and vector (cosine) scores are on incompatible
scales -- RRF's rank-only logic sidesteps the problem without
requiring per-deployment tuning. Weighted fusion is exposed for
callers who have calibrated their sources and want to bias
results toward one of them.

Design invariants
-----------------

1. **Pure functions.**
   No I/O, no state, no imports from ``omweb``. Both algorithms
   take two ranked lists and return one merged list. This makes
   them trivially testable and reusable in Phase 5's reranking
   layer without modification.

2. **Deterministic output.**
   Ties in the fused score are broken by ``id`` ascending. This
   guarantees that the same input produces the same output every
   time -- critical for reproducibility when debugging a search
   result, and for golden-dataset tests (Task 4.12) that compare
   against fixed expected orderings.

3. **Frozen value objects.**
   ``RankedItem`` and ``FusedItem`` are ``frozen=True``. A
   caller that mutates a returned list cannot corrupt the
   caller's own ``RankedItem`` inputs, and a caller that
   accidentally assigns to ``item.score`` gets a fast error
   instead of a silent bug.

4. **Validation at the boundary.**
   ``k >= 1`` (enforced by ``reciprocal_rank_fusion``):
   ``1 / (k + rank)`` with ``k < 0`` can produce zero or
   negative weights, breaking the "higher rank = more score"
   invariant. ``alpha in [0, 1]`` (enforced by
   ``weighted_fusion``): values outside the range would either
   double-count one source or produce negative contributions.

5. **Missing items are penalized, not dropped.**
   An item present in only one source's list gets the
   contribution from that source only. In RRF, this halves its
   score relative to an item at the same rank in BOTH lists. In
   weighted fusion, it halves its score relative to an item with
   normalized score 1.0 in both lists (assuming ``alpha=0.5``).
   The difference is deliberate and matches the algorithms'
   semantics: an item found by both searches is more likely to
   be relevant than an item found by only one.

6. **Duplicates within one list are undefined behavior.**
   Neither list is expected to contain the same ``id`` more than
   once -- FTS and vector search both return unique ids per
   query. If a caller violates this, RRF sums the contributions
   (an item appearing twice in one list gets a higher score --
   arguably correct if "appearing twice" means "found by two
   sub-queries"), while weighted fusion keeps the MAX score (the
   min-max normalizer builds a dict keyed by id, and the last
   duplicate wins; we document this rather than fix it because
   real callers never hit it).

7. **Stable ordering within a fused result.**
   ``FusedItem.score`` reflects the algorithm's raw output, not
   a normalized one. RRF scores are in ``[0, 2 * Σ_{r=1}^{n}
   1/(k+r)]`` -- roughly ``[0, 0.033 * total_items]`` for
   ``k=60``. Weighted scores are in ``[0, 1]``. Callers that
   need a normalized score must normalize it themselves; the
   fusion layer does not impose a scale.

RRF vs weighted: when to use which
----------------------------------

Use **RRF** when:

  * the two sources produce scores on different scales
    (BM25 negative vs cosine [0, 1]) -- which is the case in
    PELDRUN's hybrid mode today;
  * you want the algorithm to work without per-deployment
    tuning -- ``k=60`` is optimal across a wide range of
    datasets per Cormack 2009;
  * you want "both sources found it" to count as strong
    evidence, without either source dominating.

Use **weighted fusion** when:

  * both sources produce scores in a known, comparable range
    (e.g., both return probabilities);
  * you have a reason to prefer one source (e.g., lexical
    exact-match matters more in a code-search deployment);
  * you can afford to tune ``alpha`` per deployment.

The plan sets RRF as the default (§5.6: ``MemoryRetrievalSettings
.rrf_k`` with ``weighted`` available via ``vector_alpha``).

See also
--------

    * ``vector_search.py`` (this package)
        -- the vector source that produces one of the two lists
    * ``ranker.py`` (``peldrun.memory.services``)
        -- the ranker that normalizes BM25 scores; its output
           feeds the lexical side of fusion
    * ``router.py`` (this package, Task 4.9)
        -- the policy layer that decides between RRF and
           weighted fusion based on settings
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

# NOTE: ``fusion.py`` deliberately does NOT import from
# ``vector_search.py``. The two modules share an ``(id, score)``
# shape only conceptually -- ``RankedItem`` here and the
# ``(memory_id, similarity)`` tuples the vector backend returns
# are structurally compatible but not the same type. The
# retrieval router (Task 4.9) adapts one to the other. Keeping
# this module dependency-free makes ``fusion`` trivially
# importable in isolation (tests, ad-hoc analysis, Phase 5
# reranking) without dragging in the vector search stack.


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RankedItem:
    """One item from one source, with its source-specific score.

    Attributes
    ----------
    id
        Unique identifier -- for memory search, a ``memory_id``.
        Not validated here (fusion is source-agnostic); callers
        produce ids from whatever domain they are searching.
    score
        The source's raw score. Meaning depends on the source:

          * lexical (BM25) -- normalized to ``[0, 1]`` by
            ``MemoryRanker`` before reaching this module;
          * vector (cosine similarity) -- already in ``[0, 1]``
            by construction.

        **RRF ignores this field entirely.** RRF uses only the
        ``id`` and its rank (position in the input list). The
        field is present so that the same ``RankedItem`` objects
        can feed either algorithm without conversion.

        **Weighted fusion uses this field.** It is min-max
        normalized per-list before fusion, so the absolute
        magnitude does not need to be meaningful -- only the
        relative order within one list matters.
    """

    id: str
    score: float


@dataclass(frozen=True)
class FusedItem:
    """One item from the fused result, with its combined score.

    Attributes
    ----------
    id
        The same id the input ``RankedItem``s carried.
    score
        The algorithm's raw fused score. Range depends on the
        algorithm:

          * RRF: ``[0, 2 * Σ_{r=1}^{n} 1/(k+r)]``, roughly
            ``[0, 0.033 * n]`` for ``k=60`` where ``n`` is the
            longer input list's length. Higher = better.
          * Weighted fusion: ``[0, 1]``, where 1.0 means "top
            of both lists" and 0.0 means "bottom of both
            lists". Higher = better.

        Callers that need a normalized score for a threshold
        comparison must normalize it themselves -- the fusion
        layer does not impose a canonical scale.
    """

    id: str
    score: float


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------


def reciprocal_rank_fusion(
    lexical: List[RankedItem],
    vector: List[RankedItem],
    k: int = 60,
) -> List[FusedItem]:
    """Combine two ranked lists using Reciprocal Rank Fusion.

    For each item, ``score = Σ_{sources} 1 / (k + rank_source)``
    where ``rank_source`` is the item's 1-based position in that
    source's list. An item absent from a source contributes 0
    from that source.

    The formula (with ``k=60``) is the choice recommended by
    Cormack, Clarke, and Buettcher (2009) after empirically
    testing on TREC datasets. It works because ``1/(k+rank)``
    decays slowly enough that an item ranked 1st in one list and
    5th in another outscores an item ranked 2nd in both -- a
    desirable property called "consensus bonus". The ``k``
    parameter controls how quickly rank matters: larger ``k``
    flattens the curve (rank differences matter less), smaller
    ``k`` makes the top of each list dominate more.

    Parameters
    ----------
    lexical
        The lexical (FTS / BM25) result list, best first. The
        ``score`` field of each ``RankedItem`` is ignored --
        only the order matters.
    vector
        The vector (embedding / cosine) result list, best
        first. Same: only order matters.
    k
        Smoothing constant. Must be >= 1 (validated). Default
        60, per Cormack 2009. Typical range: 30-100.

    Returns
    -------
    List[FusedItem]
        Items sorted by descending fused score. Ties broken by
        ``id`` ascending (deterministic output).

    Raises
    ------
    ValueError
        When ``k < 1``. There is no meaningful interpretation of
        a non-positive ``k``: ``k=0`` collapses to "inverse
        rank" (top result gets 1.0, all others 1/rank), which
        is not what RRF is designed to do; ``k<0`` produces
        zero or negative contributions, breaking the
        higher-rank-means-higher-score invariant.

    Notes
    -----
    Empty inputs are legal:
      * Both empty       -> ``[]``.
      * One empty        -> RRF reduces to single-list ranking
                            (each item still gets ``1/(k+rank)``
                            from its one source).
    """
    if not isinstance(k, int) or isinstance(k, bool):
        raise TypeError(
            f"k must be an int, got {type(k).__name__}"
        )
    if k < 1:
        raise ValueError(
            f"k must be >= 1, got {k}. RRF's smoothing constant "
            f"cannot be non-positive -- it would produce zero or "
            f"negative contributions and break the "
            f"higher-rank-means-higher-score invariant."
        )

    scores: Dict[str, float] = {}
    for rank, item in enumerate(lexical, start=1):
        contribution = 1.0 / (k + rank)
        scores[item.id] = scores.get(item.id, 0.0) + contribution
    for rank, item in enumerate(vector, start=1):
        contribution = 1.0 / (k + rank)
        scores[item.id] = scores.get(item.id, 0.0) + contribution

    return sorted(
        [FusedItem(id=mid, score=s) for mid, s in scores.items()],
        key=lambda x: (-x.score, x.id),
    )


# ---------------------------------------------------------------------------
# Weighted fusion
# ---------------------------------------------------------------------------


def _min_max_normalize(scores: Dict[str, float]) -> Dict[str, float]:
    """Min-max normalize a score map into ``[0, 1]``.

    Every value is mapped to ``(v - min) / (max - min)``. When
    all values are equal (including the single-item case), every
    value maps to ``1.0`` -- there is no "worst" score when all
    scores are the same, so the entire set is treated as the
    top of its list. (An alternative would map them to 0.0,
    which would incorrectly imply "worst" -- neither is
    semantically meaningful, and 1.0 preserves the intuition
    that "these are the only results we have".)

    Negative inputs are handled correctly:
    ``[-5, -3, -1] -> [0.0, 0.5, 1.0]``. This matters because
    BM25 returns negative unbounded scores before the
    ``MemoryRanker`` normalizes them; the normalizer here is a
    backup for callers who pass pre-normalized lists but want
    weighted fusion's arithmetic to be safe regardless.
    """
    if not scores:
        return {}
    values = list(scores.values())
    lo = min(values)
    hi = max(values)
    if hi == lo:
        return {k: 1.0 for k in scores}
    span = hi - lo
    return {k: (v - lo) / span for k, v in scores.items()}


def weighted_fusion(
    lexical: List[RankedItem],
    vector: List[RankedItem],
    alpha: float = 0.5,
) -> List[FusedItem]:
    """Combine two ranked lists using weighted score averaging.

    For each item, ``score = alpha * lexical_norm + (1 - alpha)
    * vector_norm`` where ``*_norm`` is min-max normalized within
    that source's list. An item absent from a source contributes
    0 from that source (equivalent to being at the bottom of the
    list for fusion purposes).

    Parameters
    ----------
    lexical
        The lexical result list. Order matters only in that
        it defines the item set; the numeric scores are what
        get weighted (after normalization).
    vector
        The vector result list. Same: numeric scores matter.
    alpha
        Weight for the lexical source. Must be in ``[0, 1]``
        (validated):
          * ``0.0`` -- pure vector: lexical contributes 0.
          * ``1.0`` -- pure lexical: vector contributes 0.
          * ``0.5`` -- equal weight (default).

    Returns
    -------
    List[FusedItem]
        Items sorted by descending fused score. Ties broken by
        ``id`` ascending (deterministic output).

    Raises
    ------
    ValueError
        When ``alpha`` is outside ``[0, 1]``. A value outside
        that range would either double-count one source
        (``alpha > 1`` makes the sum exceed 1 for items present
        in only one list) or produce negative contributions
        (``alpha < 0``).

    Notes
    -----
    Empty inputs are legal and behave the same as in
    ``reciprocal_rank_fusion``: one empty list reduces the
    calculation to ``alpha * 0 + (1-alpha) * normalized`` (or
    the symmetric case), so weighted fusion gracefully
    degenerates to single-source ranking when only one source
    returns results.
    """
    if (
        not isinstance(alpha, (int, float))
        or isinstance(alpha, bool)
    ):
        raise TypeError(
            f"alpha must be a number, got {type(alpha).__name__}"
        )
    alpha_f = float(alpha)
    if alpha_f < 0.0 or alpha_f > 1.0:
        raise ValueError(
            f"alpha must be in [0.0, 1.0], got {alpha_f}. Values "
            f"outside this range would either double-count one "
            f"source (alpha > 1) or produce negative "
            f"contributions (alpha < 0)."
        )

    lex_norm = _min_max_normalize(
        {item.id: item.score for item in lexical}
    )
    vec_norm = _min_max_normalize(
        {item.id: item.score for item in vector}
    )
    all_ids = set(lex_norm) | set(vec_norm)

    return sorted(
        [
            FusedItem(
                id=mid,
                score=(
                    alpha_f * lex_norm.get(mid, 0.0)
                    + (1.0 - alpha_f) * vec_norm.get(mid, 0.0)
                ),
            )
            for mid in all_ids
        ],
        key=lambda x: (-x.score, x.id),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "FusedItem",
    "RankedItem",
    "reciprocal_rank_fusion",
    "weighted_fusion",
]
