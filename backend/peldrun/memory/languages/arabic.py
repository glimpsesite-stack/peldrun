# backend/peldrun/memory/languages/arabic.py
"""
PELDRUN Memory Platform - Arabic Language Pack.

Migrated verbatim from the three service files that previously
carried inline Arabic patterns:

    * ``services/policies.py``   -> ``replacement_markers``,
                                    ``type_hints``.
    * ``services/router.py``     -> ``greeting_patterns``,
                                    ``directed_patterns``,
                                    ``precise_patterns``.
    * ``extraction/validator.py``-> ``self_refusal_patterns``.

The Arabic strings below are REGEX PAYLOAD (matching data), not
prose. They identify Arabic-language user input and must remain
in their original orthography. Prose comments, identifiers,
and log messages in this file are English-only by policy.

Folding note -- the load-bearing invariant
------------------------------------------
``replacement_markers`` are matched against FOLDED content
(``services/policies._fold``): Arabic hamza/alef variants
unified to plain alef, taa-marbuta -> haa, alef-maqsura ->
yaa, tatweel and diacritics stripped, lowercased. The markers
below are therefore written WITHOUT diacritics and using the
FOLDED letter forms:

    user types          folds to            marker stored here
    -------------       ---------           -------------------
    لم أعد              لم اعد              r"لم\\s+اعد"
    بدلاً من            بدلا من             r"بدلا?\\s+من"
    أصبحت               اصبحت               r"اصبحت?"
    انتقلت إلى          انتقلت الي          r"انتقلت\\s+(?:الي|الى)"

Writing a marker in its unfolded form (e.g. ``r"لم\\s+أعد"``
with hamza) would NEVER match because the candidate content has
already been folded before the pattern sees it. This is the
single most common mistake when extending this file.

Every OTHER field (``type_hints``, ``greeting_patterns``,
``directed_patterns``, ``precise_patterns``,
``self_refusal_patterns``) is matched against RAW content, so
those patterns keep their hamza / alef / taa-marbuta forms
exactly as users type them, and include BOTH hamza and
non-hamza spellings wherever the original source did so.

Latin transliteration
---------------------
``salam``, ``assalam``, and ``salaam`` were previously part of
the English greeting pattern in ``services/router.py``. They
live here instead: they are Arabic greetings written in Latin
script, used by Arabic speakers typing without an Arabic
keyboard. Grouping them with the Arabic pack keeps the English
pack purely English and the Arabic pack "the complete Arabic
dictionary" (Arabic script plus its Latin-script
transliterations).
"""

from __future__ import annotations

from peldrun.memory.domain import MemoryType
from peldrun.memory.languages.base import LanguagePack


