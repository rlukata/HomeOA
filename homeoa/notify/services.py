"""``notify()``: turn an event into per-recipient notifications and channel deliveries.

Routing per recipient:

1. Start from the type's default channels; a ``Preference`` row adds (enabled) or removes
   (disabled) a channel. Optional types may end with no channels (switched off). Mandatory
   types (legal notices) cannot be switched off: if no external channel is left, the
   notice still goes out by the type's default external channel.
2. Consent: email only with valid e-delivery consent on the person's current
   ``registry.NoticePreference`` and an address; otherwise a mandatory notice goes by postal
   mail and an optional email is dropped. Types with ``needs_consent=False`` (the portal
   invitation, which is how consent is collected) only need an address.
3. Resident gate: while residents are off, deliveries to anyone who is neither a board
   member nor a System admin are stored with status ``preview`` and never handed to a channel.
4. ``preview_only=True`` (callers that must never send on their own, e.g. the delinquency
   ladder): every delivery is ``preview`` whatever the board flag or residents switch.

Idempotency: one notification per (type, recipient, object, period); a repeat call returns
the existing one and creates nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.template import engines
from django.utils import timezone

from homeoa.core import residents_enabled
from homeoa.core.context import association_today, get_current_association

from . import channels as ch
from . import types as ntypes
from .models import Delivery, Notification, Preference

logger = logging.getLogger(__name__)


class MandatoryNoticeError(ValueError):
    """A mandatory notice can change channel but cannot be turned off."""


@dataclass(frozen=True)
class Rendered:
    subject: str
    body: str


def render(ntype: ntypes.NotificationType, context: dict) -> Rendered:
    engine = engines["django"]
    subject = engine.from_string(ntype.subject_template or str(ntype.label)).render(context)
    body = engine.from_string(ntype.body_template).render(context) if ntype.body_template else ""
    return Rendered(subject=" ".join(subject.split()), body=body.strip())


def object_key_for(obj) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    meta = getattr(obj, "_meta", None)
    if meta is not None:
        return f"{meta.label_lower}:{obj.pk}"
    return str(obj)


def idempotency_key(type_key: str, recipient, obj=None, period: str = "") -> str:
    return f"{type_key}|{recipient.pk}|{object_key_for(obj)}|{period}"


def is_board(person, association) -> bool:
    user = getattr(person, "user", None)
    if user is None:
        return False
    from homeoa.access.services import is_board_member

    return is_board_member(user, association)


def passes_resident_gate(person, association) -> bool:
    """Whether this person is staff who may be contacted before go-live or while residents
    are paused: a board member, or a System admin (an operator, not a resident)."""
    if is_board(person, association):
        return True
    user = getattr(person, "user", None)
    if user is None:
        return False
    from homeoa.access import permissions as perms
    from homeoa.access.services import roles_for

    return perms.SYS_ADMIN in roles_for(user, association)


def board_recipients(association) -> list:
    """One ``registry.Person`` per active user with a board role, for board-audience types.

    A board user with no linked Person (e.g. a board login set up before the registry) gets
    a contact Person linked to the login, named and addressed from the user, so the alert
    still reaches them. Repeat calls reuse that Person (one per user per association).
    """
    from homeoa.access import permissions as perms
    from homeoa.access.models import RoleAssignment
    from homeoa.access.services import is_board_member
    from homeoa.registry.models import Person

    day = association_today(association)
    user_ids = (
        RoleAssignment.all_associations.filter(
            association=association, role__slug=perms.BOARD, start__lte=day
        )
        .exclude(end__lt=day)
        .values_list("user_id", flat=True)
        .distinct()
    )
    users = get_user_model().objects.filter(pk__in=list(user_ids), is_active=True).order_by("pk")
    out = []
    for user in users:
        if not is_board_member(user, association):
            continue
        person, _created = Person.all_associations.get_or_create(
            association=association,
            user=user,
            defaults={"full_name": user.full_name or user.email, "email": user.email},
        )
        out.append(person)
    return out


def current_consent(person):
    from homeoa.registry.models import NoticePreference

    return (
        NoticePreference.all_associations.filter(person=person)
        .order_by("-recorded_at", "-pk")
        .first()
    )


def email_allowed(person) -> bool:
    """Email only with fresh written consent (portal, invitation or signed paper form;
    never an import) naming the address, which must still be the person's address."""
    pref = current_consent(person)
    if pref is None or not pref.valid_consent:
        return False
    return pref.email.strip().lower() == (person.email or "").strip().lower()


def _preferred(ntype: ntypes.NotificationType, person, overrides: dict | None = None) -> list:
    chosen = list(ntype.default_channels)
    prefs = {
        p.channel: p.enabled
        for p in Preference.all_associations.filter(person=person, type_key=ntype.key)
    }
    prefs.update(overrides or {})
    for channel in sorted(prefs):
        if prefs[channel] and channel not in chosen:
            chosen.append(channel)
        elif not prefs[channel] and channel in chosen:
            chosen.remove(channel)
    return chosen


