"""Home to-dos: "Send notice by <date>" for each scheduled meeting not yet noticed, and
"Approve minutes" for a past meeting whose minutes are still a draft."""

from __future__ import annotations

from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.core.context import association_today

from . import services
from .models import Meeting, Minutes


def todo_items(association) -> list[dict]:
    today = association_today(association)
    items = []
    for meeting in services.upcoming_unnoticed(association, today):
        if hasattr(meeting, "annual"):
            continue  # the annual meeting workflow lists its own notice to-do
        window = services.notice_window(meeting, today)
        items.append(
            {
                "id": f"meetings-notice-{meeting.pk}",
                "kind": "gov",
                "title": _("Send notice by %(date)s: %(meeting)s")
                % {"date": date_format(window.latest, "M j"), "meeting": meeting.display_title},
                "sub": _("Meeting %(date)s") % {"date": date_format(meeting.date, "M j, Y")},
                "due": window.latest,
                "url": reverse("meetings:detail", args=[meeting.pk]),
                "action": _("Open"),
                "perm": perms.GOVERNANCE_MANAGE,
            }
        )
    for record in (
        Minutes.all_associations.filter(
            association=association,
            status=Minutes.Status.DRAFT,
            meeting__date__lt=today,
        )
        .exclude(meeting__status=Meeting.Status.CANCELLED)
        .select_related("meeting")
    ):
        meeting = record.meeting
        items.append(
            {
                "id": f"meetings-minutes-{meeting.pk}",
                "kind": "gov",
                "title": _("Approve minutes of %(meeting)s at the next meeting")
                % {"meeting": meeting.display_title},
                "sub": _("Minutes still in draft · meeting %(date)s")
                % {"date": date_format(meeting.date, "M j, Y")},
                "due": today,
                "url": reverse("meetings:minutes", args=[meeting.pk]),
                "action": _("Open"),
                "perm": perms.GOVERNANCE_MANAGE,
            }
        )
    return items
