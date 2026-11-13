"""Append-only audit log (PLT-04).

Public API (import as ``from homeoa import audit``)
---------------------------------------------------
``audit.register(Model, exclude=("field", ...))``
    Record create / update / delete of ``Model`` automatically (via signals). Call it in
    your app's ``AppConfig.ready()``. Updates that change nothing write no event.
    Encrypted fields (``homeoa.core.crypto``) are always recorded as ``"changed"``,
    never with their values. ``exclude`` drops fields from diffs entirely.
``audit.record(action, obj=None, *, changes=None, association=None, actor=None,
object_type=None, object_id=None, object_repr=None, message="")``
    Write an explicit event, e.g. ``audit.record("payment.void", payment,
    message="Duplicate")``. Use dotted ``<kind>.<verb>`` action names.
``audit.audit_context(actor=None, label="", ip=None, request_id="")``
    Context manager setting the actor for jobs and commands; with no user, ``label``
    (e.g. ``"job:send_statements"``) becomes the event's ``actor_label``. In requests,
    ``AuditContextMiddleware`` keeps the request (``request.user`` is read when each
    event is recorded, so ``login()``/``logout()`` mid-request are attributed right),
    the IP and a request id (``X-Request-ID`` or a new UUID, echoed in the response,
    error responses included).
``homeoa.audit.context.RequestIdLogFilter``
    Logging filter adding ``record.request_id`` for log formatters.

``AuditEvent`` rows can never be changed: ``save()`` on an existing row, ``delete()``
and queryset ``update()``/``delete()`` raise ``AuditImmutableError``, and a Postgres
trigger rejects any UPDATE or DELETE on ``audit_auditevent``.

Board list view: ``/settings/audit/`` (filters: action, object type, actor, date range).
"""

from typing import Any


def register(model, exclude=()):
    from .services import register as _register

    return _register(model, exclude=exclude)


def record(action: str, obj=None, **kwargs: Any):
    from .services import record as _record

    return _record(action, obj, **kwargs)


def audit_context(**kwargs: Any):
    from .context import audit_context as _ctx

    return _ctx(**kwargs)