def channels_for(ntype: ntypes.NotificationType, person) -> list[str]:
    """The channels this person gets ``ntype`` on, after preferences and consent."""
    chosen = _preferred(ntype, person)
    if ntype.mandatory and not any(c in ch.EXTERNAL for c in chosen):
        external = [c for c in ntype.default_channels if c in ch.EXTERNAL]
        chosen.extend(external or [ch.EMAIL])
    if ch.EMAIL in chosen and not ntype.needs_consent and not person.email:
        chosen.remove(ch.EMAIL)
    if ch.EMAIL in chosen and ntype.needs_consent and not email_allowed(person):
        chosen.remove(ch.EMAIL)
        if ntype.mandatory and ch.POSTAL not in chosen:
            chosen.append(ch.POSTAL)
    return chosen


def _jsonable(context: dict) -> dict:
    out = {}
    for k, v in context.items():
        if isinstance(v, (str, int, float, bool, type(None), dt.date, Decimal)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _dispatch(delivery: Delivery) -> None:
    channel = ch.get(delivery.channel)
    if channel is None:
        return  # stays queued until a channel for it is installed
    now = timezone.now()
    try:
        provider_id = channel.send(delivery)
    except Exception as exc:
        logger.warning("delivery %s on %s failed: %s", delivery.pk, delivery.channel, exc)
        delivery.status = Delivery.Status.FAILED
        delivery.failed_at = now
        delivery.error = str(exc)[:2000]
    else:
        if channel.deferred:
            # The channel queued a job that moves the delivery on (queued -> sent/failed).
            if provider_id:
                delivery.provider_id = provider_id
                delivery.save(update_fields=["provider_id"])
            return
        delivery.provider_id = provider_id or ""
        delivery.sent_at = now
        if channel.delivers_immediately:
            delivery.status = Delivery.Status.DELIVERED
            delivery.delivered_at = now
        else:
            delivery.status = Delivery.Status.SENT
    delivery.save()
    if delivery.status == Delivery.Status.FAILED:
        from .consent import after_email_outcome

        after_email_outcome(delivery)


def unit_for(person, obj=None, unit=None, as_of: dt.date | None = None):
    """The unit a message is about: ``unit``, else ``obj`` (or ``obj.unit``) when it is a
    unit, else the recipient's only owned unit on ``as_of``; ``None`` when ambiguous."""
    from homeoa.registry.models import Unit
    from homeoa.registry.services import units_owned_by

    if unit is not None:
        return unit
    if isinstance(obj, Unit):
        return obj
    if isinstance(getattr(obj, "unit", None), Unit):
        return obj.unit
    owned = units_owned_by(person, as_of)
    return owned[0] if len(owned) == 1 else None


def notify(
    type_key: str,
    recipients,
    context: dict | None = None,
    as_of: dt.date | None = None,
    *,
    obj=None,
    period: str = "",
    association=None,
    unit=None,
    preview_only: bool = False,
    force_channels: tuple[str, ...] | None = None,
    attachments: list[dict] | None = None,
) -> list[Notification]:
    """Create (once) and deliver ``type_key`` to each recipient ``registry.Person``.

    Returns the notifications, new or existing, in recipient order. Non-board recipients of
    a board-only type are skipped. ``unit`` files the message in that unit's comm log (see
    ``unit_for`` for the fallback). ``preview_only`` stores every delivery as ``preview``
    (never handed to a channel), for board members too and with residents on.
    ``force_channels`` replaces preference and consent routing, for notices a statute
    requires on a channel whatever the owner chose (e.g. the WA delinquency notice's
    email copy, RCW 64.34.364(17)(a)(i)); the resident gate still applies.
    ``attachments`` are ``{"name", "content_type", "file_key"}`` dicts naming FileStorage
    keys; the email channel attaches them at send time (they are never copied into the DB).
    """
    ntype = ntypes.get(type_key)
    association = association or get_current_association()
    if association is None:
        raise ValueError("No current association; pass association=.")
    as_of = as_of or association_today(association)
    context = dict(context or {})
    gate_residents = not residents_enabled(association)

    out: list[Notification] = []
    for person in recipients:
        board = is_board(person, association)
        if ntype.audience == ntypes.AUDIENCE_BOARD and not board:
            logger.info("skipping non-board recipient %s for %s", person.pk, type_key)
            continue
        key = idempotency_key(type_key, person, obj, period)
        existing = Notification.all_associations.filter(
            association=association, idempotency_key=key
        ).first()
        if existing is not None:
            out.append(existing)
            continue
        chosen = list(force_channels) if force_channels is not None else channels_for(ntype, person)
        if not chosen:
            logger.info("no channels for recipient %s on %s; skipped", person.pk, type_key)
            continue
        msg = render(ntype, {**context, "as_of": as_of, "recipient": person})
        held = gate_residents and not passes_resident_gate(person, association)
        preview = preview_only or held
        status = Delivery.Status.PREVIEW if preview else Delivery.Status.QUEUED
        try:
            with transaction.atomic():
                note = Notification.all_associations.create(
                    association=association,
                    type_key=type_key,
                    recipient=person,
                    subject=msg.subject[:300],
                    body=msg.body,
                    context=_jsonable(context),
                    object_key=object_key_for(obj),
                    unit=unit_for(person, obj, unit, as_of),
                    period=period,
                    idempotency_key=key,
                    as_of=as_of,
                    attachments=list(attachments or []),
                )
                deliveries = [
                    Delivery.all_associations.create(
                        association=association, notification=note, channel=c, status=status
                    )
                    for c in chosen
                ]
        except IntegrityError:  # a concurrent call created it first
            out.append(
                Notification.all_associations.get(association=association, idempotency_key=key)
            )
            continue
        for delivery in deliveries:
            if delivery.status == Delivery.Status.QUEUED:
                _dispatch(delivery)
        out.append(note)
    return out


def notify_after_commit(
    type_key: str,
    recipients,
    context: dict | None = None,
    as_of: dt.date | None = None,
    *,
    on_sent=None,
    **kwargs,
) -> None:
    """``notify(...)`` once the caller's transaction commits.

    A side effect never rolls back or blocks the state change that triggered it: the call
    runs in ``transaction.on_commit`` inside its own savepoint, and any error is logged and
    audited (``notify.send_failed``), never raised. ``on_sent(notes)`` runs in that same
    savepoint with the created notifications (e.g. to link them to a record). Recipients
    are materialized now, so the list is the one the caller saw.
    """
    recipients = list(recipients)
    association = kwargs.get("association") or get_current_association()
    kwargs["association"] = association
    obj = kwargs.get("obj")

    def _run() -> None:
        try:
            with transaction.atomic():
                notes = notify(type_key, recipients, context, as_of, **kwargs)
                if on_sent is not None:
                    on_sent(notes)
        except Exception as exc:
            logger.exception("Notification %s failed after commit", type_key)
            try:
                from homeoa import audit

                audit.record(
                    "notify.send_failed",
                    obj if hasattr(obj, "_meta") else None,
                    association=association,
                    message=f"{type_key}: {exc}"[:500],
                )
            except Exception:
                logger.exception("Could not audit the failed notification %s", type_key)

    _schedule(_run)


def _schedule(fn) -> None:
    """Run ``fn`` after commit (tests may swap this to run it at once)."""
    transaction.on_commit(fn)


def preview(type_key: str, context: dict | None = None) -> Rendered:
    """Render without storing or sending anything."""
    return render(ntypes.get(type_key), dict(context or {}))


def release_previews(notifications) -> list[Notification]:
    """Send stored previews now, re-routing each through current preferences and consent.

    For each ``preview`` delivery: if its channel is still one ``channels_for`` picks for the
    recipient today (e-notice consent is checked again), it is queued and handed to the channel;
    otherwise it is deleted. The resident gate still holds: previews to recipients who are
    neither board members nor System admins stay previews while residents are off. Returns the
    notifications left with no live delivery (the caller decides on a fallback, e.g. a letter
    by mail).
    """
    out: list[Notification] = []
    for note in notifications:
        association = note.association
        gate = not residents_enabled(association) and not passes_resident_gate(
            note.recipient, association
        )
        previews = list(note.deliveries.filter(status=Delivery.Status.PREVIEW))
        if gate:
            continue
        allowed = channels_for(ntypes.get(note.type_key), note.recipient)
        for delivery in previews:
            if delivery.channel in allowed:
                delivery.status = Delivery.Status.QUEUED
                delivery.save(update_fields=["status"])
                _dispatch(delivery)
            else:
                delivery.delete()
        if not note.deliveries.exclude(status=Delivery.Status.PREVIEW).exists():
            out.append(note)
    return out


def retry(delivery: Delivery) -> Delivery:
    """Hand a failed or queued delivery to its channel again (never a preview one)."""
    if delivery.status in (Delivery.Status.FAILED, Delivery.Status.QUEUED):
        delivery.status = Delivery.Status.QUEUED
        delivery.error = ""
        _dispatch(delivery)
    return delivery


def mark_read(notification: Notification) -> None:
    if notification.read_at is None:
        notification.read_at = timezone.now()
        notification.save(update_fields=["read_at"])


def set_preference(person, type_key: str, channel: str, enabled: bool) -> Preference:
    """Record a person's choice. A mandatory type refuses to lose its last external channel."""
    ntype = ntypes.get(type_key)
    if ntype.mandatory and not enabled and channel in ch.EXTERNAL:
        remaining = _preferred(ntype, person, {channel: False})
        if not any(c in ch.EXTERNAL for c in remaining):
            raise MandatoryNoticeError(
                "a mandatory notice can change channel but cannot be turned off"
            )
    pref, _ = Preference.all_associations.update_or_create(
        association_id=person.association_id,
        person=person,
        type_key=type_key,
        channel=channel,
        defaults={"enabled": enabled},
    )
    return pref
