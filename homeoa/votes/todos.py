"""Home to-do: "Vote closes <date>: N units outstanding" for each open vote."""

from __future__ import annotations

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from homeoa.access import permissions as perms

from .models import Vote, VoteStatus
from .services import _tz
from .views import turnout


def todo_items(association) -> list[dict]:
    items = []
    for vote in Vote.all_associations.filter(association=association, status=VoteStatus.OPEN):
        t = turnout(vote)
        closes = timezone.localdate(vote.closes_at, _tz(association))
        items.append(
            {
                "id": f"votes-open-{vote.pk}",
                "kind": "gov",
                "title": ngettext(
                    "Vote closes %(date)s: %(n)s unit outstanding",
                    "Vote closes %(date)s: %(n)s units outstanding",
                    t["outstanding"],
                )
                % {"date": date_format(closes, "M j"), "n": t["outstanding"]},
                "sub": _("%(title)s · %(voted)s of %(eligible)s units voted")
                % {"title": vote.title, **t},
                "due": closes,
                "url": reverse("votes:detail", args=[vote.pk]),
                "action": _("Open"),
                "perm": perms.VOTES_ADMINISTER,
            }
        )
    return items
