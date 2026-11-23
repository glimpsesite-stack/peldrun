# backend/peldrun/memory/languages/__init__.py
"""
PELDRUN Memory Platform - Language Pack Aggregator.

Compiles every registered ``LanguagePack`` into the single
``re.Pattern`` objects consumed by the memory services. The
services import from THIS module, not from the per-language
files -- so adding a language is invisible to them.

Public surface
--------------
    ``LanguagePack``          -- re-exported from ``base``.
    ``LANGUAGES``             -- ordered tuple of registered packs.
    ``LANGUAGE_BY_CODE``      -- lookup by BCP-47 code.
    ``REPLACEMENT_MARKERS``   -- ``policies.detect_conflict`` signal.
    ``TYPE_HINTS``            -- ``policies.classify_memory_type`` signal.
    ``GREETING_PATTERN``      -- ``router.classify_route`` signal.
    ``DIRECTED_PATTERN``      -- ``router.classify_route`` signal.
    ``PRECISE_PATTERN``       -- ``router.classify_route`` signal.
    ``SELF_REFUSAL_PATTERN``  -- ``validator`` self-poisoning guard.

How to add a new language
-------------------------
    1. Create ``<code>.py`` in this package exporting ``PACK``.
    2. Add ``from peldrun.memory.languages.<code> import PACK as _<CODE>``
       to the registry block below.
    3. Add ``_<CODE>`` to the ``LANGUAGES`` tuple.
    4. Run ``pytest tests/memory/languages/``.

No change to ``policies.py``, ``router.py``, or ``validator.py``
is ever required for a new language. That is the entire point
of this package -- the alternative (inline patterns in three
files) diverges the moment one file is updated and another is
forgotten, and the resulting bug is invisible until a user
writes a memory in the forgotten language.

Ordering invariant
------------------
``LANGUAGES`` order determines the alternation order in the
compiled patterns. ``re.search`` finds the LEFTMOST match, not
the first alternative that happens to match -- so ordering
matters only when two alternatives can start at the SAME
position, which is rare across languages (they start with
different characters). The order below is ``EN, AR, RU`` --
the exact source order in the pre-refactor files -- so the
migration is byte-for-byte regardless of pattern-order
semantics.

Fail-fast contract
------------------
A malformed pattern in any pack raises ``ValueError`` when the
pack module is imported (via ``LanguagePack.__post_init__``),
which means importing THIS module fails before any service
starts serving traffic. A typo in a regex must crash startup,
not a single request.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from peldrun.memory.domain import MemoryType
from peldrun.memory.languages.base import LanguagePack


# ---------------------------------------------------------------------------
# Registry -- imports happen at module load, so a malformed pack
# fails HERE, before any service code runs.
# ---------------------------------------------------------------------------

from peldrun.memory.languages.english import PACK as _EN
from peldrun.memory.languages.arabic  import PACK as _AR
from peldrun.memory.languages.russian import PACK as _RU


#: Ordered tuple of every registered pack. The order is
#: load-bearing only insofar as it preserves the pre-refactor
#: source order (see module docstring).
LANGUAGES: Tuple[LanguagePack, ...] = (_EN, _AR, _RU)

#: Fast lookup by BCP-47 code. Used by diagnostics and by the
#: integrity test to verify the registry has no duplicate codes.
LANGUAGE_BY_CODE: Dict[str, LanguagePack] = {
    pack.code: pack for pack in LANGUAGES
}


# ---------------------------------------------------------------------------
# Compilation helper
# ---------------------------------------------------------------------------


#: Pattern that can never match. Returned by ``_compile_any`` when
#: the union of all languages' patterns is empty -- e.g. Russian's
#: ``self_refusal_patterns`` in the current registry. Using a real
#: never-match object (rather than ``None``) keeps every caller's
#: code path identical: they always get an ``re.Pattern``, always
#: call ``.search`` or ``.match``, and the "no patterns registered"
#: case is handled by the pattern itself.
_NEVER_MATCH: re.Pattern[str] = re.compile(r"(?!x)x")


def _compile_any(
    parts: List[str],
    *,
    anchored_start_word: bool = False,
) -> re.Pattern[str]:
    """Compile ``parts`` into one ``re.IGNORECASE`` alternation.

    Parameters
    ----------
    parts
        Regex fragments. Each fragment carries its own ``\\b`` or
        other boundary markers if it needs them; this helper adds
        only the outer grouping and the case flag.
    anchored_start_word
        When True, wraps the alternation with ``^(?:...)\\b`` --
        the exact shape of the pre-refactor
        ``router._GREETING_PATTERN``. Only ``GREETING_PATTERN``
        sets this flag.

    Returns
    -------
    re.Pattern[str]
        A single compiled pattern matching ANY of ``parts``.
        When ``parts`` is empty, returns ``_NEVER_MATCH`` -- a
        valid pattern object that simply never matches anything.
    """
    if not parts:
        return _NEVER_MATCH
    body = "|".join(parts)
    if anchored_start_word:
        return re.compile(rf"^(?:{body})\b", re.IGNORECASE)
    return re.compile(rf"(?:{body})", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Aggregated public patterns
# ---------------------------------------------------------------------------


#: Matched against FOLDED content. See ``policies.detect_conflict``.
REPLACEMENT_MARKERS: re.Pattern[str] = _compile_any(
    [marker for pack in LANGUAGES for marker in pack.replacement_markers]
)


#: Priority order for type classification. This tuple is the
#: SINGLE source of truth for "which MemoryType wins when text
#: matches more than one". Each language contributes its own
#: fragments per type; the priority ACROSS types is decided here,
#: not in the language files. Keeping it here means a new language
#: can add ``type_hints[MemoryType.EPISODIC]`` without needing to
#: reason about where EPISODIC sits relative to PREFERENCE.
_TYPE_ORDER: Tuple[MemoryType, ...] = (
    MemoryType.PREFERENCE,
    MemoryType.DECISION,
    MemoryType.PROCEDURAL,
    MemoryType.EPISODIC,
)


#: Matched against RAW content. See ``policies.classify_memory_type``.
#: The list order encodes priority (first match wins), matching
#: the pre-refactor ``policies._TYPE_HINTS`` order exactly.
TYPE_HINTS: List[Tuple[re.Pattern[str], MemoryType]] = [
    (
        _compile_any([
            hint
            for pack in LANGUAGES
            for hint in pack.type_hints.get(memory_type, [])
        ]),
        memory_type,
    )
    for memory_type in _TYPE_ORDER
]


#: Matched against RAW content, anchored at start-of-string. See
#: ``router.classify_route``.
GREETING_PATTERN: re.Pattern[str] = _compile_any(
    [greeting for pack in LANGUAGES for greeting in pack.greeting_patterns],
    anchored_start_word=True,
)


#: Matched against RAW content, searched anywhere. See
#: ``router.classify_route``.
DIRECTED_PATTERN: re.Pattern[str] = _compile_any(
    [pattern for pack in LANGUAGES for pattern in pack.directed_patterns]
)


#: Matched against RAW content. See ``router.classify_route``.
PRECISE_PATTERN: re.Pattern[str] = _compile_any(
    [pattern for pack in LANGUAGES for pattern in pack.precise_patterns]
)


#: Matched against RAW snippet text. See
#: ``validator._SELF_REFUSAL_PATTERN``.
SELF_REFUSAL_PATTERN: re.Pattern[str] = _compile_any(
    [pattern for pack in LANGUAGES for pattern in pack.self_refusal_patterns]
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "LanguagePack",
    "LANGUAGES",
    "LANGUAGE_BY_CODE",
    "REPLACEMENT_MARKERS",
    "TYPE_HINTS",
    "GREETING_PATTERN",
    "DIRECTED_PATTERN",
    "PRECISE_PATTERN",
    "SELF_REFUSAL_PATTERN",
]
