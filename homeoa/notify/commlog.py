"""Per-unit communication log (COM-05).

Every delivery (email, letter, in-app, preview) of a notification filed under a unit
(``Notification.unit``) appears in that unit's log with its status. Board-only for now
(``board.shell`` and ``units.read``); owners will see their own unit through the portal.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from . import channels as ch
from .models import Delivery

CHANNEL_LABELS = {
    ch.EMAIL: _("Email"),
    ch.POSTAL: _("Letter"),
    ch.INAPP: _("In app"),
    ch.PREVIEW: _("Preview"),
}


@dataclass(frozen=True)
class Filters:
    channel: str = ""
    status: str = ""

    @classmethod
    def from_query(cls, query) -> Filters:
        channel = query.get("channel", "")
        status = query.get("status", "")
        return cls(
            channel=channel if channel in CHANNEL_LABELS else "",
            status=status if status in Delivery.Status.values else "",
        )


def entries(unit, filters: Filters | None = None) -> QuerySet[Delivery]:
    """Deliveries filed under ``unit``, newest first."""
    filters = filters or Filters()
    qs = Delivery.all_associations.filter(
        association_id=unit.association_id, notification__unit=unit
    ).select_related("notification__recipient")
    if filters.channel:
        qs = qs.filter(channel=filters.channel)
    if filters.status:
        qs = qs.filter(status=filters.status)
    return qs.order_by("-created_at", "-pk")


def channel_label(key: str):
    return CHANNEL_LABELS.get(key, key)


def last_event_at(delivery: Delivery):
    return (
        delivery.bounced_at
        or delivery.failed_at
        or delivery.delivered_at
        or delivery.sent_at
        or delivery.created_at
    )


STATUS_TONE = {
    Delivery.Status.QUEUED: "warn",
    Delivery.Status.SENT: "ok",
    Delivery.Status.DELIVERED: "ok",
    Delivery.Status.FAILED: "danger",
    Delivery.Status.BOUNCED: "danger",
    Delivery.Status.PREVIEW: "info",
}


def status_tone(status: str) -> str:
    return STATUS_TONE.get(status, "")
