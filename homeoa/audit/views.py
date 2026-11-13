import datetime

from django.apps import apps
from django.core.paginator import Paginator
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_association_perm

from .models import AuditEvent

PAGE_SIZE = 50

# Friendlier names for object types whose model name reads badly on the board.
TYPE_LABELS = {"core.association": _("Association settings")}


def type_label(object_type: str) -> str:
    if not object_type:
        return ""
    if object_type in TYPE_LABELS:
        return str(TYPE_LABELS[object_type])
    try:
        return str(apps.get_model(object_type)._meta.verbose_name).capitalize()
    except LookupError, ValueError:
        return object_type


def _date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _fmt(value) -> str:
    if value is None or value == "":
        return "—"
    return str(value)


def _change_items(changes) -> list[tuple[str, str]]:
    """``[(field, "old → new"), ...]``; encrypted fields read "changed"."""
    items = []
    if not isinstance(changes, dict):
        return items
    for name, value in changes.items():
        if isinstance(value, list) and len(value) == 2:
            items.append((name, f"{_fmt(value[0])} → {_fmt(value[1])}"))
        else:
            items.append((name, str(_("changed"))))
    return items


@require_perm(perms.AUDIT_READ)
def audit_log(request):
    """Read-only list of audit events.

    Board (or any association role with audit.read) sees this association's events. An
    instance admin without such a role sees only events outside any association.
    """
    if has_association_perm(request.user, perms.AUDIT_READ):
        base = AuditEvent.objects.all()
    else:
        base = AuditEvent.all_associations.filter(association__isnull=True)
    events = base
    f = {
        "action": request.GET.get("action", "").strip(),
        "object_type": request.GET.get("object_type", "").strip(),
        "actor": request.GET.get("actor", "").strip(),
        "since": _date(request.GET.get("since")),
        "until": _date(request.GET.get("until")),
    }
    if f["action"]:
        events = events.filter(action__icontains=f["action"])
    if f["object_type"]:
        events = events.filter(object_type=f["object_type"])
    if f["actor"]:
        events = events.filter(actor_label__icontains=f["actor"])
    if f["since"]:
        events = events.filter(created_at__date__gte=f["since"])
    if f["until"]:
        events = events.filter(created_at__date__lte=f["until"])
    object_types = (
        base.exclude(object_type="")
        .order_by("object_type")
        .values_list("object_type", flat=True)
        .distinct()
    )
    page = Paginator(events, PAGE_SIZE).get_page(request.GET.get("page"))
    for event in page:
        event.type_label = type_label(event.object_type)
        event.change_items = _change_items(event.changes)
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "audit/log.html",
        {
            "current_hub": None,
            "subtabs": [],
            "page_obj": page,
            "filters": f,
            "object_types": [(t, type_label(t)) for t in object_types],
            "query": query.urlencode(),
        },
    )
