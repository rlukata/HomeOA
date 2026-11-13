"""Owner maintenance requests in search."""

from __future__ import annotations

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.search.services import LIMIT, Hit


def maintenance_requests(user, association, query):
    from homeoa.maintenance.models import MaintenanceRequest

    if not has_perm(user, perms.MAINTENANCE_MANAGE, association=association):
        return []
    number = query[5:].strip() if query.lower().startswith("unit ") else query
    found = (
        MaintenanceRequest.all_associations.filter(association=association)
        .filter(
            Q(title__icontains=query)
            | Q(description__icontains=query)
            | Q(location__icontains=query)
            | Q(unit__number__iexact=number)
        )
        .select_related("unit")
        .order_by("-created_at", "-pk")[:LIMIT]
    )
    return [
        Hit(
            _("Maintenance requests"),
            r.title,
            reverse("maintenance:detail", args=[r.pk]),
            " · ".join(x for x in (str(r.unit or ""), str(r.get_status_display())) if x),
        )
        for r in found
    ]
