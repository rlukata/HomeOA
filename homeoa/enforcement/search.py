"""Violations in search. Who reported a violation is never shown."""

from __future__ import annotations

from django.db.models import Q
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.search.services import LIMIT, Hit


def violations(user, association, query):
    from homeoa.enforcement.models import Violation

    if not has_perm(user, perms.VIOLATIONS_MANAGE, association=association):
        return []
    number = query[5:].strip() if query.lower().startswith("unit ") else query
    found = (
        Violation.all_associations.filter(association=association)
        .filter(
            Q(description__icontains=query)
            | Q(status_note__icontains=query)
            | Q(category__name__icontains=query)
            | Q(unit__number__iexact=number)
        )
        .select_related("unit", "category")
        .order_by("-observed_on", "-pk")[:LIMIT]
    )
    return [
        Hit(
            _("Violations"),
            f"{v.category.name} · {v.unit}",
            reverse("enforcement:detail", args=[v.pk]),
            f"{date_format(v.observed_on, 'M j, Y')} · {v.get_status_display()}",
        )
        for v in found
    ]
