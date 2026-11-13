"""The current association for this request or job, held in a context variable."""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

if TYPE_CHECKING:
    from .models import Association

_current: ContextVar[Association | None] = ContextVar("homeoa_current_association", default=None)


def get_current_association() -> Association | None:
    return _current.get()


def set_current_association(association: Association | None):
    """Set the association; returns a token for ``reset_current_association``."""
    return _current.set(association)


def reset_current_association(token) -> None:
    _current.reset(token)


@contextmanager
def association_context(association: Association | None) -> Iterator[None]:
    """Run a block (job, management command, test) scoped to one association."""
    token = _current.set(association)
    try:
        yield
    finally:
        _current.reset(token)


def association_today(association: Association | None = None) -> datetime.date:
    """Today's date in the association's time zone (not the server's).

    Defaults to the current association; with none set (or a bad zone name), uses UTC.
    ``HOMEOA_FIXED_TODAY`` (``YYYY-MM-DD``) pins the date for tests, E2E and demos.
    """
    fixed = fixed_today()
    if fixed is not None:
        return fixed
    association = association if association is not None else get_current_association()
    tz = datetime.UTC
    if association is not None:
        with suppress(ZoneInfoNotFoundError, ValueError, TypeError):
            tz = ZoneInfo(association.timezone)
    return timezone.localdate(timezone=tz)


def fixed_today() -> datetime.date | None:
    """The pinned date from ``HOMEOA_FIXED_TODAY``, or None.

    Honored only with ``DEBUG`` or ``HOMEOA_FIXED_TODAY_ALLOWED`` (test, e2e and dev
    settings); otherwise a set value is refused with ImproperlyConfigured.
    """
    value = getattr(settings, "HOMEOA_FIXED_TODAY", "") or ""
    if not value:
        return None
    if not (settings.DEBUG or getattr(settings, "HOMEOA_FIXED_TODAY_ALLOWED", False)):
        raise ImproperlyConfigured("HOMEOA_FIXED_TODAY is not allowed in these settings.")
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ImproperlyConfigured("HOMEOA_FIXED_TODAY must be YYYY-MM-DD.") from exc
