"""Home to-dos: board terms ending soon, and open action items for their assignee."""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.core.context import association_today

from . import services
from .models import ActionItem


def todo_items(association) -> list[dict]:
    today = association_today(association)
    url = reverse("governance:index")
    items = []
    for term in services.ending_terms(association, today):
        items.append(
            {
                "id": f"governance-term-end-{term.pk}",
                "kind": "gov",
                "title": _("Board term ends: %(person)s (%(office)s)")
                % {"person": term.person, "office": term.get_office_display()},
                "sub": _("Term ends %(date)s. Plan the election or reappointment.")
                % {"date": term.end.isoformat()},
                "due": term.end,
                "url": url,
                "action": _("Open"),
                "perm": perms.GOVERNANCE_MANAGE,
            }
        )
    open_items = ActionItem.all_associations.filter(
        association=association, status=ActionItem.Status.OPEN
    ).select_related("owner", "resolution")
    for item in open_items:
        user_id = item.owner.user_id
        if user_id is None:
            continue  # no login: nobody to show it to
        sub = (
            _("Resolution %(number)s") % {"number": item.resolution.number}
            if item.resolution
            else _("Board action item")
        )
        items.append(
            {
                "id": f"governance-action-{item.pk}",
                "kind": "gov",
                "title": item.title,
                "sub": sub,
                "due": item.due_date,
                "url": url + f"#action-{item.pk}",
                "action": _("Open"),
                "for_user": user_id,
                "perm": perms.GOVERNANCE_READ,
            }
        )
    return items