PACK = LanguagePack(
    code="ar",
    name="Arabic",

    # ------------------------------------------------------------------
    # policies.detect_conflict -- matched on FOLDED content.
    #
    # These are the Arabic short-form change markers the golden
    # update set uses. Each alternative below is byte-identical to
    # its source fragment in ``services/policies.py`` -- including
    # the redundant ``الى`` alternative inside ``(?:الي|الى)``:
    # after ``_fold`` the candidate can never contain ``الى``
    # (alef-maqsura is mapped to yaa), so ``الى`` is unreachable
    # at match time. It is preserved here anyway to keep this
    # migration a byte-for-byte port; removing it would be a
    # separate cleanup that requires re-validating against the
    # golden set.
    # ------------------------------------------------------------------
    replacement_markers=[
        r"لم\s+اعد",                       # "no longer" (masc. speaker)
        r"لا\s+اعد",                       # "no longer" (variant)
        r"لم\s+يعد",                       # "no longer" (masc. subject)
        r"لم\s+تعد",                       # "no longer" (fem. subject)
        r"بدلا?\s+من",                     # "instead of"
        r"عوضا?\s+عن",                     # "instead of" (variant)
        r"اصبحت?",                         # "became" / "has become"
        r"صرت",                            # "I have become"
        r"انتقلت\s+(?:الي|الى)",           # "I moved to"
        r"غيرت",                           # "I changed"
        r"تغير(?:ت)?\s+(?:الي|الى)",       # "changed to"
        r"تحولت\s+(?:الي|الى)",            # "switched to"
        r"الان",                           # "now"
        r"حاليا",                          # "currently"
    ],

    # ------------------------------------------------------------------
    # policies.classify_memory_type -- matched on RAW content.
    #
    # No ``\b`` boundaries: Arabic word segmentation does not map
    # cleanly onto ``\b`` because every Arabic letter is ``\w``,
    # so a boundary only fires between an Arabic letter and a
    # non-letter. The original inline patterns omitted ``\b`` for
    # this reason and the port preserves that choice exactly.
    #
    # Both hamza and non-hamza forms are NOT duplicated here --
    # the classifier runs on RAW text but ``re.IGNORECASE`` does
    # not help with hamza. The original source listed only the
    # hamza form for PREFERENCE/EPISODIC; port it verbatim.
    # ------------------------------------------------------------------
    type_hints={
        MemoryType.PREFERENCE: [
            r"أفضل",       # "prefer" (1st person)
            r"يفضل",       # "prefer" (3rd masc.)
            r"تفضل",       # "prefer" (3rd fem. / 2nd masc.)
            r"يحب",        # "like" (3rd masc.)
            r"تحب",        # "like" (3rd fem.)
            r"يكره",       # "hate" (3rd masc.)
            r"تكره",       # "hate" (3rd fem.)
            r"دائماً",     # "always"
            r"أبداً",      # "never"
        ],
        MemoryType.DECISION: [
            r"قررنا",              # "we decided"
            r"قرر",                # "decided"
            r"اخترنا",             # "we chose"
            r"اختر",               # "chose"
            r"سنستخدم",            # "we will use"
            r"وقع\s+الاختيار",     # "was selected"
        ],
        MemoryType.PROCEDURAL: [
            r"شغل",                # "run" (imperative)
            r"نفذ",                # "execute"
            r"الخطوة",             # "step"
            r"أولاً",              # "first"
            r"ثم",                 # "then"
            r"أخيراً",             # "finally"
        ],
        MemoryType.EPISODIC: [
            r"أمس",                    # "yesterday"
            r"اليوم",                  # "today"
            r"الأسبوع\s+الماضي",       # "last week"
            r"حدث",                    # "happened"
            r"فشل",                    # "failed"
            r"نجح",                    # "succeeded"
        ],
    },

    # ------------------------------------------------------------------
    # router.classify_route -- matched against RAW content.
    #
    # ``greeting_patterns`` are wrapped by the aggregator with
    # ``^(?:...)\b``. That is why the Arabic greetings below carry
    # no ``^`` or ``\b`` of their own. The ``\s*`` inside
    # ``السلام\s*عليكم`` tolerates the no-space spelling common in
    # casual chat ("السلامعليكم").
    #
    # The three Latin transliterations (``salam``, ``assalam``,
    # ``salaam``) moved here from the English pack -- see the
    # module docstring for the rationale.
    # ------------------------------------------------------------------
    greeting_patterns=[
        r"مرحبا",                  # "welcome" / "hello"
        r"أهلا",                   # "welcome" (hamza form)
        r"اهلا",                   # "welcome" (non-hamza)
        r"السلام\s*عليكم",         # "peace be upon you"
        r"salam",
        r"assalam",
        r"salaam",
    ],

    # ------------------------------------------------------------------
    # router.classify_route -- searched ANYWHERE in the query.
    #
    # Note the double spelling coverage on ``أنا``/``اسمي``/``عني``:
    # these are matched RAW, and Arabic speakers routinely type the
    # initial alef without hamza on mobile keyboards. The original
    # source included both forms only where it mattered (the recall
    # verbs themselves are unambiguous enough that only one form
    # was needed).
    # ------------------------------------------------------------------
    directed_patterns=[
        r"تذكر",                         # "remember" (2nd masc.)
        r"تتذكر",                        # "do you remember"
        r"هل\s+تذكر",                    # "do you remember" (quest.)
        r"ما\s+القرار",                  # "what is the decision"
        r"ما\s+قررنا",                   # "what did we decide"
        r"من\s+أنا",                     # "who am I"
        r"ما\s+اسمي",                    # "what is my name"
        r"ماذا\s+تعرف\s+عني",            # "what do you know about me"
        r"ما\s+دوري",                    # "what is my role"
        r"ما\s+خلفيتي",                  # "what is my background"
    ],

    # ------------------------------------------------------------------
    # router.classify_route -- PRECISE hints, consulted only when the
    # caller has an active project. Without one, these fire nothing.
    # ------------------------------------------------------------------
    precise_patterns=[
        r"هذا\s+المشروع",        # "this project"
        r"هذه\s+المحفظة",        # "this repository"
        r"الملف",                # "the file"
    ],

    # ------------------------------------------------------------------
    # validator._SELF_REFUSAL_PATTERN -- the assistant self-poisoning
    # guard. Matched against RAW snippet text. Both hamza and
    # non-hamza spellings appear inside a single alternative, exactly
    # as in the original source, because a model reply may echo the
    # user's mobile-keyboard input.
    #
    # Do NOT broaden without re-running the golden validator tests:
    # this guard must not reject a legitimate user memory such as
    # "I don't know Rust" (in Arabic: "لا أعرف رست" -- which the
    # second alternative deliberately does NOT match because it
    # requires a pronoun-of-address such as ``انت`` / ``عنك`` after
    # the verb).
    # ------------------------------------------------------------------
    self_refusal_patterns=[
        # "I have no information / data / details"
        r"ل(?:ا|يس)\s+(?:لدي|عندي)\s+(?:اي\s+)?"
        r"(?:معلومات|بيانات|تفاصيل)",

        # "I don't know you / about you / who you are"
        r"لا\s+(?:اعرف|أعرف)\s+(?:من\s+)?(?:انت|أنت|عنت|عنك)",
    ],
)


__all__ = ["PACK"]
