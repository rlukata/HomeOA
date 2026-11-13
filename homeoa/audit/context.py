"""Who is acting, from where: held in a context variable for the request or job."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuditActor:
    """The acting party. In a request, ``request`` is kept so the user is read when the
    event is recorded (``login()``/``logout()`` replace ``request.user`` mid-request)."""

    user: Any = None
    ip: str | None = None
    request_id: str = ""
    label: str = ""
    request: Any = None

    @property
    def current_user(self):
        if self.request is not None:
            return getattr(self.request, "user", None)
        return self.user


_NOBODY = AuditActor()
_current: ContextVar[AuditActor | None] = ContextVar("homeoa_audit_actor", default=None)


def get_audit_actor() -> AuditActor:
    return _current.get() or _NOBODY


def set_audit_actor(actor: AuditActor):
    return _current.set(actor)


def reset_audit_actor(token) -> None:
    _current.reset(token)


@contextmanager
def audit_context(*, actor=None, label="", ip=None, request_id=""):
    """Attribute writes in this block to ``actor``, or with no user to a system
    ``label`` such as ``"job:send_statements"``."""
    token = _current.set(AuditActor(user=actor, ip=ip, request_id=request_id, label=label))
    try:
        yield
    finally:
        _current.reset(token)


class RequestIdLogFilter(logging.Filter):
    """Adds ``record.request_id`` (``"-"`` outside a request) for log formatters."""

    def filter(self, record):
        record.request_id = get_audit_actor().request_id or "-"
        return True
