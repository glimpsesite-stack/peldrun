# backend/peldrun/memory/languages/english.py
"""
PELDRUN Memory Platform - English Language Pack.

Migrated verbatim from the three service files that previously
carried inline English patterns:

    * ``services/policies.py``   -> ``replacement_markers``,
                                    ``type_hints``.
    * ``services/router.py``     -> ``greeting_patterns``,
                                    ``directed_patterns``,
                                    ``precise_patterns``.
    * ``extraction/validator.py``-> ``self_refusal_patterns``.

No pattern text was altered. Every alternation element below is
byte-identical to its source fragment (same ``\\b`` boundaries,
same non-capturing groups, same order within a category), so the
aggregated ``re.Pattern`` produced by ``languages/__init__.py``
matches exactly what the pre-refactor code matched. This is the
safety property that makes the migration verifiable by
inspection: diff a fragment here against its source, they are
the same string.

Folding note
------------
English is not folded by ``policies._fold`` beyond lowercasing
(there are no hamza / taa-marbuta / diacritic variants to
unify). ``re.IGNORECASE`` on the compiled patterns is therefore
sufficient for both the folded (``replacement_markers``) and
the raw (everything else) matching contexts. Any future English
pattern that depends on case OR diacritic normalization must be
documented here.
"""

from __future__ import annotations

from peldrun.memory.domain import MemoryType
from peldrun.memory.languages.base import LanguagePack


