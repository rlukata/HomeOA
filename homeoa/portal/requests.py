"""Resident Requests tab (POR-04): one list of the resident's requests of every kind.

Each kind of request comes from a module through the ``OWNER_PORTAL`` slot (a
``RequestKind``): its "New request" button and its items. The board's preview-as-unit
shows the unit's requests for the chosen persona, read-only.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.utils.functional import Promise

from .contributions import request_kinds


@dataclass
class Item:
    kind: str  # e.g. "arc" or "maintenance"
    kind_label: str | Promise
    icon: str
    title: str
    status: str
    badge: str
    date: dt.date
    url: str = ""
    timeline: list[tuple[dt.date, str]] = field(default_factory=list)


@dataclass(frozen=True)
class NewRequest:
    """A "New request" button; hidden ones keep their line so the page layout stays put."""

    kind: object
    shown: bool


def _sorted(items: list[Item]) -> list[Item]:
    return sorted(items, key=lambda i: i.date, reverse=True)


def for_user(request) -> dict:
    """Items and "New request" buttons for the signed-in resident."""
    kinds = request_kinds(request.association)
    found = {k.kind: k.for_user(request) for k in kinds}
    items = []
    # Residents have always seen the later kinds first among requests of the same day.
    for k in reversed(kinds):
        items += found[k.kind] or []
    return {
        "items": _sorted(items),
        "new_requests": [NewRequest(k, found[k.kind] is not None) for k in kinds],
    }


def for_unit(unit, *, owner: bool) -> dict:
    """Items for the board's read-only preview of ``unit`` (owner or tenant persona)."""
    kinds = request_kinds(unit.association)
    found = {k.kind: k.for_unit(unit, owner) for k in kinds}
    items = []
    for k in kinds:
        items += found[k.kind] or []
    return {
        "items": _sorted(items),
        "new_requests": [NewRequest(k, found[k.kind] is not None) for k in kinds],
    }
