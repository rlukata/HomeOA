"""Unit file contributor (DOC-02): the unit's architectural requests, for reviewers."""

from __future__ import annotations

from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from homeoa.access import permissions as perms
from homeoa.access.services import has_any
from homeoa.documents.unitfile import Entry


def arc_requests(unit, request):
    if not has_any(
        request.user,
        (perms.ARCH_REVIEW, perms.ARCH_DECIDE),
        association=getattr(request, "association", None),
    ):
        return []
    from .models import ArcRequest

    qs = ArcRequest.all_associations.filter(association_id=unit.association_id, unit=unit)
    out = []
    for r in qs:
        detail = r.get_status_display()
        if r.decided_on:
            detail = _("%(status)s · decided %(date)s") % {
                "status": detail,
                "date": r.decided_on.isoformat(),
            }
        out.append(
            Entry(
                date=r.submitted_on,
                kind="arc",
                kind_label=gettext_lazy("Architectural request"),
                title=r.title,
                url=reverse("arc:detail", args=[r.pk]),
                detail=detail,
            )
        )
    return out


def register() -> None:
    from homeoa.documents import unitfile

    unitfile.register("arc", arc_requests, module="arc")
