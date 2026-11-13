"""Unit page include hook: other apps add sections to the unit page without editing it.

``register("app/_unit_section.html")`` in an ``AppConfig.ready``. Each template is included
at the end of the unit page with the page context (``unit``, ``request``, ``perms`` ...),
in registration order. A section checks its own permissions.
"""

from __future__ import annotations

_sections: list[str] = []


def register(template_name: str) -> None:
    if template_name not in _sections:
        _sections.append(template_name)


def unregister(template_name: str) -> None:
    if template_name in _sections:
        _sections.remove(template_name)


def all_sections() -> list[str]:
    return list(_sections)
