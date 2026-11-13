"""Design approval requests in search."""

from __future__ import annotations

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext as _

from homeoa.access import permissions as perms
from homeoa.access.services import has_any
from homeoa.search.services import LIMIT, Hit


def design_requests(user, association, query):
    from homeoa.arc.models import ArcRequest

    codes = (perms.ARCH_REVIEW, perms.ARCH_DECIDE)
    if not has_any(user, codes, association=association):
        return []
    number = query[5:].strip() if query.lower().startswith("unit ") else query
    found = (
        ArcRequest.all_associations.filter(association=association)
        .filter(
            Q(title__icontains=query)
            | Q(conditions__icontains=query)
            | Q(decision_note__icontains=query)
            | Q(unit__number__iexact=number)
        )
        .select_related("unit")
        .order_by("-submitted_on", "-pk")[:LIMIT]
    )
    return [
        Hit(
            _("Design requests"),
            r.title,
            reverse("arc:detail", args=[r.pk]),
            f"{r.unit} · {r.get_status_display()}",
        )
        for r in found
    ]
