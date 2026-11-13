"""Owner records requests: an owner asks in the portal or the board logs one; the deadline
shows on Home until the board marks it fulfilled.

The deadline comes from the active rule pack (``records.inspection_days`` and
``records.inspection_max_days``); the WA values are unverified rule-pack data. The board
is notified when an owner asks, and the owner when the board marks the request fulfilled.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import rules
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from .models import Person, RecordsRequest

RECEIVED_TYPE = "records.request_received"
FULFILLED_TYPE = "records.request_fulfilled"


def register_notification_types() -> None:
    ntypes.register(
        ntypes.NotificationType(
            key=RECEIVED_TYPE,
            label=gettext_lazy("Records request from an owner"),
            default_channels=(ch.EMAIL, ch.INAPP),
            audience=ntypes.AUDIENCE_BOARD,
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% blocktranslate with n=owner %}"
                "Records request from {{ n }}{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with n=owner how=delivery %}"
                "{{ n }} asked for these records ({{ how }}):{% endblocktranslate %}\n\n"
                "{{ what }}\n\n"
                "{% if due %}{% blocktranslate %}Due {{ due }}.{% endblocktranslate %}\n{% endif %}"
                "{{ url }}"
            ),
        )
    )
    ntypes.register(
        ntypes.NotificationType(
            key=FULFILLED_TYPE,
            label=gettext_lazy("Records request fulfilled"),
            default_channels=(ch.EMAIL, ch.INAPP),
            audience=ntypes.AUDIENCE_RESIDENTS,
            # A reply to the owner's own request, not an association notice.
            needs_consent=False,
            subject_template=(
                "{% load i18n %}{% translate 'Your records request was fulfilled' %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate with on=received %}"
                "The board marked your records request of {{ on }} as fulfilled:"
                "{% endblocktranslate %}\n\n{{ what }}\n\n"
                "{% translate 'Questions? Message the board from the website.' %}"
            ),
        )
    )


def _offset(association, key: str, start: dt.date) -> dt.date | None:
    days = rules.value(association, key, start)
    return start + dt.timedelta(days=int(days)) if days is not None else None


@transaction.atomic
def log_request(
    association,
    *,
    received_on: dt.date,
    person: Person | None = None,
    requester_name: str = "",
    note: str = "",
    delivery: str = "",
) -> RecordsRequest:
    """Record an owner's records request with its due date (audited via the model)."""
    return RecordsRequest.objects.create(
        association=association,
        person=person,
        requester_name=requester_name.strip(),
        delivery=delivery,
        received_on=received_on,
        due_on=_offset(association, "records.inspection_days", received_on),
        latest_on=_offset(association, "records.inspection_max_days", received_on),
        note=note.strip(),
    )


@transaction.atomic
def submit(association, person: Person, *, what: str, delivery: str, on: dt.date):
    """An owner asks in the portal. Logged like a board entry; the board is notified."""
    from homeoa.notify.services import board_recipients, notify_after_commit

    req = log_request(association, received_on=on, person=person, note=what, delivery=delivery)
    notify_after_commit(
        RECEIVED_TYPE,
        board_recipients(association),
        {
            "owner": person.full_name,
            "what": req.note,
            "delivery": req.get_delivery_display(),
            "due": req.due_on.isoformat() if req.due_on else "",
            "url": getattr(settings, "HOMEOA_BASE_URL", "") + reverse("registry:records_requests"),
            "association_name": association.name,
        },
        obj=req,
        association=association,
    )
    return req


@transaction.atomic
def mark_fulfilled(request: RecordsRequest, on: dt.date) -> RecordsRequest:
    """Close the request; an owner who has a registry entry is told."""
    from homeoa.notify.services import notify_after_commit

    request.fulfilled_on = on
    request.save(update_fields=["fulfilled_on"])
    if request.person_id:
        notify_after_commit(
            FULFILLED_TYPE,
            [request.person],
            {
                "received": request.received_on.isoformat(),
                "what": request.note,
                "association_name": request.association.name,
            },
            obj=request,
            association=request.association,
        )
    return request


def todo_items(association) -> list[dict]:
    """One Home to-do per open records request, due on its inspection deadline."""
    items = []
    open_requests = RecordsRequest.all_associations.filter(
        association=association, fulfilled_on__isnull=True
    ).select_related("person")
    for req in open_requests:
        who = req.person.full_name if req.person_id else req.requester_name
        if req.due_on and req.latest_on:
            sub = _("Due %(due)s (at the latest %(latest)s with notice)") % {
                "due": req.due_on.isoformat(),
                "latest": req.latest_on.isoformat(),
            }
        elif req.due_on:
            sub = _("Due %(due)s") % {"due": req.due_on.isoformat()}
        else:
            sub = _("Received %(date)s") % {"date": req.received_on.isoformat()}
        items.append(
            {
                "id": f"records-request-{req.pk}",
                "kind": "deadline",
                "title": _("Records request from %(who)s") % {"who": who or "?"},
                "sub": sub,
                "due": req.due_on,
                "url": reverse("registry:records_requests"),
                "action": _("Open"),
            }
        )
    return items
