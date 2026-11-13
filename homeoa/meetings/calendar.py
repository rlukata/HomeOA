"""Calendar source: meetings as events for the community calendar.

Board-only while a draft; once noticed (or held) everyone in the association sees it.
``apps.ready`` registers ``events`` with ``community.services.register_source``.
"""

from __future__ import annotations

import datetime as dt

from django.urls import reverse

from homeoa.community.models import Audience
from homeoa.community.services import Occurrence

from .models import Meeting

SOURCE_KEY = "meetings"


def events(association, start: dt.date, end: dt.date) -> list[Occurrence]:
    qs = (
        Meeting.all_associations.filter(association=association, date__gte=start, date__lte=end)
        .exclude(status=Meeting.Status.CANCELLED)
        .order_by("date", "start_time", "pk")
    )
    return [
        Occurrence(
            uid=f"meeting-{m.pk}@homeoa",
            title=m.display_title,
            start_date=m.date,
            start_time=m.start_time,
            location=m.location or m.video_url,
            description=m.video_url if m.location else "",
            audience=Audience.BOARD if m.status == Meeting.Status.DRAFT else Audience.ALL,
            source=SOURCE_KEY,
            url=reverse("meetings:detail", args=[m.pk]),
        )
        for m in qs
    ]
