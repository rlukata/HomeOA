"""Work orders in search."""

from __future__ import annotations

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.search.services import LIMIT, Hit, name_match


def work_orders(user, association, query):
    from homeoa.maintenance.models import WorkOrder

    if not has_perm(user, perms.MAINTENANCE_MANAGE, association=association):
        return []
    digits = query.lstrip("#")
    found = (
        WorkOrder.all_associations.filter(association=association)
        .filter(
            Q(scope__icontains=query)
            | name_match("vendor__name", query)
            | (Q(pk=int(digits)) if digits.isdigit() else Q(pk__in=[]))
        )
        .select_related("vendor")
        .order_by("-created_at", "-pk")[:LIMIT]
    )
    return [
        Hit(
            _("Work orders"),
            _("Work order #%(n)s, %(vendor)s") % {"n": wo.pk, "vendor": wo.vendor.name},
            reverse("maintenance:work_order", args=[wo.pk]),
            " · ".join(
                (wo.scope.splitlines()[0][:80] if wo.scope else "", str(wo.get_status_display()))
            ).strip(" ·"),
        )
        for wo in found
    ]
