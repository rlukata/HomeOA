"""Requests hub > Maintenance (board) and the resident submit/track pages under ``/me/``.

Board pages need ``maintenance.manage``. Resident pages sit behind the resident gate
 and need ``maintenance.submit`` (owners and tenants); a resident only ever
sees requests they submitted.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.access.services import units_for
from homeoa.ap.models import Bill
from homeoa.changes.models import ChangeRequest
from homeoa.core.context import association_today
from homeoa.documents import services as documents
from homeoa.registry.models import Person
from homeoa.ui.nav import get_hub

from . import oneoff, services
from .forms import (
    LinkBillForm,
    RequestForm,
    RequestStatusForm,
    ResidentRequestForm,
    WorkOrderForm,
    WorkOrderStatusForm,
)
from .models import MaintenancePhoto, MaintenanceRequest, WorkOrder

MANAGE = perms.MAINTENANCE_MANAGE

REQUEST_BADGES = {"new": "warn", "in_progress": "accent", "done": "ok", "declined": ""}
WO_BADGES = {
    "draft": "",
    "sent": "accent",
    "scheduled": "accent",
    "in_progress": "warn",
    "completed": "ok",
    "cancelled": "",
}
FILTERS = {
    "open": ("new", "in_progress"),
    "new": ("new",),
    "in_progress": ("in_progress",),
    "done": ("done",),
    "declined": ("declined",),
}


def _render(request, template, context, status=200):
    hub = get_hub("requests", request.association)
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "maintenance"}
        for i, t in enumerate(hub.subtabs)
    ]
    ctx = {
        "current_hub": hub,
        "subtabs": tabs,
        "today": association_today(request.association),
        **context,
    }
    return render(request, template, ctx, status=status)


def _decorate_requests(reqs):
    for r in reqs:
        r.badge = REQUEST_BADGES.get(r.status, "")
    return reqs


def _decorate_wos(wos, today):
    for wo in wos:
        wo.badge = WO_BADGES.get(wo.status, "")
        wo.overdue = wo.is_overdue(today)
    return wos


@require_perm(MANAGE)
def index(request):
    status = request.GET.get("status", "open")
    if status not in FILTERS and status != "all":
        status = "open"
    qs = MaintenanceRequest.objects.select_related("unit", "submitted_by")
    if status != "all":
        qs = qs.filter(status__in=FILTERS[status])
    today = association_today(request.association)
    wos = WorkOrder.objects.filter(status__in=WorkOrder.OPEN).select_related("vendor", "request")
    context = {
        "requests": _decorate_requests(list(qs)),
        "work_orders": _decorate_wos(list(wos.order_by("scheduled_for", "pk")), today),
        "status": status,
        "statuses": [
            ("open", _("Open")),
            ("new", _("New")),
            ("in_progress", _("In progress")),
            ("done", _("Done")),
            ("declined", _("Declined")),
            ("all", _("All")),
        ],
    }
    return _render(request, "maintenance/list.html", context)


@require_perm(MANAGE)
def add(request):
    form = RequestForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        req = services.submit_request(
            title=data["title"],
            unit=data["unit"],
            location=data["location"],
            description=data["description"],
            priority=data["priority"],
            submitted_by=data["submitted_by"],
            entered_by=request.user,
            photos=data["photos"],
        )
        messages.success(request, _("Maintenance request saved."))
        return redirect("maintenance:detail", pk=req.pk)
    status = 400 if request.method == "POST" else 200
    return _render(request, "maintenance/request_form.html", {"form": form}, status=status)


def _request(pk) -> MaintenanceRequest:
    return get_object_or_404(
        MaintenanceRequest.objects.select_related("unit", "submitted_by", "entered_by"), pk=pk
    )


@require_perm(MANAGE)
def detail(request, pk):
    req = _request(pk)
    _decorate_requests([req])
    today = association_today(request.association)
    wos = _decorate_wos(list(req.work_orders.select_related("vendor")), today)
    context = {
        "req": req,
        "work_orders": wos,
        "status_form": RequestStatusForm(initial={"status": req.status}),
        "history": req.history.select_related("changed_by"),
        "photos": req.photos.select_related("version"),
    }
    return _render(request, "maintenance/detail.html", context)


@require_POST
@require_perm(MANAGE)
def request_status(request, pk):
    req = _request(pk)
    form = RequestStatusForm(request.POST)
    if form.is_valid():
        services.set_request_status(
            req, form.cleaned_data["status"], user=request.user, note=form.cleaned_data["note"]
        )
        messages.success(request, _("Status updated."))
    else:
        messages.error(request, _("Choose a valid status."))
    return redirect("maintenance:detail", pk=req.pk)


@require_POST
@require_perm(MANAGE)
def add_photos(request, pk):
    req = _request(pk)
    uploads = request.FILES.getlist("photos")
    try:
        services.add_photos(req, uploads, user=request.user)
    except services.MaintenanceError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if uploads:
            messages.success(request, _("Photos added."))
    return redirect("maintenance:detail", pk=req.pk)


def _photo_response(photo: MaintenancePhoto) -> HttpResponse:
    version = photo.version
    response = HttpResponse(documents.read_file(version), content_type=version.content_type)
    response["Content-Disposition"] = f'inline; filename="{version.file_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_perm(MANAGE)
def photo(request, pk, photo_pk):
    return _photo_response(
        get_object_or_404(
            MaintenancePhoto.objects.select_related("version"), pk=photo_pk, request_id=pk
        )
    )


# Work orders ---------------------------------------------------------------------------


def _work_order(pk) -> WorkOrder:
    return get_object_or_404(
        WorkOrder.objects.select_related("vendor", "request", "request__unit"), pk=pk
    )


@require_perm(MANAGE)
def work_order_add(request, pk=None):
    req = _request(pk) if pk is not None else None
    initial = {}
    if req is not None:
        initial["scope"] = "\n".join(p for p in (req.title, req.description) if p)
    form = WorkOrderForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        wo = services.create_work_order(
            vendor=data["vendor"],
            scope=data["scope"],
            request=req,
            estimated_cost=data["estimated_cost"],
            scheduled_for=data["scheduled_for"],
            user=request.user,
        )
        messages.success(request, _("Work order created."))
        return redirect("maintenance:work_order", pk=wo.pk)
    status = 400 if request.method == "POST" else 200
    return _render(
        request,
        "maintenance/work_order_form.html",
        {"form": form, "req": req, "title": _("New work order")},
        status=status,
    )


@require_perm(MANAGE)
def work_order_edit(request, pk):
    wo = _work_order(pk)
    form = WorkOrderForm(request.POST or None, instance=wo)
    if request.method == "POST" and form.is_valid():
        if oneoff.estimate_needs_approval(
            WorkOrder.objects.get(pk=wo.pk), form.cleaned_data["estimated_cost"]
        ):
            wo.refresh_from_db()
            change = oneoff.propose_estimate(wo, form, user=request.user)
            return _sent_for_approval(request, change, wo)
        form.save()
        messages.success(request, _("Work order saved."))
        return redirect("maintenance:work_order", pk=wo.pk)
    status = 400 if request.method == "POST" else 200
    return _render(
        request,
        "maintenance/work_order_form.html",
        {"form": form, "req": wo.request, "wo": wo, "title": _("Edit work order")},
        status=status,
    )


def _sent_for_approval(request, change, wo):
    if change.status == ChangeRequest.Status.APPLIED:
        messages.success(request, _("Status updated."))
    elif change.status == ChangeRequest.Status.FAILED:
        messages.error(request, change.error)
    else:
        messages.success(
            request,
            _(
                "This job is over the one-off limit of %(limit)s (or has no estimate), so it "
                "was sent to the board for approval."
            )
            % {"limit": f"${oneoff.limit(wo.association):,.2f}"},
        )
    return redirect("maintenance:work_order", pk=wo.pk)


def _bill_prefill_url(wo: WorkOrder) -> str:
    # The AP bill form prefills vendor, description and amount (the estimate) from the work
    # order and links the bill to it on save.
    return reverse("ap:add") + "?" + urlencode({"vendor": wo.vendor_id, "work_order": wo.pk})


@require_perm(MANAGE)
def work_order(request, pk):
    wo = _work_order(pk)
    _decorate_wos([wo], association_today(request.association))
    bills = list(wo.bills.prefetch_related("lines").order_by("bill_date", "pk"))
    context = {
        "wo": wo,
        "bills": bills,
        "status_form": WorkOrderStatusForm(
            initial={"status": wo.status, "scheduled_for": wo.scheduled_for}
        ),
        "link_form": LinkBillForm(work_order=wo),
        "history": wo.history.select_related("changed_by"),
        "create_bill_url": _bill_prefill_url(wo),
        "oneoff_limit": oneoff.limit(request.association),
        "pending_approvals": list(oneoff.pending_for(wo)),
    }
    return _render(request, "maintenance/work_order.html", context)


@require_POST
@require_perm(MANAGE)
def work_order_status(request, pk):
    wo = _work_order(pk)
    form = WorkOrderStatusForm(request.POST)
    if form.is_valid() and oneoff.needs_approval(wo, form.cleaned_data["status"]):
        if oneoff.pending_for(wo).exists():
            messages.info(request, _("This work order is already waiting for board approval."))
            return redirect("maintenance:work_order", pk=wo.pk)
        data = form.cleaned_data
        change = oneoff.propose_commission(
            wo,
            status=data["status"],
            note=data["note"],
            scheduled_for=data["scheduled_for"],
            user=request.user,
        )
        return _sent_for_approval(request, change, wo)
    if form.is_valid():
        data = form.cleaned_data
        services.set_work_order_status(
            wo,
            data["status"],
            user=request.user,
            note=data["note"],
            scheduled_for=data["scheduled_for"],
        )
        messages.success(request, _("Status updated."))
    else:
        messages.error(request, _("Choose a valid status."))
    return redirect("maintenance:work_order", pk=wo.pk)


@require_POST
@require_perm(MANAGE)
def link_bill(request, pk):
    wo = _work_order(pk)
    form = LinkBillForm(request.POST, work_order=wo)
    if form.is_valid():
        try:
            services.link_bill(wo, form.cleaned_data["bill"], user=request.user)
        except services.MaintenanceError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, _("Bill linked."))
    else:
        messages.error(request, _("Choose one of this vendor's bills."))
    return redirect("maintenance:work_order", pk=wo.pk)


@require_POST
@require_perm(MANAGE)
def unlink_bill(request, pk, bill_pk):
    wo = _work_order(pk)
    bill = get_object_or_404(Bill.objects.all(), pk=bill_pk)
    if services.unlink_bill(wo, bill, user=request.user):
        messages.success(request, _("Bill unlinked."))
    return redirect("maintenance:work_order", pk=wo.pk)


@require_perm(MANAGE)
def work_order_print(request, pk):
    wo = _work_order(pk)
    return render(
        request,
        "maintenance/work_order_print.html",
        {"wo": wo, "association": request.association},
    )


# Resident pages ------------------------------------------------------------------------


def _person(request):
    return Person.objects.filter(user=request.user).first()


def _resident_render(request, template, **extra):
    from homeoa.portal.views.base import _render as portal_render

    return portal_render(request, "requests", template, **extra)


@resident_view(perm=perms.MAINTENANCE_SUBMIT)
def my_requests(request):
    person = _person(request)
    reqs = (
        MaintenanceRequest.objects.filter(submitted_by=person).select_related("unit")
        if person
        else MaintenanceRequest.objects.none()
    )
    return _resident_render(
        request, "maintenance/me_list.html", requests=_decorate_requests(list(reqs))
    )


@resident_view(perm=perms.MAINTENANCE_SUBMIT)
def my_request_add(request):
    person = _person(request)
    units = units_for(request.user, request.association)
    form = ResidentRequestForm(request.POST or None, request.FILES or None, units=units)
    if request.method == "POST" and person is not None and form.is_valid():
        data = form.cleaned_data
        req = services.submit_request(
            title=data["title"],
            unit=data["unit"],
            location=data["location"],
            description=data["description"],
            priority=data["priority"],
            submitted_by=person,
            entered_by=request.user,
            photos=data["photos"],
        )
        messages.success(request, _("Thanks. The board has your request."))
        return redirect("maintenance:my_detail", pk=req.pk)
    return _resident_render(request, "maintenance/me_form.html", form=form)


def _my_request(request, pk) -> MaintenanceRequest:
    person = _person(request)
    if person is None:
        from django.http import Http404

        raise Http404
    return get_object_or_404(
        MaintenanceRequest.objects.select_related("unit"), pk=pk, submitted_by=person
    )


@resident_view(perm=perms.MAINTENANCE_SUBMIT)
def my_request_detail(request, pk):
    req = _my_request(request, pk)
    _decorate_requests([req])
    return _resident_render(
        request,
        "maintenance/me_detail.html",
        req=req,
        history=req.history.all(),
        photos=req.photos.select_related("version"),
    )


@resident_view(perm=perms.MAINTENANCE_SUBMIT)
def my_photo(request, pk, photo_pk):
    req = _my_request(request, pk)
    return _photo_response(
        get_object_or_404(
            MaintenancePhoto.objects.select_related("version"), pk=photo_pk, request=req
        )
    )
