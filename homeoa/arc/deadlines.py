"""ARC-03: decision deadlines and reminders to reviewers.

``decide_by`` = the clock start (submission, or the day the owner answered a request for
more information) + the ``arc.decision_days`` rule. The value is association policy from
the declaration, kept in the rule pack (generic default, board override); HomeOA never
approves a request on its own, even when ``arc.deemed_approved`` is on: that rule only
shows a note.

Reminders go to the reviewers ``arc.first_reminder_days`` and ``arc.final_reminder_days``
before the deadline, from the daily ``arc.deadline_reminders`` job. Each one is sent once
per deadline (the notification idempotency key carries the deadline and the offset), so
a rerun or catch-up never repeats it, and a reset deadline gets fresh reminders.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy

from homeoa import rules
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from .models import ArcRequest

REMINDER = "arc.decision_reminder"
REMINDER_KEYS = ("arc.first_reminder_days", "arc.final_reminder_days")


def decision_days(association, on: dt.date | None = None) -> int | None:
    return rules.value(association, "arc.decision_days", on)


def start_clock(req: ArcRequest, on: dt.date) -> None:
    """Set the clock start and ``decide_by`` (the caller saves)."""
    days = decision_days(req.association, on)
    req.clock_started_on = on
    req.decide_by = on + dt.timedelta(days=days) if days is not None else None


def deemed_approved(association) -> bool:
    return bool(rules.value(association, "arc.deemed_approved"))


def offsets(association, on: dt.date | None = None) -> list[int]:
    out = {rules.value(association, key, on) for key in REMINDER_KEYS}
    return sorted((o for o in out if o is not None), reverse=True)


def send_reminders(association, as_of: dt.date) -> int:
    """Remind reviewers of open requests whose deadline is within an offset. Returns the
    number of (request, offset) reminders due today (already-sent ones are no-ops)."""
    from homeoa.notify.services import notify_after_commit

    from . import services

    offs = offsets(association, as_of)
    if not offs:
        return 0
    people = services.reviewers(association, as_of)
    if not people:
        return 0
    base = getattr(settings, "HOMEOA_BASE_URL", "") or ""
    count = 0
    reqs = ArcRequest.all_associations.filter(
        association=association,
        status__in=[ArcRequest.Status.SUBMITTED, ArcRequest.Status.UNDER_REVIEW],
        decide_by__isnull=False,
        decide_by__gte=as_of,
    ).select_related("unit")
    for req in reqs:
        left = (req.decide_by - as_of).days
        # The tightest offset reached; earlier, larger ones are not sent late.
        due = [o for o in offs if left <= o]
        if not due:
            continue
        offset = min(due)
        count += 1
        notify_after_commit(
            REMINDER,
            people,
            {
                "title": req.title,
                "unit": str(req.unit),
                "decide_by": date_format(req.decide_by, "F j, Y"),
                "days": left,
                "url": base + reverse("arc:detail", args=[req.pk]),
            },
            as_of,
            obj=req,
            period=f"{req.decide_by.isoformat()}:{offset}",
            association=association,
            unit=req.unit,
        )
    return count


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            module="arc",
            key=REMINDER,
            label=gettext_lazy("Architectural request: decision deadline reminder"),
            default_channels=(ch.INAPP, ch.EMAIL),
            audience=ntypes.AUDIENCE_ALL,
            needs_consent=False,  # reviewers' working mail, like AP approvals
            subject_template=(
                "{% load i18n %}{% blocktranslate %}Decide by {{ decide_by }}: {{ title }}"
                "{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate count days=days %}The architectural request"
                " “{{ title }}” ({{ unit }}) must be decided by {{ decide_by }}, in"
                " {{ days }} day.{% plural %}The architectural request “{{ title }}”"
                " ({{ unit }}) must be decided by {{ decide_by }}, in {{ days }} days."
                "{% endblocktranslate %}\n\n{{ url }}"
            ),
        )
    )
