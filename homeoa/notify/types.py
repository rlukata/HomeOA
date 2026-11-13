"""Notification type registry, kept in code. Apps register their types in ``ready()``."""

from __future__ import annotations

from dataclasses import dataclass

from django.utils.functional import Promise

AUDIENCE_BOARD = "board"
AUDIENCE_RESIDENTS = "residents"
AUDIENCE_ALL = "all"
AUDIENCES = (AUDIENCE_BOARD, AUDIENCE_RESIDENTS, AUDIENCE_ALL)


@dataclass(frozen=True)
class NotificationType:
    """``mandatory`` marks legal notices: they can change channel but never be turned off."""

    key: str
    label: str | Promise
    default_channels: tuple[str, ...] = ("inapp",)
    audience: str = AUDIENCE_ALL
    mandatory: bool = False
    #: False only for messages that are not association notices and must reach owners
    #: before they can consent (the portal invitation); email then needs just an address.
    needs_consent: bool = True
    subject_template: str = ""
    body_template: str = ""
    #: The optional module the type belongs to; residents do not see it among their
    #: notification preferences while the module is off.
    module: str | None = None

    def __post_init__(self):
        if self.audience not in AUDIENCES:
            raise ValueError(f"unknown audience {self.audience!r}")
        if not self.default_channels:
            raise ValueError("a notification type needs at least one default channel")


_types: dict[str, NotificationType] = {}


def register(ntype: NotificationType) -> NotificationType:
    existing = _types.get(ntype.key)
    if existing is not None and existing != ntype:
        raise ValueError(f"notification type {ntype.key!r} already registered")
    _types[ntype.key] = ntype
    return ntype


def unregister(key: str) -> None:
    _types.pop(key, None)


def get(key: str) -> NotificationType:
    try:
        return _types[key]
    except KeyError:
        raise LookupError(f"unknown notification type {key!r}") from None


def all_types() -> list[NotificationType]:
    return sorted(_types.values(), key=lambda t: t.key)
