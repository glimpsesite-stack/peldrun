# backend/peldrun/memory/languages/base.py
"""
PELDRUN Memory Platform - Language Pack Contract.

Single source of truth for every language-specific regex
alternative used by the memory services. Adding a new language
is a three-step operation that touches no service code:

    1. Create ``<code>.py`` in this package exporting ``PACK``.
    2. Register it in ``LANGUAGES`` inside ``__init__.py``.
    3. Run ``pytest tests/memory/languages/``.

Public surface
--------------
    ``LanguagePack``  -- frozen value object, one per language.
    ``LanguagePack.__post_init__`` -- validates every pattern at
    import time (fail fast).

Why a dataclass and not a plain module-level dict
-------------------------------------------------
Each language module exports exactly one ``LanguagePack``. Keeping
the contract in a typed, frozen dataclass means:

    * Every field is IDE-complete and mypy-checked at the call
      site in the aggregator.
    * A missing field is a TypeError at import, not a silent
      ``None`` fallback hours later at request time.
    * The fields are self-documenting: the dataclass IS the
      specification of what a language can contribute.

Raw vs folded content -- the one invariant to get right
-------------------------------------------------------
Two matching contexts exist in the memory services and they
must NOT be confused:

  * ``replacement_markers`` are matched against **folded**
    content (see ``policies._fold``): Arabic hamza/alef
    variants unified, taa-marbuta -> haa, alef-maqsura -> yaa,
    tatweel and diacritics stripped, lowercased. Write Arabic
    markers WITHOUT diacritics and using the FOLDED letter
    forms (e.g. ``لم اعد`` not ``لم أعد``), otherwise
    ``detect_conflict`` will never see them.

  * Every other field (``type_hints``, ``greeting_patterns``,
    ``directed_patterns``, ``precise_patterns``,
    ``self_refusal_patterns``) is matched against **raw**
    content. Write them in the orthography a user is likely to
    actually type.

Mixing the two produces silent false negatives -- a marker
that only fires when the user happened to spell a hamza in a
specific way, or a greeting that never matches because it was
folded into an unrecognizable form. Each per-language module
carries a short header comment that restates the fold for its
own markers so a future contributor cannot overlook it.

Validation policy -- fail at import, never at request
-----------------------------------------------------
``__post_init__`` compiles every regex once, at module import
time, and raises ``ValueError`` with a precise locator
(language code, field name, list index, underlying
``re.error``) on any failure. A typo in a pattern must crash
the process at startup, not the one request that happens to
exercise it in production. Startup failure is cheap and
visible; a mid-request ``re.error`` is neither.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from peldrun.memory.domain import MemoryType


@dataclass(frozen=True)
class LanguagePack:
    """Every language-specific pattern the memory services use.

    One instance per language, defined in the corresponding
    ``<code>.py`` module and registered in ``__init__.LANGUAGES``.
    Frozen so that no caller can mutate a pack after the
    aggregator has compiled it.

    Attributes
    ----------
    code
        BCP-47 language code (``"en"``, ``"ar"``, ``"ru"``).
        Used as the registry key; MUST be unique across packs.
    name
        Human-readable display name (``"English"``,
        ``"العربية"``, ``"Русский"``). Diagnostics only --
        never parsed.
    replacement_markers
        Alternation fragments for ``policies._REPLACEMENT_MARKERS``.
        Matched against FOLDED content (see module docstring).
        A candidate containing any of these fragments is treated
        as an EXPLICIT declaration of change, which is the only
        Phase 1 signal that unlocks ``SUPERSEDE``.
    type_hints
        ``MemoryType`` -> list of alternation fragments for
        ``policies._TYPE_HINTS``. Matched against RAW content.
        The aggregator preserves the order
        ``PREFERENCE > DECISION > PROCEDURAL > EPISODIC`` (see
        ``_TYPE_ORDER`` in ``__init__.py``); SEMANTIC has no
        entry because it is the caller-side fallback.
    greeting_patterns
        Alternation fragments for ``router._GREETING_PATTERN``.
        Anchored at start-of-string with a trailing ``\\b`` by
        the aggregator, so list bare greetings (``"hi"``) not
        prefixed anchors. Matched against RAW content.
    directed_patterns
        Alternation fragments for ``router._DIRECTED_PATTERN``.
        Searched anywhere in the query. Matched against RAW
        content.
    precise_patterns
        Alternation fragments for ``router._PRECISE_PATTERN``.
        Only consulted when the caller has an active project.
        Matched against RAW content.
    self_refusal_patterns
        Alternation fragments for
        ``validator._SELF_REFUSAL_PATTERN`` -- the assistant's
        "I don't know anything about you" guard. Matched
        against RAW snippet text. Empty list is a legal value
        (a language whose assistant-refusal phrasing is not
        yet characterized).
    """

    code: str
    name: str
    replacement_markers: List[str] = field(default_factory=list)
    type_hints: Dict[MemoryType, List[str]] = field(default_factory=dict)
    greeting_patterns: List[str] = field(default_factory=list)
    directed_patterns: List[str] = field(default_factory=list)
    precise_patterns: List[str] = field(default_factory=list)
    self_refusal_patterns: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate every field at import time.

        Raises
        ------
        ValueError
            When ``code`` or ``name`` is empty / non-str, when a
            ``type_hints`` key is not a ``MemoryType``, when any
            pattern element is not a ``str``, or when any
            pattern element does not compile as a regex. The
            error message always names the language code, the
            offending field, and the failing index so the
            contributor fixes the right file.
        """
        if not isinstance(self.code, str) or not self.code:
            raise ValueError(
                f"LanguagePack: code must be a non-empty str, "
                f"got {self.code!r}"
            )
        if not isinstance(self.name, str) or not self.name:
            raise ValueError(
                f"[{self.code}] name must be a non-empty str, "
                f"got {self.name!r}"
            )

        self._validate_pattern_field(
            "replacement_markers", self.replacement_markers
        )
        self._validate_pattern_field(
            "greeting_patterns", self.greeting_patterns
        )
        self._validate_pattern_field(
            "directed_patterns", self.directed_patterns
        )
        self._validate_pattern_field(
            "precise_patterns", self.precise_patterns
        )
        self._validate_pattern_field(
            "self_refusal_patterns", self.self_refusal_patterns
        )

        for mtype, parts in self.type_hints.items():
            if not isinstance(mtype, MemoryType):
                raise ValueError(
                    f"[{self.code}] type_hints key must be a "
                    f"MemoryType, got {mtype!r}"
                )
            self._validate_pattern_field(
                f"type_hints[{mtype.value}]", parts
            )

    def _validate_pattern_field(
        self, field_name: str, patterns: List[str]
    ) -> None:
        """Compile every pattern in ``patterns`` or raise.

        Kept as a private helper so the six call sites above stay
        readable. Never called outside ``__post_init__``.
        """
        for index, part in enumerate(patterns):
            if not isinstance(part, str):
                raise ValueError(
                    f"[{self.code}] {field_name}[{index}] must be "
                    f"str, got {type(part).__name__}"
                )
            try:
                re.compile(part)
            except re.error as exc:
                raise ValueError(
                    f"[{self.code}] {field_name}[{index}] invalid "
                    f"regex {part!r}: {exc}"
                ) from exc


__all__ = ["LanguagePack"]
