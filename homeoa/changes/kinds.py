"""What can be changed through a change request.

Each kind has a label and an ``apply(change)`` function that makes the change from
``change.payload`` and returns a link to the result (or ""). Apps register their kinds in
``AppConfig.ready``. ``apply`` runs inside a savepoint: if it raises, nothing it did is kept
and the request is marked as not applied, with the error shown to the board.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

Apply = Callable[[object], str]


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    apply: Apply
    # Approved by citing a board resolution (applied when it is adopted), not by clicks.
    by_resolution: bool = False


_registry: dict[str, Kind] = {}


def register(kind: Kind) -> Kind:
    _registry[kind.key] = kind
    return kind


def get(key: str) -> Kind | None:
    return _registry.get(key)


def label(key: str) -> str:
    kind = _registry.get(key)
    return str(kind.label) if kind else key
