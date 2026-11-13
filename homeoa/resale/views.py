"""Resale certificate pages: Units > Resale certificates for the board, More > Resale
certificate for the selling owner. Statements of unpaid assessments use the same pages."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from homeoa.access import permissions as perms
from homeoa.access.decorators import require_perm, resident_view
from homeoa.access.services import has_perm, today_for
from homeoa.changes.replay import money_change
from homeoa.registry.models import Person, Unit
from homeoa.ui.nav import get_hub

from . import catalog, services
from .models import ResaleRequest

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


KIND_CHOICES = ResaleRequest.Kind.choices


class LogForm(forms.Form):
    kind = forms.ChoiceField(
        label=gettext_lazy("Document"),
        choices=KIND_CHOICES,
        initial=ResaleRequest.Kind.CERTIFICATE,
        required=False,
        widget=forms.RadioSelect,
    )
    unit = forms.ModelChoiceField(label=gettext_lazy("Unit"), queryset=Unit.objects.none())
    requester_name = forms.CharField(
        label=gettext_lazy("Requested by (agent, escrow, lender or owner)"), max_length=200
    )
    send_to = forms.EmailField(label=gettext_lazy("Send it to"))
    received_on = forms.DateField(label=gettext_lazy("Received on"), widget=DATE)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit"].queryset = Unit.objects.all()


class OwnerForm(forms.Form):
    kind = forms.ChoiceField(
        label=gettext_lazy("What do you need?"),
        choices=KIND_CHOICES,
        initial=ResaleRequest.Kind.CERTIFICATE,
        required=False,
        widget=forms.RadioSelect,
        help_text=gettext_lazy(
            "A resale certificate for a sale; a statement of unpaid assessments for a "
            "refinance or a lender."
        ),
    )
    send_to = forms.EmailField(
        label=gettext_lazy("Send it to"),
        help_text=gettext_lazy("Usually your escrow officer, the buyer's agent or your lender."),
    )
    note = forms.CharField(
        label=gettext_lazy("Anything the board should know (optional)"),
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


def _render(request, template, context, status=200):
    hub = get_hub("units")
    tabs = [
        {"label": t.label, "url": hub.subtab_url(i), "on": t.slug == "resale"}
        for i, t in enumerate(hub.subtabs)
    ]
    response = render(request, template, {"current_hub": hub, "subtabs": tabs, **context})
    response.status_code = status
    return response


@require_perm(perms.UNITS_MANAGE)
def index(request):
    association = request.association
    form = LogForm(request.POST or None, initial={"received_on": today_for(association)})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        req = services.open_request(
            association,
            unit=d["unit"],
            on=d["received_on"],
            send_to=d["send_to"],
            requester_name=d["requester_name"],
            kind=d["kind"] or ResaleRequest.Kind.CERTIFICATE,
        )
        messages.success(request, _("Request logged."))
        return redirect("resale:detail", pk=req.pk)
    return _render(
        request,
        "resale/index.html",
        {"requests": ResaleRequest.objects.select_related("unit", "seller"), "form": form},
        status=400 if request.method == "POST" else 200,
    )


def _request(pk) -> ResaleRequest:
    return get_object_or_404(ResaleRequest.objects.select_related("unit", "seller"), pk=pk)


@require_perm(perms.UNITS_MANAGE)
def detail(request, pk):
    req = _request(pk)
    association = request.association
    if request.method == "POST" and request.POST.get("action") == "save":
        answers = {k[2:]: v for k, v in request.POST.items() if k.startswith("a-")}
        try:
            services.save_answers(req, answers, user=request.user)
            messages.success(request, _("Answers saved."))
        except ValidationError as exc:
            for m in exc.messages:
                messages.error(request, m)
        return redirect("resale:detail", pk=req.pk)
    form = catalog.FORMS.get(req.form_id)
    cap = services.fee_cap(association, today_for(association), req.kind)
    last = (
        ResaleRequest.objects.filter(
            status=ResaleRequest.Status.ISSUED, fee__isnull=False, kind=req.kind
        )
        .order_by("-issued_on")
        .values_list("fee", flat=True)
        .first()
    )
    return _render(
        request,
        "resale/detail.html",
        {
            "req": req,
            "form": form,
            "items": services.items(req),
            "fee_cap": cap,
            "default_fee": _default_fee(association, cap, last),
            "can_issue": has_perm(request.user, perms.FINANCE_MANAGE, association=association)
            or has_perm(request.user, perms.FINANCE_PROPOSE, association=association),
        },
    )


def _default_fee(association, cap, last):
    """The fee the board adopted (Settings > Association setup), else the last fee
    charged, else the cap; never above the cap."""
    from homeoa.setup.targets import resale_fee

    fee = resale_fee(association)
    if fee is None:
        fee = last if last is not None else cap
    if fee is not None and cap is not None:
        fee = min(fee, cap)
    return fee


def _describe_issue(request, pk):
    req = ResaleRequest.objects.select_related("unit").filter(pk=pk).first()
    unit = str(req.unit) if req else f"#{pk}"
    return (
        _("Issue: %(document)s") % {"document": req} if req else _("Issue #%(pk)s") % {"pk": pk},
        [(_("Unit"), unit), (_("Fee"), request.POST.get("fee", ""))],
    )


@money_change("resale.issue", gettext_lazy("Issue a resale certificate"), describe=_describe_issue)
@require_POST
@require_perm(perms.FINANCE_MANAGE)
def issue(request, pk):
    req = _request(pk)
    try:
        fee = Decimal(request.POST.get("fee") or "0").quantize(Decimal("0.01"))
    except InvalidOperation:
        messages.error(request, _("Enter the fee as an amount."))
        return redirect("resale:detail", pk=req.pk)
    try:
        services.issue(req, fee=fee, user=request.user, on=today_for(request.association))
        messages.success(request, _("Issued and emailed."))
    except ValidationError as exc:
        for m in exc.messages:
            messages.error(request, m)
    return redirect("resale:detail", pk=req.pk)


@require_POST
@require_perm(perms.UNITS_MANAGE)
def cancel(request, pk):
    req = _request(pk)
    try:
        services.cancel(req, user=request.user, reason=request.POST.get("reason", ""))
        messages.success(request, _("Request cancelled."))
    except ValidationError as exc:
        for m in exc.messages:
            messages.error(request, m)
    return redirect("resale:detail", pk=req.pk)


def _pdf_response(req):
    if not req.pdf_key:
        raise Http404
    resp = HttpResponse(services.pdf_bytes(req), content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="{services.filename(req)}.pdf"'
    return resp


@require_perm(perms.UNITS_MANAGE)
def pdf(request, pk):
    return _pdf_response(_request(pk))


def _seller(request) -> tuple[Person | None, list[Unit]]:
    from homeoa.portal.views.mine import my_person
    from homeoa.registry.services import units_owned_by

    person = my_person(request)
    units = sorted(units_owned_by(person), key=lambda u: u.sort_key) if person else []
    return person, units


@resident_view(perm=perms.BALANCE_OWN)
def portal(request):
    from homeoa.portal.views.base import _render as portal_render

    person, units = _seller(request)
    if person is None or not units:
        raise PermissionDenied
    form = OwnerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        unit = next((u for u in units if u.number == request.POST.get("unit")), units[0])
        req = services.submit(
            request.association,
            unit=unit,
            seller=person,
            send_to=form.cleaned_data["send_to"],
            note=form.cleaned_data["note"],
            on=today_for(request.association),
            kind=form.cleaned_data["kind"] or ResaleRequest.Kind.CERTIFICATE,
        )
        messages.success(
            request,
            _("Request sent. The board will send it by %(date)s.")
            % {"date": req.due_on.isoformat()}
            if req.due_on
            else _("Request sent to the board."),
        )
        return redirect("resale_portal:index")
    mine = ResaleRequest.objects.filter(seller=person).select_related("unit")
    response = portal_render(
        request, "more", "resale/portal.html", form=form, units=units, requests=list(mine)
    )
    if request.method == "POST":
        response.status_code = 400
    return response


@resident_view(perm=perms.BALANCE_OWN)
def portal_pdf(request, pk):
    person, _units = _seller(request)
    req = get_object_or_404(ResaleRequest.objects.select_related("unit"), pk=pk, seller=person)
    return _pdf_response(req)
