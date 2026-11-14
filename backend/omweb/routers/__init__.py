# backend/omweb/routers/__init__.py
"""
Aggregated router package for the PELDRUN web API.

Importing a router module through this package keeps the import
surface stable and makes it easy to discover which routers are
available to `omweb.main`. The list is intentionally explicit;
routers are added here only once they are wired into the application
entrypoint.

Lazy loading
------------
This module uses PEP 562 `__getattr__` to import each router the
first time it is accessed, instead of eagerly importing all of them
at package-import time. The change was forced by a real deployment
bug:

    `backend/omweb/routers/source_control.py` (and a few other
    routers) uses RELATIVE imports to reach
    `backend.integrations.*`, so it works whether the project is
    imported as `backend.omweb.routers.*` or as `omweb.routers.*`.
    However, `config_rtr.py` (and a few others) still uses
    ABSOLUTE imports such as `from omweb.config import ...`, which
    only work when `backend/` is directly on `sys.path`.

    Before this change, importing ANY router (for example,
    `from backend.omweb.routers.source_control import router`)
    executed `__init__.py`, which eagerly imported EVERY router,
    including the ones with absolute imports. The whole import
    would then fail with `ModuleNotFoundError: No module named
    'omweb'` when the caller started from the project root
    (`python -c "from backend.omweb.routers.source_control import ..."`),
    even though the target router was perfectly healthy.

    Lazy loading isolates the failure: a broken router only
    affects callers that actually need it, and the rest of the
    package remains importable.

Compatibility
-------------
The lazy `__getattr__` produces the SAME objects as the previous
eager imports: `from omweb.routers import source_control` returns
the module, and `from omweb.routers.source_control import router`
returns the router. `from omweb.routers import *` is NOT supported
(the `__all__` list is provided for introspection only); callers
should import the specific routers they need.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, TYPE_CHECKING

__all__ = [
    "config_rtr",
    "files",
    "mcp",
    "mcp_auth",
    "resource_monitor",
    "run",
    "search_rtr",
    "setup",
    "source_control",
    "status",
]


if TYPE_CHECKING:  # pragma: no cover - type-checker hint only
    # The TYPE_CHECKING block lets static analyzers (mypy, pyright,
    # IDEs) resolve the names below as modules without triggering
    # the runtime import that `__getattr__` handles lazily.
    from . import (  # noqa: F401
        config_rtr,
        files,
        mcp,
        mcp_auth,
        resource_monitor,
        run,
        search_rtr,
        setup,
        source_control,
        status,
    )


#: Cache of modules that have already been imported. Populating it
#: makes subsequent accesses O(1) and guarantees that two calls to
#: `__getattr__("source_control")` return the SAME module object
#: (the standard import system already caches, but this is a belt-
#: and-suspenders measure that also short-circuits the `__all__`
#: membership check).
_LAZY_CACHE: dict[str, Any] = {}


def __getattr__(name: str) -> Any:
    """
    Lazily import a router module on first access.

    Raises `AttributeError` for names that are not in `__all__`,
    matching the behavior of a normal package that does not define
    them. This is required by the import machinery: `__getattr__`
    must only handle the names the package actually owns; otherwise
    `hasattr(routers, "nonexistent")` would return True and break
    introspection.
    """
    if name in _LAZY_CACHE:
        return _LAZY_CACHE[name]
    if name not in __all__:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    module = import_module(f".{name}", __name__)
    _LAZY_CACHE[name] = module
    return module


def __dir__() -> list[str]:
    """Return the package's attributes for `dir()` and REPL completion."""
    return sorted(__all__)
