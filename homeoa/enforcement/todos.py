"""Home to-do: open violations for the board."""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from homeoa.access import permissions as perms

from . import services


def todo_items(association) -> list[dict]:
    from homeoa.core.context import association_today

    from . import ladder

    items = []
    for v in ladder.cure_overdue(association, association_today(association)).select_related(
        "unit", "category"
    ):
        items.append(
            {
                "id": f"enforcement-cure-passed-{v.pk}",
                "kind": "enforce",
                "title": _("Cure deadline passed: schedule hearing"),
                "sub": _("Unit %(unit)s, %(category)s") % {"unit": v.unit, "category": v.category},
                "url": reverse("enforcement:detail", args=[v.pk]),
                "action": _("Open"),
                "perm": perms.VIOLATIONS_MANAGE,
            }
        )
    from .models import Hearing

    for h in Hearing.all_associations.filter(
        association=association,
        outcome="",
        cancelled_on__isnull=True,
        hearing_on__lte=association_today(association),
        violation__status="open",
    ).select_related("violation__unit", "violation__category"):
        v = h.violation
        items.append(
            {
                "id": f"enforcement-hearing-decision-{h.pk}",
                "kind": "enforce",
                "title": _("Record hearing decision"),
                "sub": _("Unit %(unit)s, %(category)s") % {"unit": v.unit, "category": v.category},
                "url": reverse("enforcement:detail", args=[v.pk]),
                "action": _("Open"),
                "perm": perms.VIOLATIONS_MANAGE,
            }
        )
    from . import responses

    for v in responses.disputes_without_hearing(association):
        items.append(
            {
                "id": f"enforcement-dispute-{v.pk}",
                "kind": "enforce",
                "title": _("Owner disagrees: offer a hearing"),
                "sub": _("Unit %(unit)s, %(category)s") % {"unit": v.unit, "category": v.category},
                "url": reverse("enforcement:detail", args=[v.pk]),
                "action": _("Open"),
                "perm": perms.VIOLATIONS_MANAGE,
            }
        )
    n = services.open_violations(association).count()
    if not n:
        return items
    return items + [
        {
            "id": "enforcement-open-violations",
            "kind": "enforce",
            "title": ngettext("%(n)s open violation", "%(n)s open violations", n) % {"n": n},
            "sub": _("Follow up, mark cured or close."),
            "url": reverse("enforcement:index") + "?status=open",
            "action": _("Review"),
            "perm": perms.VIOLATIONS_MANAGE,
        }
    ]
