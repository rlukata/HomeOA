"""Named slots through which core reaches optional modules.

Core and modules add contributions to a slot, each tagged with the id of the module it
belongs to (``None`` for core). Readers get the contributions of a slot in order: by the
optional ``order`` key, then by registration order. Modules register from the ``ready``
hook named in their manifest.

Readers given an ``association`` (an object or its id) get only core contributions and
those of modules turned on for it; without one (startup, global listings) they get all.
"""

from __future__ import annotations

import dataclasses
from typing import Any

MENU_TABS = "menu_tabs"
HOME_CARDS = "home_cards"
HOME_TODOS = "home_todos"
OWNER_PORTAL = "owner_portal"
DAILY_JOBS = "daily_jobs"
EVENT_HANDLERS = "event_handlers"
WIZARD_SECTIONS = "wizard_sections"
RESALE_CERTIFICATE_ANSWERS = "resale_certificate_answers"
BOARD_PACKET_SECTIONS = "board_packet_sections"
OPEN_ITEMS_CHECKS = "open_items_checks"
#: ``fn(association, start, end) -> list[compliance.calendar.Entry]``: vendor-side dates.
COMPLIANCE_CALENDAR_ENTRIES = "compliance_calendar_entries"
#: ``fn(association, on) -> list[Person]``: people besides the board who hold an office.
OFFICE_HOLDERS = "office_holders"
#: ``fn(period, as_of) -> dict``: fields added to an owner statement for a period.
STATEMENT_EXTRAS = "statement_extras"
#: ``fn(user, association, query) -> list[search.services.Hit]``: one kind of record.
SEARCH_SOURCES = "search_sources"

SLOTS = frozenset(
    {
        MENU_TABS,
        HOME_CARDS,
        HOME_TODOS,
        OWNER_PORTAL,
        DAILY_JOBS,
        EVENT_HANDLERS,
        WIZARD_SECTIONS,
        RESALE_CERTIFICATE_ANSWERS,
        BOARD_PACKET_SECTIONS,
        OPEN_ITEMS_CHECKS,
        COMPLIANCE_CALENDAR_ENTRIES,
        OFFICE_HOLDERS,
        STATEMENT_EXTRAS,
        SEARCH_SOURCES,
    }
)


@dataclasses.dataclass(frozen=True)
class Contribution:
    slot: str
    value: Any
    module: str | None = None
    order: int = 0


_contributions: dict[str, list[Contribution]] = {}


def register(slot: str, value: Any, *, module: str | None = None, order: int = 0) -> None:
    """Add ``value`` to ``slot`` on behalf of ``module`` (``None`` means core)."""
    if slot not in SLOTS:
        raise ValueError(f"Unknown slot {slot!r}.")
    _contributions.setdefault(slot, []).append(Contribution(slot, value, module, order))


def contributions(slot: str, association=None) -> list[Contribution]:
    """The contributions to ``slot``, sorted by ``order`` then registration order, leaving
    out modules that are off for ``association``."""
    if slot not in SLOTS:
        raise ValueError(f"Unknown slot {slot!r}.")
    found = sorted(_contributions.get(slot, []), key=lambda c: c.order)
    if association is None or all(c.module is None for c in found):
        return found
    on = _switches(association)
    return [c for c in found if c.module is None or on(c.module)]


def values(slot: str, association=None) -> list[Any]:
    return [c.value for c in contributions(slot, association)]


def module_on(module_id: str | None, association=None) -> bool:
    """Whether ``module_id`` is on for ``association`` (an object or its id); core
    (``None``) always is, and so is every module when there is no association."""
    if module_id is None or association is None:
        return True
    return _switches(association)(module_id)


def _switches(association):
    from . import modules

    return modules.switches_for(association)


@dataclasses.dataclass(frozen=True)
class OpenItem:
    """Something still in flight in a module, which stops the module from being turned off.

    ``description`` is a short translated line; ``url_name`` and ``url_args`` point to the
    item when it has a page of its own."""

    description: str
    url_name: str = ""
    url_args: tuple = ()


def open_items(module_id: str, association) -> list[OpenItem]:
    """The open items of ``module_id`` in ``association``, from the checks it added to
    ``OPEN_ITEMS_CHECKS`` (each ``fn(association) -> list[OpenItem]``). A module that adds
    none has no open items. The checks run whether or not the module is on."""
    items: list[OpenItem] = []
    for c in contributions(OPEN_ITEMS_CHECKS):
        if c.module == module_id:
            items.extend(c.value(association))
    return items


def clear() -> None:
    """Forget every contribution (tests)."""
    _contributions.clear()