PACK = LanguagePack(
    code="en",
    name="English",

    # ------------------------------------------------------------------
    # policies.detect_conflict -- matched on FOLDED content.
    #
    # An explicit declaration of change ("no longer", "switched to",
    # ...) is the ONLY Phase 1 signal that unlocks SUPERSEDE. See the
    # cost-asymmetry rationale in ``services/policies.py``: a missed
    # conflict is recoverable by the user, a spurious supersede is
    # silent data loss. The list is therefore deliberately narrow --
    # only phrases the user themselves uses to describe a transition.
    # ------------------------------------------------------------------
    replacement_markers=[
        r"no\s+longer",
        r"changed\s+to",
        r"switched\s+to",
        r"updated\s+to",
        r"moved\s+to",
        r"now\s+use[sd]?",
        r"instead\s+of",
        r"rather\s+than",
        r"replaced\s+(?:\S+\s+)?with",
        r"used\s+to",
    ],

    # ------------------------------------------------------------------
    # policies.classify_memory_type -- matched on RAW content.
    #
    # Every alternative keeps its original ``\b`` word boundaries.
    # Removing them would let "always" match inside "alwayson" and
    # "hates" match inside "hatespeech" -- a real false-positive path
    # the original inline pattern deliberately closed. The aggregator
    # preserves the outer PREFERENCE > DECISION > PROCEDURAL >
    # EPISODIC ordering (see ``_TYPE_ORDER`` in ``__init__.py``);
    # SEMANTIC is absent because it is the caller-side fallback and
    # has no pattern of its own.
    # ------------------------------------------------------------------
    type_hints={
        MemoryType.PREFERENCE: [
            r"\bprefers?\b",
            r"\blikes?\b",
            r"\bwants?\b",
            r"\bdislikes?\b",
            r"\bhates?\b",
            r"\bfavou?rs?\b",     # accepts both "-our" and "-or"
            r"\balways\b",
            r"\bnever\b",
        ],
        MemoryType.DECISION: [
            r"\bdecided\b",
            r"\bchose\b",
            r"\bchosen\b",
            r"\bgoing\s+with\b",
            r"\bwe\s+will\s+use\b",
            r"\bselected\b",
            r"\bsettled\s+on\b",
        ],
        MemoryType.PROCEDURAL: [
            r"\brun\s+`",
            r"\bexecute\s+`",
            r"`\w+\s+[^`]+`",     # inline code block: `uv sync`
            r"\bstep\s+\d",
            r"\bfirst\b.*\bthen\b.*\bfinally\b",
        ],
        MemoryType.EPISODIC: [
            r"\b(?:yesterday|today|last\s+(?:week|month|year))\b",
            r"\b\d{4}-\d{2}-\d{2}\b",                 # ISO date
            r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{4}\b",     # 2025-11-23 / 23/11/2025
            r"\b(?:happened|occurred|was\s+deployed|failed|succeeded)\b",
        ],
    },

    # ------------------------------------------------------------------
    # router.classify_route -- matched against RAW content.
    #
    # ``greeting_patterns`` are wrapped by the aggregator with
    # ``^(?:...)\b`` so that "hello, remember when..." falls
    # through to the directed check instead of being swallowed as
    # a greeting. Bare "hi" here, therefore, and NOT "^hi" -- the
    # anchor belongs to the aggregator, not to the language pack.
    # ------------------------------------------------------------------
    greeting_patterns=[
        r"hi",
        r"hello",
        r"hey",
        r"yo",
        r"sup",
    ],

    # ------------------------------------------------------------------
    # router.classify_route -- searched ANYWHERE in the query.
    #
    # A directed request can be preceded by small talk ("before we
    # continue, please remember that X"); anchoring would miss it.
    # These carry no ``\b`` for the same reason the original did not
    # add them: "remembering" and "recalls" are still recall verbs,
    # and the classification returns a Route for the dispatcher to
    # act on, not a verbatim confirmation.
    # ------------------------------------------------------------------
    directed_patterns=[
        r"remember",
        r"recall",
        r"do\s+you\s+know",
        r"did\s+(?:we|I)",
        r"what\s+(?:did\s+we|was\s+the|is\s+my|are\s+my|do\s+I)",
        r"who\s+am\s+I",
        r"who\s+is\s+the\s+user",
        r"my\s+(?:name|role|preferences?|background)",
    ],

    # ------------------------------------------------------------------
    # router.classify_route -- PRECISE hints, consulted only when the
    # caller has an active project. Without one, these fire nothing:
    # there is no project scope to narrow TO.
    # ------------------------------------------------------------------
    precise_patterns=[
        r"in\s+this\s+(?:project|repo|repository|workspace)",
        r"the\s+file",
        r"this\s+codebase",
    ],

    # ------------------------------------------------------------------
    # validator._SELF_REFUSAL_PATTERN -- the assistant self-poisoning
    # guard. See the "feedback loop" comment in
    # ``extraction/validator.py`` for why these four alternatives
    # exist and why they are narrowly anchored on knowledge-of-the-
    # user phrasing.
    #
    # NOTE: "I don't know Rust" (the USER speaking about a skill) is
    # NOT matched because the second and third alternatives require
    # "information/info/details/knowledge" or "you/who you are" after
    # the verb. Do not broaden these without re-running the golden
    # validator tests -- a broadened pattern that rejects legitimate
    # user memories is the failure mode this guard is calibrated
    # against.
    # ------------------------------------------------------------------
    self_refusal_patterns=[
        # "I don't have any other personal information about you"
        # "I do not know any further details"
        r"(?:i\s+)?(?:do\s+not|don'?t)\s+(?:have|know)\s+(?:any\s+)?"
        r"(?:other\s+|further\s+|more\s+)?"
        r"(?:personal\s+)?(?:information|info|details|data|knowledge)",

        # "I have no information about you"
        r"(?:i\s+)?have\s+no\s+(?:other\s+|further\s+|more\s+)?"
        r"(?:personal\s+)?(?:information|info|details|knowledge)",

        # "I don't know you" / "I do not know who you are"
        r"(?:i\s+)?(?:do\s+not|don'?t)\s+know\s+(?:you|who\s+you\s+are)",

        # "As an AI ...", "As a language model ..."
        r"\bas\s+an?\s+(?:ai|artificial\s+intelligence|language\s+model)\b",
    ],
)


__all__ = ["PACK"]
