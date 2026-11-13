"""Unit file contributor (DOC-02): the unit's violations, board only. Owners and tenants
see theirs on the Requests tab (``responses``)."""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.documents.unitfile import Entry


def violations(unit, request):
    if not has_perm(
        request.user, perms.VIOLATIONS_MANAGE, association=getattr(request, "association", None)
    ):
        return []
    from .models import Violation

    qs = Violation.all_associations.filter(
        association_id=unit.association_id, unit=unit
    ).select_related("category")
    out = []
    for v in qs:
        detail = v.get_status_display()
        if v.cured_on:
            detail = _("%(status)s · cured %(date)s") % {
                "status": detail,
                "date": v.cured_on.isoformat(),
            }
        out.append(
            Entry(
                date=v.observed_on,
                kind="violations",
                kind_label=gettext_lazy("Violation"),
                title=str(v.category),
                url=reverse("enforcement:detail", args=[v.pk]),
                detail=detail,
            )
        )
    return out


def register() -> None:
    from homeoa.documents import unitfile

    unitfile.register("violations", violations, module="violations")
