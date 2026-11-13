"""Unit file contributors (DOC-02): the unit's maintenance requests and work orders, for
users who manage maintenance."""

from __future__ import annotations

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.documents.unitfile import Entry


def _can(request) -> bool:
    return has_perm(
        request.user,
        perms.MAINTENANCE_MANAGE,
        association=getattr(request, "association", None),
    )


def requests(unit, request):
    if not _can(request):
        return []
    from .models import MaintenanceRequest

    qs = MaintenanceRequest.all_associations.filter(association_id=unit.association_id, unit=unit)
    return [
        Entry(
            date=timezone.localdate(r.created_at),
            kind="maintenance",
            kind_label=gettext_lazy("Maintenance request"),
            title=r.title,
            url=reverse("maintenance:detail", args=[r.pk]),
            detail=r.get_status_display(),
        )
        for r in qs
    ]


def work_orders(unit, request):
    if not _can(request):
        return []
    from .models import WorkOrder

    qs = WorkOrder.all_associations.filter(
        association_id=unit.association_id, request__unit=unit
    ).select_related("vendor")
    return [
        Entry(
            date=timezone.localdate(w.created_at),
            kind="work_orders",
            kind_label=gettext_lazy("Work order"),
            title=w.scope[:120],
            url=reverse("maintenance:work_order", args=[w.pk]),
            detail=f"{w.vendor} · {w.get_status_display()}",
        )
        for w in qs
    ]


def register() -> None:
    from homeoa.documents import unitfile

    unitfile.register("maintenance", requests, module="owner_requests")
    unitfile.register("work_orders", work_orders, module="work_orders")
