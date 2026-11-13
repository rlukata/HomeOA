"""Maintenance requests on the resident Requests tab: the ones the resident submitted."""

from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeoa.access import permissions as perms
from homeoa.access.services import has_perm
from homeoa.portal.contributions import RequestKind
from homeoa.portal.requests import Item

BADGES = {"new": "warn", "in_progress": "accent", "done": "ok"}


def _item(r, *, link: bool) -> Item:
    timeline = [
        (timezone.localdate(h.changed_at), h.new_label) for h in r.history.order_by("changed_at")
    ]
    return Item(
        kind="maintenance",
        kind_label=_("Maintenance"),
        icon="wrench",
        title=r.title,
        status=r.get_status_display(),
        badge=BADGES.get(r.status, ""),
        date=timezone.localdate(r.created_at),
        url=reverse("maintenance:my_detail", args=[r.pk]) if link else "",
        timeline=timeline,
    )


def for_user(request):
    """The requests the resident submitted; ``None`` when they cannot submit any."""
    from homeoa.maintenance.models import MaintenanceRequest
    from homeoa.registry.models import Person

    if not has_perm(request.user, perms.MAINTENANCE_SUBMIT, association=request.association):
        return None
    people = list(Person.objects.filter(user=request.user))
    if not people:
        return []
    return [_item(r, link=True) for r in MaintenanceRequest.objects.filter(submitted_by__in=people)]


def for_unit(unit, owner: bool):
    from homeoa.maintenance.models import MaintenanceRequest

    return [_item(r, link=False) for r in MaintenanceRequest.objects.filter(unit=unit)]


REQUEST_KIND = RequestKind(
    kind="maintenance",
    new_label=_("Report a problem"),
    new_icon="wrench",
    new_url_name="maintenance:my_add",
    new_testid="req-new-maint",
    for_user=for_user,
    for_unit=for_unit,
)
