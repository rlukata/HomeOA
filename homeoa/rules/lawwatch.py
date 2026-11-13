"""Law change sources: which statute sections a legislature changed since a date.

A rule pack names its source in ``RulePack.change_source`` (a dotted path to a class
built with no arguments). ``ChangeSource.affected(sections, since)`` returns one
``LawChange`` per change it knows of. A source that cannot reach its service raises
``LawSourceError`` with a plain reason. Without a source the pack's law cannot be
checked automatically. ``FakeChangeSource`` serves tests and the ``law_check --fake``
command; it never touches the network.
"""

from __future__ import annotations

import abc
import dataclasses
import datetime

from django.conf import settings
from django.utils.module_loading import import_string

from .base import RulePack


class LawSourceError(Exception):
    """The change source could not answer (network down, service error, bad reply)."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class LawChange:
    """One change to a section. ``action`` is amended, added or repealed, or what the
    source says; fields the source does not give stay empty."""

    section: str
    bill: str = ""
    session_law: str = ""
    action: str = ""
    effective: datetime.date | None = None
    url: str = ""

    def to_json(self) -> dict:
        data = dataclasses.asdict(self)
        data["effective"] = self.effective.isoformat() if self.effective else None
        return data


class ChangeSource(abc.ABC):
    @abc.abstractmethod
    def affected(self, sections, since: datetime.date) -> list[LawChange]: ...


class FakeChangeSource(ChangeSource):
    """Answers from ``changes``: section -> list of ``LawChange`` (or dicts of its
    fields). ``error`` makes every call fail with that reason."""

    def __init__(self, changes: dict | None = None, error: str = ""):
        self.changes = changes or {}
        self.error = error
        self.calls: list[tuple[tuple[str, ...], datetime.date]] = []

    def affected(self, sections, since: datetime.date) -> list[LawChange]:
        sections = tuple(sections)
        self.calls.append((sections, since))
        if self.error:
            raise LawSourceError(self.error)
        out = []
        for section in sections:
            for change in self.changes.get(section, ()):
                if isinstance(change, dict):
                    change = LawChange(section=section, **change)
                out.append(change)
        return out


def network_allowed() -> bool:
    """False under test settings: a real source must refuse to call out."""
    return getattr(settings, "HOMEOA_LAW_CHECK_NETWORK", True)


def source_for(pack: RulePack) -> ChangeSource | None:
    """The pack's change source, or ``None`` when it has none."""
    if not pack.change_source:
        return None
    return import_string(pack.change_source)()
