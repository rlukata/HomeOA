from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.registry.models import Unit
from homeoa.reports.export import DATE, Column, TableSpec
from homeoa.ui.export import export_format, export_response

from . import commlog, inbox
from .models import Delivery


def can_read_comms(request) -> bool:
    assoc = getattr(request, "association", None)
    return has_perm(request.user, perms.BOARD_SHELL, association=assoc) and has_perm(
        request.user, perms.UNITS_READ, association=assoc
    )


@require_perm(perms.UNITS_READ)
def unit_comms(request, number):
    if not can_read_comms(request):
        raise PermissionDenied
    unit = get_object_or_404(Unit.objects, number=number)
    filters = commlog.Filters.from_query(request.GET)
    rows = list(commlog.entries(unit, filters))
    if fmt := export_format(request):
        spec = TableSpec(
            _("Unit %(n)s communications") % {"n": unit.number},
            [
                Column(_("Date"), DATE),
                Column(_("Recipient")),
                Column(_("Channel")),
                Column(_("Subject")),
                Column(_("Status")),
                Column(_("Detail")),
            ],
            [
                (
                    commlog.last_event_at(d),
                    d.notification.recipient.full_name,
                    commlog.channel_label(d.channel),
                    d.notification.subject,
                    d.get_status_display(),
                    d.error,
                )
                for d in rows
            ],
            filename=f"unit-{unit.number}-comms",
            pii=True,
        )
        return export_response(request, spec, fmt, audit_obj=unit)
    return render(
        request,
        "notify/unit_comms.html",
        {
            "unit": unit,
            "rows": [
                {
                    "d": d,
                    "channel": commlog.channel_label(d.channel),
                    "at": commlog.last_event_at(d),
                    "tone": commlog.status_tone(d.status),
                }
                for d in rows
            ],
            "filters": filters,
            "channel_choices": list(commlog.CHANNEL_LABELS.items()),
            "status_choices": Delivery.Status.choices,
        },
    )


@require_POST
@require_perm(perms.UNITS_MANAGE)
def delivery_handled(request, pk: int):
    """Clear a bounced or failed delivery from the Home to-do list."""
    delivery = get_object_or_404(Delivery.objects, pk=pk)
    if delivery.status in (Delivery.Status.BOUNCED, Delivery.Status.FAILED):
        if delivery.resolved_at is None:
            delivery.resolved_at = timezone.now()
            delivery.save(update_fields=["resolved_at"])
        messages.success(request, _("Marked as handled."))
    unit = delivery.notification.unit
    if unit is not None:
        return redirect("notify:unit_comms", number=unit.number)
    return redirect("dashboard:index")


def _closed(request):
    """None when the user may see the inbox, else the "not open yet" response."""
    if inbox.can_see(request):
        return None
    return render(request, "portal/closed.html", {"tabs": []}, status=403)


@login_required
def inbox_list(request):
    """The signed-in user's in-app notifications, in the board or resident shell."""
    if (closed := _closed(request)) is not None:
        return closed
    rows = list(inbox.for_user(request)[:200])
    context = {"rows": rows, "unread": any(n.read_at is None for n in rows)}
    if inbox.is_board_view(request):
        context["base_template"] = "ui/base.html"
    else:
        from homeoa.portal.nav import get_tabs

        context.update(base_template="portal/base.html", tabs=get_tabs(request))
    return render(request, "notify/inbox.html", context)


@require_POST
@login_required
def inbox_read(request, pk: int):
    if (closed := _closed(request)) is not None:
        return closed
    note = get_object_or_404(inbox.for_user(request), pk=pk)
    if note.read_at is None:
        note.read_at = timezone.now()
        note.save(update_fields=["read_at"])
    return redirect("notify:inbox")


@require_POST
@login_required
def inbox_read_all(request):
    if (closed := _closed(request)) is not None:
        return closed
    count = inbox.for_user(request).filter(read_at__isnull=True).update(read_at=timezone.now())
    if count:
        messages.success(request, _("All notifications marked as read."))
    return redirect("notify:inbox")
