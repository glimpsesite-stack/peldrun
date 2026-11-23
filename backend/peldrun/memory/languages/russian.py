# backend/peldrun/memory/languages/russian.py
"""
PELDRUN Memory Platform - Russian Language Pack.

Migrated verbatim from the three service files that previously
carried inline Russian patterns:

    * ``services/policies.py``   -> ``replacement_markers``,
                                    ``type_hints``.
    * ``services/router.py``     -> ``greeting_patterns``,
                                    ``directed_patterns``,
                                    ``precise_patterns``.

The Russian strings below are REGEX PAYLOAD (matching data), not
prose. Prose comments, identifiers, and log messages in this file
are English-only by policy.

Folding note
------------
``policies._fold`` does NOT transliterate or normalize Cyrillic:
it only lowercases. Russian has no hamza / taa-marbuta /
diacritic variants to unify, so ``re.IGNORECASE`` on the
compiled patterns is sufficient for both the folded
(``replacement_markers``) and the raw (everything else) matching
contexts. A future Russian pattern that depends on case OR
yo-vs-ye normalization (``ё`` / ``е``) must be documented here
and in ``_fold`` together -- this port is intentionally
case-and-yo-agnostic just like the original.

Self-refusal guard: intentionally empty
---------------------------------------
``validator._SELF_REFUSAL_PATTERN`` had NO Russian alternative
in the pre-refactor source. That omission is preserved here as
an empty list, NOT as an oversight:

    * The Russian self-refusal phrasings the live golden set
      exercises ("у меня нет информации о вас", "я не знаю, кто
      вы") were never characterized against a Russian assistant
      corpus.
    * Adding an untested Russian alternative would broaden the
      guard's rejection surface without evidence that it helps
      and with real risk of rejecting legitimate user memories
      ("я не знаю Rust" -- "I don't know Rust" -- is a valid
      fact about the user, and a hasty pattern like
      ``r"я\\s+не\\s+знаю"`` would swallow it).

An empty list compiles to the aggregator's never-match pattern
(``(?!x)x``), so ``self_refusal_patterns=[]`` is exactly
equivalent to "Russian contributes nothing to this guard".
When a characterized Russian corpus is available, fill this
list in a dedicated commit with its own golden tests.
"""

from __future__ import annotations

from peldrun.memory.domain import MemoryType
from peldrun.memory.languages.base import LanguagePack


PACK = LanguagePack(
    code="ru",
    name="Russian",

    # ------------------------------------------------------------------
    # policies.detect_conflict -- matched on FOLDED content.
    #
    # Byte-identical to the Russian alternatives in
    # ``services/policies.py``. Note the [ся]? optional reflexive
    # suffix on ``переключил[ся]?`` -- both "переключил на" and
    # "переключился на" are valid Russian for "switched to", and
    # the original pattern accepted both. Preserve that.
    # ------------------------------------------------------------------
    replacement_markers=[
        r"больше\s+не",                    # "no longer"
        r"изменилось\s+на",                # "changed to" (neuter)
        r"переключил[ся]?\s+на",           # "switched to" (with/without -ся)
        r"вместо",                         # "instead of"
        r"теперь\s+использую",             # "now I use"
    ],

    # ------------------------------------------------------------------
    # policies.classify_memory_type -- matched on RAW content.
    #
    # No ``\b`` boundaries: Cyrillic word segmentation does not
    # map cleanly onto ``\b`` in the same way Latin does for the
    # inflected forms below, and the original inline pattern
    # omitted them deliberately. The port preserves that.
    #
    # ``не\s+любит`` is listed BEFORE ``любит`` inside the
    # PREFERENCE list for readability only -- regex alternation
    # tries alternatives left to right and BOTH ``предпочитает``
    # and ``любит`` independently match a preference statement,
    # so the relative order inside a single language's list does
    # not affect classification (the outer PREFERENCE > DECISION
    # > PROCEDURAL > EPISODIC ordering is what governs priority,
    # and that lives in ``__init__._TYPE_ORDER``).
    # ------------------------------------------------------------------
    type_hints={
        MemoryType.PREFERENCE: [
            r"предпочитает",       # "prefers"
            r"любит",              # "likes"
            r"не\s+любит",         # "dislikes"
            r"всегда",             # "always"
            r"никогда",            # "never"
        ],
        MemoryType.DECISION: [
            r"решили",             # "we decided"
            r"решил",              # "decided" (masc.)
            r"выбрали",            # "we chose"
            r"выбрал",             # "chose" (masc.)
        ],
        MemoryType.PROCEDURAL: [
            r"запустить",          # "to run"
            r"выполнить",          # "to execute"
            r"шаг\s+\d",           # "step N"
            r"сначала",            # "first"
            r"затем",              # "then"
        ],
        MemoryType.EPISODIC: [
            r"вчера",                       # "yesterday"
            r"сегодня",                     # "today"
            r"на\s+прошлой\s+неделе",       # "last week"
            r"случилось",                   # "happened"
            r"произошло",                   # "occurred"
        ],
    },

    # ------------------------------------------------------------------
    # router.classify_route -- matched against RAW content.
    #
    # ``greeting_patterns`` are wrapped by the aggregator with
    # ``^(?:...)\b``. The ``(?:те)?`` optional suffix on
    # ``здравствуй(?:те)?`` covers the informal/formal pair
    # ("здравствуй" / "здравствуйте") exactly as the original did.
    # ------------------------------------------------------------------
    greeting_patterns=[
        r"привет",                 # "hi" (informal)
        r"здравствуй(?:те)?",      # "hello" (informal / formal)
    ],

    # ------------------------------------------------------------------
    # router.classify_route -- searched ANYWHERE in the query.
    #
    # ``кто\s+я`` ("who am I") and ``как\s+меня\s+зовут`` ("what is
    # my name") are identity questions, not recall verbs, and are
    # grouped here because they trigger the same DIRECTED retrieval
    # path -- the original source placed them in this list and the
    # port preserves that grouping.
    # ------------------------------------------------------------------
    directed_patterns=[
        r"помнишь",                              # "do you remember" (informal)
        r"помните",                              # "do you remember" (formal)
        r"что\s+мы",                             # "what did we ..."
        r"какое\s+решение",                      # "what decision ..."
        r"кто\s+я",                              # "who am I"
        r"как\s+меня\s+зовут",                   # "what is my name"
        r"что\s+ты\s+обо\s+мне\s+знаешь",        # "what do you know about me"
    ],

    # ------------------------------------------------------------------
    # router.classify_route -- PRECISE hints, consulted only when the
    # caller has an active project. Without one, these fire nothing.
    # ------------------------------------------------------------------
    precise_patterns=[
        r"в\s+этом\s+проекте",     # "in this project"
        r"в\s+этом\s+файле",       # "in this file"
    ],

    # ------------------------------------------------------------------
    # validator._SELF_REFUSAL_PATTERN -- intentionally empty.
    #
    # See the module docstring: the pre-refactor source never
    # carried a Russian alternative, and an untested alternative
    # is a net negative (broader rejection surface with no
    # evidence of benefit). An empty list compiles to the
    # aggregator's never-match pattern, which is exactly the
    # pre-refactor behavior for Russian input.
    # ------------------------------------------------------------------
    self_refusal_patterns=[],
)


__all__ = ["PACK"]
