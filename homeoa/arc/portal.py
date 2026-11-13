"""Design approval requests on the resident Requests tab.

Owners see the requests of the units they own now; tenants see none.
"""

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.portal.contributions import RequestKind
from homeoa.portal.requests import Item

BADGES = {
    "denied": "danger",
    "info_requested": "warn",
    "approved": "ok",
    "approved_with_conditions": "ok",
}


def _item(r, *, link: bool) -> Item:
    timeline = [(r.submitted_on, str(_("Submitted")))]
    if r.decided_on:
        timeline.append((r.decided_on, r.get_status_display()))
    return Item(
        kind="arc",
        kind_label=_("Architectural"),
        icon="pencil",
        title=r.title,
        status=r.get_status_display(),
        badge=BADGES.get(r.status, ""),
        date=r.submitted_on,
        url=reverse("arc:mine_detail", args=[r.pk]) if link else "",
        timeline=timeline,
    )


def for_user(request):
    """The requests of the units the resident owns now; ``None`` when they own none."""
    from homeoa.arc.models import ArcRequest
    from homeoa.registry.models import Person
    from homeoa.registry.services import units_owned_by

    if not has_perm(request.user, perms.ARCH_SUBMIT, association=request.association):
        return None
    people = Person.objects.filter(user=request.user)
    owned = {u.pk for p in people for u in units_owned_by(p)}
    if not owned:
        return None
    return [
        _item(r, link=True)
        for r in ArcRequest.objects.filter(unit__in=owned).select_related("unit")
    ]


def for_unit(unit, owner: bool):
    from homeoa.arc.models import ArcRequest

    if not owner:
        return None
    return [_item(r, link=False) for r in ArcRequest.objects.filter(unit=unit)]


REQUEST_KIND = RequestKind(
    kind="arc",
    new_label=_("Ask to change my exterior"),
    new_icon="pencil",
    new_url_name="arc:mine_new",
    new_testid="req-new-arc",
    for_user=for_user,
    for_unit=for_unit,
)
