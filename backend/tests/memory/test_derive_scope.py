# backend/tests/memory/test_derive_scope.py (ملف جديد)

import pytest

from peldrun.memory.domain.enums import MemoryScope
from omweb.memory.extraction.trigger_router import _derive_scope


def test_derive_scope_real_project_stays_project():
    """A real project id stays PROJECT-scoped."""
    scope_type, scope_id = _derive_scope(
        chat_id="chat_1", project_id="acme_api",
    )
    assert scope_type == MemoryScope.PROJECT
    assert scope_id == "acme_api"


def test_derive_scope_default_project_maps_to_user():
    """The sentinel `default_project` maps to USER scope.

    Standalone chats are auto-assigned `project_id="default_project"`
    at creation time. Extracting into `project:default_project`
    would make memories invisible to any chat whose project_id is
    None -- so we route them to `user:default` instead. This makes
    them available to every future chat via
    `resolve_scope_refs`'s unconditional `user:default` inclusion.
    """
    scope_type, scope_id = _derive_scope(
        chat_id="chat_1", project_id="default_project",
    )
    assert scope_type == MemoryScope.USER
    assert scope_id == "default"


def test_derive_scope_no_project_maps_to_user():
    """A chat without any project also goes to USER scope."""
    scope_type, scope_id = _derive_scope(
        chat_id="chat_1", project_id=None,
    )
    assert scope_type == MemoryScope.USER
    assert scope_id == "default"


def test_derive_scope_empty_project_maps_to_user():
    """An empty-string project id is treated as 'no project'."""
    scope_type, scope_id = _derive_scope(
        chat_id="chat_1", project_id="",
    )
    assert scope_type == MemoryScope.USER
    assert scope_id == "default"
