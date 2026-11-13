"""Registration of audited models and explicit event recording. See ``homeoa.audit``."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from django.db import models
from django.db.models.signals import post_delete, post_save, pre_save

from homeoa.core.crypto import is_encrypted

from .context import get_audit_actor
from .models import AuditEvent

CHANGED = "changed"

# model class -> frozenset of excluded field names (name or attname)
_registry: dict[type[models.Model], frozenset[str]] = {}


def _json_value(value):
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def _fields(model):
    excluded = _registry.get(model, frozenset())
    return [
        f
        for f in model._meta.concrete_fields
        if f.name not in excluded and f.attname not in excluded
    ]


def snapshot(instance) -> dict:
    """attname -> JSON-safe value. Encrypted values are kept only to compare, never stored."""
    data = {}
    for field in _fields(type(instance)):
        value = getattr(instance, field.attname)
        data[field.attname] = value if is_encrypted(field) else _json_value(value)
    return data


def diff(model, before: dict | None, after: dict | None) -> dict:
    """``{attname: [before, after]}`` for changed fields; encrypted ones become ``"changed"``."""
    changes = {}
    for field in _fields(model):
        name = field.attname
        old = before.get(name) if before is not None else None
        new = after.get(name) if after is not None else None
        if before is not None and after is not None and old == new:
            continue
        if old is None and new is None:
            continue
        changes[name] = CHANGED if is_encrypted(field) else [old, new]
    return changes


def _actor_fields(actor):
    ctx = get_audit_actor()
    if actor is None:
        actor = ctx.current_user
    if actor is not None and getattr(actor, "is_authenticated", False):
        return {"actor_id": actor.pk, "actor_label": str(actor)[:200]}
    return {"actor_id": None, "actor_label": ctx.label[:200]}


def record(
    action: str,
    obj=None,
    *,
    changes: dict | None = None,
    association=None,
    actor=None,
    object_type: str | None = None,
    object_id=None,
    object_repr: str | None = None,
    message: str = "",
) -> AuditEvent:
    """Write one explicit audit event and return it.

    ``association`` defaults to ``obj.association``, then the current association.
    ``actor`` defaults to the user in the audit context (request or ``audit_context``).
    """
    ctx = get_audit_actor()
    if obj is not None:
        object_type = object_type or obj._meta.label_lower
        object_id = obj.pk if object_id is None else object_id
        object_repr = str(obj) if object_repr is None else object_repr
        if association is None:
            if obj._meta.label_lower == "core.association":
                association = obj
            elif getattr(obj, "association_id", None) is not None:
                association = obj.association
    event = AuditEvent(
        association=association,
        action=action,
        object_type=object_type or "",
        object_id="" if object_id is None else str(object_id),
        object_repr=(object_repr or "")[:200],
        changes=_json_value(changes or {}),
        message=message,
        ip=ctx.ip,
        request_id=ctx.request_id,
        **_actor_fields(actor),
    )
    event.save()
    return event


def _pre_save(sender, instance, raw=False, **kwargs):
    if raw:
        return
    before = None
    if instance.pk is not None and not instance._state.adding:
        old = type(instance)._base_manager.filter(pk=instance.pk).first()
        if old is not None:
            before = snapshot(old)
    instance._audit_before = before


def _post_save(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    before = None if created else instance.__dict__.pop("_audit_before", None)
    changes = diff(sender, before, snapshot(instance))
    if not created and not changes:
        return
    action = AuditEvent.ACTION_CREATE if created else AuditEvent.ACTION_UPDATE
    record(action, instance, changes=changes)


def _post_delete(sender, instance, **kwargs):
    record(AuditEvent.ACTION_DELETE, instance, changes=diff(sender, snapshot(instance), None))


def register(model, exclude=()):
    """Audit create/update/delete of ``model``. Idempotent; usable as a class decorator."""
    if model is AuditEvent:
        raise ValueError("AuditEvent cannot audit itself.")
    first = model not in _registry
    _registry[model] = frozenset(exclude)
    if first:
        uid = f"homeoa.audit:{model._meta.label_lower}"
        pre_save.connect(_pre_save, sender=model, dispatch_uid=uid + ":pre")
        post_save.connect(_post_save, sender=model, dispatch_uid=uid + ":post")
        post_delete.connect(_post_delete, sender=model, dispatch_uid=uid + ":del")
    return model


def unregister(model) -> None:
    """Stop auditing ``model`` (mainly for tests)."""
    if _registry.pop(model, None) is None:
        return
    uid = f"homeoa.audit:{model._meta.label_lower}"
    pre_save.disconnect(sender=model, dispatch_uid=uid + ":pre")
    post_save.disconnect(sender=model, dispatch_uid=uid + ":post")
    post_delete.disconnect(sender=model, dispatch_uid=uid + ":del")


def is_registered(model) -> bool:
    return model in _registry
