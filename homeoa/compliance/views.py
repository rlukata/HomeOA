import datetime as dt

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm
from homeoa.access.services import has_perm
from homeoa.core.context import association_today
from homeoa.providers import get_provider

from . import calendar as cal
from . import services
from .forms import ManualItemForm
from .models import Evidence, ManualItem, ObligationPeriod, PeriodStatus

PAST_DAYS = 60
FUTURE_DAYS = 400


def _can_manage(request) -> bool:
    return has_perm(request.user, perms.CALENDAR_MANAGE, association=request.association)


def _manage(request) -> None:
    if not _can_manage(request):
        raise PermissionDenied


@require_perm(perms.BOARD_SHELL)
def index(request):
    periods = list(ObligationPeriod.objects.order_by("due_date", "obligation_id"))
    today = association_today(request.association)
    upcoming = cal.entries(
        request.association,
        today - dt.timedelta(days=PAST_DAYS),
        today + dt.timedelta(days=FUTURE_DAYS),
    )
    overdue_ids = {i.pk for i in cal.overdue_items(request.association, today)}
    return render(
        request,
        "compliance/index.html",
        {
            "upcoming": [(e, e.overdue(today)) for e in upcoming],
            "items": [(i, i.pk in overdue_ids) for i in ManualItem.objects.filter(active=True)],
            "offsets": cal.reminder_offsets(request.association),
            "suggestions": [(k, v[0]) for k, v in cal.SUGGESTIONS.items()],
            "open_periods": [p for p in periods if p.status == PeriodStatus.OPEN],
            "closed_periods": [p for p in periods if p.status != PeriodStatus.OPEN][::-1],
            "today": association_today(request.association),
            "can_manage": _can_manage(request),
        },
    )


@require_POST
@require_perm(perms.BOARD_SHELL)
def check_now(request):
    _manage(request)
    opened = services.run(request.association, association_today(request.association))
    messages.success(
        request, _("Obligations checked: %(n)s new period(s) opened.") % {"n": len(opened)}
    )
    return redirect("compliance:index")


@require_perm(perms.BOARD_SHELL)
def period(request, pk):
    p = get_object_or_404(ObligationPeriod.objects, pk=pk)
    ob = services.obligation_for(p)
    exemptions = []
    if ob is not None:
        for e in ob.exemptions:
            exemptions.append({"e": e, "answer": (p.exemption_answers or {}).get(e.id)})
    path_views = services.paths(p)
    return render(
        request,
        "compliance/period.html",
        {
            "period": p,
            "obligation": ob,
            "paths": path_views,
            "chosen": next((pv for pv in path_views if pv.id == p.path_id), None),
            "exemptions": exemptions,
            "evidence": list(p.evidence.all()),
            "can_manage": _can_manage(request) and p.is_open,
        },
    )


@require_POST
@require_perm(perms.BOARD_SHELL)
def act(request, pk):
    _manage(request)
    p = get_object_or_404(ObligationPeriod.objects, pk=pk)
    action = request.POST.get("action", "")
    try:
        if action == "path":
            services.choose_path(p, request.POST.get("path", ""), request.user)
            messages.success(request, _("Path recorded."))
        elif action == "exemption":
            applies = request.POST.get("answer") == "yes"
            services.answer_exemption(p, request.POST.get("exemption", ""), applies, request.user)
            if applies:
                messages.success(request, _("Exemption recorded; this period is closed."))
            else:
                messages.success(request, _("Answer recorded."))
        elif action == "evidence":
            services.add_evidence(
                p,
                upload=request.FILES.get("file"),
                url=request.POST.get("url", ""),
                note=request.POST.get("note", ""),
                user=request.user,
            )
            messages.success(request, _("Evidence added."))
        elif action == "complete":
            services.complete(p, request.user)
            messages.success(request, _("Marked as done."))
        else:
            raise Http404
    except services.ComplianceError as exc:
        messages.error(request, str(exc))
    return redirect("compliance:period", pk=p.pk)


@require_perm(perms.BOARD_SHELL)
def item_edit(request, pk=None):
    item = get_object_or_404(ManualItem.objects, pk=pk) if pk else None
    can_manage = _can_manage(request)
    if request.method == "POST":
        _manage(request)
        form = ManualItemForm(request.POST, instance=item)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.association = request.association
            cal.save_item(obj, request.user)
            messages.success(request, _("Saved."))
            return redirect("compliance:index")
    else:
        initial = {}
        if item is None:
            initial = cal.suggestion(
                request.GET.get("suggest", ""), association_today(request.association)
            )
        form = ManualItemForm(instance=item, initial=initial)
    return render(
        request,
        "compliance/item.html",
        {"item": item, "form": form, "can_manage": can_manage},
    )


@require_POST
@require_perm(perms.BOARD_SHELL)
def item_done(request, pk):
    _manage(request)
    item = get_object_or_404(ManualItem.objects, pk=pk)
    try:
        cal.mark_done(item, request.user, upload=request.FILES.get("file"))
        messages.success(request, _("Marked as done."))
    except cal.CalendarError as exc:
        messages.error(request, str(exc))
    return redirect("compliance:index")


@require_POST
@require_perm(perms.BOARD_SHELL)
def item_remove(request, pk):
    _manage(request)
    item = get_object_or_404(ManualItem.objects, pk=pk)
    item.active = False
    item.save(update_fields=["active"])
    messages.success(request, _("Removed from the calendar."))
    return redirect("compliance:index")


@require_POST
@require_perm(perms.BOARD_SHELL)
def reminder_settings(request):
    _manage(request)
    try:
        days = cal.parse_offsets(request.POST.get("days", ""))
    except cal.CalendarError as exc:
        messages.error(request, str(exc))
    else:
        cal.set_reminder_offsets(request.association, days, by=request.user)
        messages.success(request, _("Reminder schedule saved."))
    return redirect("compliance:index")


@require_perm(perms.BOARD_SHELL)
def evidence_file(request, pk):
    ev = get_object_or_404(Evidence.objects, pk=pk)
    if not ev.file_key:
        raise Http404
    data = get_provider("FileStorage", request.association).get(ev.file_key)
    resp = HttpResponse(data, content_type=ev.content_type or "application/octet-stream")
    safe = "".join(c for c in ev.filename if c.isalnum() or c in "._- ") or "evidence"
    resp["Content-Disposition"] = f'attachment; filename="{safe}"'
    resp["Referrer-Policy"] = "no-referrer"
    return resp
