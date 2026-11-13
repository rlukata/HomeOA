"""What optional modules add to the owner portal, through the ``OWNER_PORTAL`` slot.

A module registers two kinds of contribution:

- ``PortalPart``: a template shown at a named place of a portal page (see ``PLACES``).
  It renders with the page's context; its ``context`` callable may add values, or return
  ``None`` to hide the part.
- ``RequestKind``: one kind of resident request on the Requests tab. It gives the "New
  request" button and the items listed under "Your requests".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.utils.functional import Promise

from homeoa.plugins import slots

PLACES = frozenset(
    {
        "home",  # resident Home, under "From the board"
        "more",  # More page links, before Owner votes
        "requests",  # Requests tab, between "New request" and "Your requests"
        "pay_balance",  # Pay page, under the pay buttons (online payments only)
        "pay_methods",  # Pay page, end of "How you pay" (online payments only)
        "welcome_how",  # welcome packet, end of "How things work"
    }
)


@dataclass(frozen=True)
class PortalPart:
    place: str
    template: str
    context: Callable[[Any], dict | None] | None = None

    def __post_init__(self):
        if self.place not in PLACES:
            raise ValueError(f"Unknown owner portal place {self.place!r}.")


@dataclass(frozen=True)
class RequestKind:
    kind: str
    new_label: str | Promise
    new_icon: str
    new_url_name: str
    new_testid: str
    #: The signed-in resident's items, or ``None`` when they cannot send this kind.
    for_user: Callable[[Any], list | None]
    #: The items of a unit for the board's preview; ``owner`` is the previewed persona.
    for_unit: Callable[[Any, bool], list | None]


def parts(place: str, association=None) -> list[PortalPart]:
    return [
        v
        for v in slots.values(slots.OWNER_PORTAL, association)
        if isinstance(v, PortalPart) and v.place == place
    ]


def request_kinds(association=None) -> list[RequestKind]:
    return [v for v in slots.values(slots.OWNER_PORTAL, association) if isinstance(v, RequestKind)]
