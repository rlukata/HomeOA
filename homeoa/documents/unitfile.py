"""Unit file (DOC-02): one dated list of everything filed about a unit.

Apps add to it with ``register(key, contributor)``, where ``contributor(unit, request)``
returns an iterable of ``Entry``. A contributor checks the request's permissions itself
and returns nothing the user may not see. ``entries(unit, request)`` merges every
contributor's entries, newest first. The core ones (ledger, letters, statements,
communications) live in ``contributors``; later apps (violations, ARC) register theirs.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from django.utils.functional import Promise


@dataclass(frozen=True)
class Entry:
    date: dt.date
    kind: str  # contributor key, e.g. "letters"
    kind_label: str | Promise
    title: str | Promise
    url: str = ""
    detail: str | Promise = ""
    extra_url: str = ""  # an optional second link, e.g. the CSV of an export
    extra_label: str | Promise = ""


Contributor = Callable[..., Iterable[Entry]]
_contributors: dict[str, Contributor] = {}
#: contributor key -> the optional module it belongs to. While that module is off for the
#: unit's association its past entries still show, read-only: without links into the
#: module's pages, which are hidden.
_modules: dict[str, str] = {}


def register(key: str, contributor: Contributor, *, module: str | None = None) -> None:
    _contributors[key] = contributor
    if module is None:
        _modules.pop(key, None)
    else:
        _modules[key] = module


def unregister(key: str) -> None:
    _contributors.pop(key, None)
    _modules.pop(key, None)


def registered() -> list[str]:
    return list(_contributors)


def entries(unit, request, kind: str = "") -> list[Entry]:
    from homeoa.plugins.slots import module_on

    out: list[Entry] = []
    for key, fn in list(_contributors.items()):
        if kind and key != kind:
            continue
        found = fn(unit, request)
        if not module_on(_modules.get(key), unit.association):
            found = [dataclasses.replace(e, url="", extra_url="", extra_label="") for e in found]
        out.extend(found)
    return sorted(out, key=lambda e: (e.date, e.kind), reverse=True)
