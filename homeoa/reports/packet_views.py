"""Money > Reports > Board packet: download any month; board members file and email it."""

import datetime as dt

from django.core.exceptions import PermissionDenied
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.core.context import association_today

from .views import _ctx


def _can_send_packet(request) -> bool:
    from homeoa.access.services import has_any

    return has_any(
        request.user,
        (perms.FINANCE_MANAGE, perms.FINANCE_PROPOSE),
        association=request.association,
    )


@require_perm(perms.FINANCE_READ)
def board_packet_view(request):
    """Monthly board packet: download any month; board members file and email it."""
    from django.contrib import messages
    from django.http import HttpResponse
    from django.shortcuts import redirect

    from . import packet

    association = request.association
    today = association_today(association)
    last = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    raw = request.POST.get("month") or request.GET.get("month") or f"{last:%Y-%m}"
    try:
        year, month = (int(x) for x in raw.split("-"))
        dt.date(year, month, 1)
    except ValueError:
        year, month = last.year, last.month
    if request.method == "POST":
        if not _can_send_packet(request):
            raise PermissionDenied
        packet.file_and_send(association, year, month, user=request.user)
        messages.success(request, _("Board packet filed under Documents and emailed to the board."))
        return redirect(f"{request.path}?month={year}-{month:02d}")
    if request.GET.get("download"):
        response = HttpResponse(
            packet.build(association, year, month), content_type="application/pdf"
        )
        response["Content-Disposition"] = (
            f'attachment; filename="board-packet-{year}-{month:02d}.pdf"'
        )
        response["Cache-Control"] = "private, no-store"
        return response
    months = []
    day = today.replace(day=1)
    for _i in range(12):
        day = (day - dt.timedelta(days=1)).replace(day=1)
        months.append(day)
    ctx = _ctx(request, _("Monthly board packet"), "board_packet")
    ctx.update(
        {
            "months": months,
            "picked": f"{year}-{month:02d}",
            "can_send": _can_send_packet(request),
        }
    )
    return render(request, "reports/board_packet.html", ctx)
