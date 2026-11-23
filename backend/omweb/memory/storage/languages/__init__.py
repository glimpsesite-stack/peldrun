# backend/omweb/memory/storage/languages/__init__.py
"""
PELDRUN Memory Platform - Language Profile Discovery.

Discovers every language profile in this package at import time and
exposes the resulting ordered tuple as ``PROFILES``. The discovery
runs exactly once, when ``normalizer.py`` (or anything else) first
imports this package. There is no lazy loading and no re-scan on
demand -- the goal is to fail loudly at application startup if a
language file is malformed, rather than to raise a confusing error
during the first user query.

Discovery rules
---------------

1. ``pkgutil.iter_modules`` enumerates every ``*.py`` file and
   sub-package in this directory, including already-imported ones.

2. Files whose name starts with an underscore are SKIPPED. This is
   what excludes ``_base.py`` (the Protocol + ABC definitions) from
   being treated as a profile. The convention is universal across
   Python plugin systems.

3. Every remaining module is imported. Its module-level marker
   ``IS_PROFILE_MODULE`` is inspected:

     * ``False``         -> module opts out (e.g. ``common.py``,
                            which is a shared utility, not a language).
     * ``True`` (default) -> module is a profile and MUST expose
                            a ``PROFILE`` attribute.

   The default-to-True direction is deliberate: a contributor who
   adds ``turkish.py`` and forgets to assign ``PROFILE`` gets a
   clear ``ImportError`` at startup, rather than a silently missing
   language that they only notice when their Turkish query returns
   nothing. The alternative -- default-to-False -- would require
   every language file to opt IN, which contradicts the "add a
   language by adding a file" promise.

4. Each discovered ``PROFILE`` is validated against the
   ``LanguageProfile`` protocol via ``isinstance``. Runtime protocol
   checks in ``typing`` verify method NAMES and attribute presence,
   not signatures -- sufficient to catch typos like ``stopword``
   (singular) or ``normlize``, but not signature drift. Signature
   drift is caught by the compat test.

5. ``PROFILE.code`` must be a non-empty string. ``PROFILE.code``
   collisions across modules are treated as a fatal error, because
   ``by_code`` lookups would otherwise return an arbitrary profile
   depending on import order.

6. The final tuple is sorted by ``(priority, code)``. This is what
   the module docstring of ``_base.py`` refers to as the
   deterministic ordering guarantee. Two profiles with the same
   priority are ordered lexicographically by ``code`` -- a stable,
   reviewable tiebreaker.

Non-profile modules
-------------------

``languages/common.py`` is the only current non-profile module. It
exports ``normalize_common`` (the shared tail of the pipeline) and
declares ``IS_PROFILE_MODULE = False`` so discovery skips it.

Future shared utilities (e.g. a stemming helper for a language
family, a shared check that scans multiple scripts) MUST follow the
same pattern. They belong in this package only if they logically
relate to language handling -- and they MUST declare the opt-out
marker, or discovery will fail at startup with a clear error.

Failure modes
-------------

Every failure mode below raises at import time, before any user
request touches the pipeline:

  * Missing ``PROFILE`` in a non-opt-out module -> ``ImportError``.
  * ``PROFILE`` fails ``LanguageProfile`` isinstance -> ``TypeError``.
  * Empty ``PROFILE.code`` -> ``ValueError``.
  * Duplicate ``PROFILE.code`` -> ``ValueError`` with both module
    names in the message so the culprit is obvious.

None of these can fire at runtime after the import succeeds. This
is what allows ``normalize_for_storage``'s "never raises" contract
(see ``normalizer.py``, invariant #4) to remain intact.

Circular imports
----------------

``_base.py`` imports nothing from this package. Each profile module
imports only from ``._base``. ``common.py`` imports nothing from
this package either. Therefore:

  * ``import languages``        -> imports ``_base`` then each
                                    profile module in ``pkgutil`` order
                                    (which is filesystem order; the
                                    final tuple is re-sorted).
  * ``from .languages import PROFILES`` (from ``normalizer.py``)
                                -> same import, then attribute access.

No cycle can form. If a future profile needs a helper from
``common.py``, it can import it directly (``from .common import ...``)
-- that still forms no cycle, because ``common`` imports nothing
from the package.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Final

from ._base import LanguageProfile
from .common import normalize_common


#: Name of the module-level marker that non-profile modules must set
#: to ``False``. Kept as a constant so the discovery code and any
#: future documentation read from the same source of truth.
_NON_PROFILE_MARKER: Final[str] = "IS_PROFILE_MODULE"


def _discover() -> tuple[LanguageProfile, ...]:
    """Import every candidate module and collect its profile.

    See the module docstring for the full discovery rules. This
    function is called exactly once, at module import time. It is
    kept separate from the module-level assignment below so that it
    can be unit-tested against a synthetic package if needed.
    """
    discovered: list[LanguageProfile] = []
    code_owners: dict[str, str] = {}  # code -> module filename

    for module_info in pkgutil.iter_modules(__path__):
        name = module_info.name

        # Rule 2 -- underscore-prefixed files are private helpers.
        if name.startswith("_"):
            continue

        full_name = f"{__name__}.{name}"
        module = importlib.import_module(full_name)

        # Rule 3 -- explicit opt-out for shared utilities.
        if getattr(module, _NON_PROFILE_MARKER, True) is False:
            continue

        # Rule 3 (cont.) -- a candidate module MUST expose PROFILE.
        if not hasattr(module, "PROFILE"):
            raise ImportError(
                f"languages/{name}.py: no `PROFILE` attribute. "
                f"Every non-underscore module in the languages "
                f"package must either expose `PROFILE` (a "
                f"LanguageProfile instance) or declare "
                f"`IS_PROFILE_MODULE = False` to opt out of "
                f"discovery. See languages/__init__.py, rule 3."
            )

        profile = module.PROFILE

        # Rule 4 -- runtime protocol conformance.
        if not isinstance(profile, LanguageProfile):
            raise TypeError(
                f"languages/{name}.py: `PROFILE` does not satisfy the "
                f"LanguageProfile protocol (got "
                f"{type(profile).__name__}). Subclass "
                f"BaseLanguageProfile from languages/_base.py, or "
                f"implement every method of the Protocol by hand."
            )

        # Rule 5a -- non-empty code.
        if not profile.code:
            raise ValueError(
                f"languages/{name}.py: `PROFILE.code` is empty. "
                f"Every profile must declare an ISO 639-1 language "
                f"code (e.g. 'ar', 'ru', 'en', 'tr')."
            )

        # Rule 5b -- no duplicate codes.
        if profile.code in code_owners:
            raise ValueError(
                f"Duplicate language code {profile.code!r}: declared "
                f"by both languages/{code_owners[profile.code]}.py "
                f"and languages/{name}.py. Codes must be unique; "
                f"rename one of the two files or change its "
                f"`PROFILE.code`."
            )
        code_owners[profile.code] = name

        discovered.append(profile)

    # Rule 6 -- deterministic ordering. Sorting here (rather than
    # relying on pkgutil's filesystem order) is what makes the
    # pipeline reproducible across machines, filesystems, and
    # deployments.
    discovered.sort(key=lambda p: (p.priority, p.code))
    return tuple(discovered)


#: The discovered and ordered language profiles. Imported by
#: ``normalizer.py`` as the source of truth for the pipeline order.
#: Do not mutate; treat as immutable.
PROFILES: Final[tuple[LanguageProfile, ...]] = _discover()


def by_code(code: str) -> LanguageProfile | None:
    """Return the profile with the given ISO 639-1 code, if any.

    Returns ``None`` (not raising) when the code is not registered,
    so callers that want a fallback can use ``if prof is None``
    rather than wrapping in try/except. ``PROFILES`` is already
    sorted, so this lookup is O(n) -- fine for the current scale of
    two to ten profiles, and easy to replace with a dict later if
    the language count grows.
    """
    for profile in PROFILES:
        if profile.code == code:
            return profile
    return None


__all__ = [
    "PROFILES",
    "by_code",
    "normalize_common",
    "LanguageProfile",
]
