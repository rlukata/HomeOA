"""Requests the law deems approved when not denied in writing in time.

Where the rule pack sets ``arc.<kind>.deemed_approval_days`` (WA: 60 days for EV charging
stations, RCW 64.90.513(3)(c), and heat pumps, RCW 64.90.580(3)(c)), a request on a form
marked with that kind must be denied in writing by ``deny_by``: the day it was received
plus those days. Time spent waiting for the owner's answer to a request for more
information doesn't count ("unless that delay is the result of a reasonable request for
additional information"), so ``deny_by`` moves later by those days.

The board's own deadline (``decide_by``) never falls after ``deny_by``, so the usual
reminders and the overdue to-do come first. On the day after ``deny_by`` the daily
``arc.deemed_approvals`` job records the approval as the law has it: the request becomes
approved, the decision letter says why, and reviewers are told. Every other request keeps
the note-only ``arc.deemed_approved`` behaviour.
"""

from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa import audit, rules
from homeoa.notify import channels as ch
from homeoa.notify import types as ntypes

from .models import ArcRequest, StatutoryKind

DEEMED = "arc.deemed_approved"


def rule_key(kind: str) -> str:
    return f"arc.{kind}.deemed_approval_days"


def days(association, kind: str, on: dt.date | None = None) -> int | None:
    if not kind:
        return None
    return rules.value(association, rule_key(kind), on)


def citation(association, kind: str, on: dt.date | None = None) -> str:
    return rules.resolve(association, rule_key(kind), on).citation or ""


def update(req: ArcRequest) -> None:
    """Set ``deny_by`` and cap ``decide_by`` by it (the caller saves)."""
    limit = days(req.association, req.statutory_kind, req.submitted_on)
    if limit is None:
        req.deny_by = None
        return
    req.deny_by = req.submitted_on + dt.timedelta(days=limit + req.paused_days)
    if req.decide_by is None or req.decide_by > req.deny_by:
        req.decide_by = req.deny_by


def info_requested(req: ArcRequest, on: dt.date) -> None:
    req.info_requested_on = on


def info_provided(req: ArcRequest, on: dt.date) -> None:
    """The owner answered: the days spent waiting push ``deny_by`` back."""
    if req.info_requested_on is not None:
        req.paused_days += max((on - req.info_requested_on).days, 0)
        req.info_requested_on = None


@transaction.atomic
def set_kind(req: ArcRequest, kind: str, *, user) -> ArcRequest:
    """The board marks an open request as an EV charger or heat pump request (or not).
    The deadline still counts from the day the request was received."""
    from . import deadlines

    if kind not in StatutoryKind.values:
        raise ValueError(kind)
    old = req.statutory_kind
    req.statutory_kind = kind
    deadlines.start_clock(req, req.clock_started_on or req.submitted_on)
    update(req)
    req.save(update_fields=["statutory_kind", "decide_by", "deny_by"])
    audit.record("arc.statutory_kind", req, actor=user, changes={"statutory_kind": [old, kind]})
    return req


def record_deemed_approvals(association, as_of: dt.date) -> int:
    """Approve every request still with the board after its ``deny_by``. Idempotent."""
    from . import decision_letters, services

    due = ArcRequest.all_associations.filter(
        association=association,
        status__in=ArcRequest.WAITING_ON_BOARD,
        deny_by__lt=as_of,
    ).select_related("unit")
    count = 0
    for req in due:
        with transaction.atomic():
            req.status = ArcRequest.Status.APPROVED
            req.deemed = True
            req.decided_on = req.deny_by + dt.timedelta(days=1)
            req.decided_by = None
            req.citations = citation(association, req.statutory_kind, req.submitted_on)
            req.decision_note = _(
                "Approved by law: the request was not denied in writing by %(date)s."
            ) % {"date": date_format(req.deny_by, "F j, Y")}
            req.save()
            audit.record(
                "arc.deemed_approved",
                req,
                association=association,
                changes={"deny_by": req.deny_by.isoformat()},
            )
            decision_letters.schedule(req)
            _tell_reviewers(req, services.reviewers(association, as_of), as_of)
        count += 1
    return count


def _tell_reviewers(req: ArcRequest, people, as_of) -> None:
    from homeoa.notify.services import notify_after_commit

    if not people:
        return
    base = getattr(settings, "HOMEOA_BASE_URL", "") or ""
    notify_after_commit(
        DEEMED,
        people,
        {
            "title": req.title,
            "unit": str(req.unit),
            "deny_by": date_format(req.deny_by, "F j, Y"),
            "url": base + reverse("arc:detail", args=[req.pk]),
        },
        as_of,
        obj=req,
        association=req.association,
        unit=req.unit,
    )


def register_notification_type() -> None:
    ntypes.register(
        ntypes.NotificationType(
            module="arc",
            key=DEEMED,
            label=gettext_lazy("Architectural request: deemed approved by law"),
            default_channels=(ch.INAPP, ch.EMAIL),
            audience=ntypes.AUDIENCE_ALL,
            needs_consent=False,  # reviewers' working mail, like the deadline reminders
            subject_template=(
                "{% load i18n %}{% blocktranslate %}Deemed approved: {{ title }}"
                "{% endblocktranslate %}"
            ),
            body_template=(
                "{% load i18n %}{% blocktranslate %}The architectural request “{{ title }}”"
                " ({{ unit }}) was not denied in writing by {{ deny_by }}, so the law treats"
                " it as approved. HomeOA recorded the approval and sent the owner a letter."
                "{% endblocktranslate %}\n\n{{ url }}"
            ),
        )
    )
